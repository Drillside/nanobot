"""Render Free launcher with encrypted checkpoints and one active gateway.

/healthz reports controller availability, including setup/standby states.
/readyz reports whether the real gateway is ready. This lets Render drain an
old instance before its replacement acquires the state lease and starts OAuth.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import signal
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web
from cryptography.fernet import Fernet, InvalidToken

MAX_RAW_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES = 6 * 1024 * 1024
MAX_MEDIA_BYTES = 2 * 1024 * 1024
EXCLUDED_DIRS = {".git", ".venv", "node_modules", "__pycache__", "logs", "cache", ".cache"}
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "content-length"}


class StateError(Exception):
    """Safe to log: never include credentials or remote response bodies."""


def candidates(root: Path):
    if not root.exists():
        return
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if name not in EXCLUDED_DIRS
                          and not name.startswith("speech-")
                          and not (Path(directory) / name).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root)
            if name.endswith((".lock", ".pid", ".log", ".tmp", "-shm")):
                continue
            try:
                if relative.parts[0] == "media" and path.stat().st_size > MAX_MEDIA_BYTES:
                    continue
            except FileNotFoundError:
                continue
            yield path, relative.as_posix()


def fingerprint(root: Path):
    values = []
    for path, name in candidates(root):
        try:
            metadata = path.stat()
        except FileNotFoundError:
            continue
        values.append((name, metadata.st_size, metadata.st_mtime_ns))
    return tuple(values)


def sqlite_bytes(path: Path) -> bytes:
    with tempfile.TemporaryDirectory(prefix="nanobot-db-") as temporary:
        destination = Path(temporary) / "snapshot.sqlite3"
        deadline = time.monotonic() + 8
        def progress(_status, _remaining, _total):
            if time.monotonic() > deadline:
                raise StateError("A history database was busy during backup.")
        try:
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1) as source:
                with sqlite3.connect(destination) as target:
                    source.backup(target, pages=256, progress=progress, sleep=0.01)
        except sqlite3.Error:
            raise StateError("A history database could not be checkpointed safely.") from None
        if destination.stat().st_size > MAX_RAW_BYTES:
            raise StateError("History exceeds the free checkpoint size limit.")
        return destination.read_bytes()


def make_archive(root: Path) -> bytes:
    output, total = io.BytesIO(), 0
    with tarfile.open(fileobj=output, mode="w:gz", compresslevel=3) as archive:
        for path, name in candidates(root):
            if name.endswith("-wal"):
                continue
            if total + path.stat().st_size > MAX_RAW_BYTES:
                raise StateError("State exceeds the free checkpoint size limit.")
            with path.open("rb") as handle:
                is_database = handle.read(16) == b"SQLite format 3\x00"
            body = sqlite_bytes(path) if is_database else path.read_bytes()
            total += len(body)
            if total > MAX_RAW_BYTES:
                raise StateError("State exceeds the free checkpoint size limit.")
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(body), 0o600
            archive.addfile(info, io.BytesIO(body))
    value = output.getvalue()
    if len(value) > MAX_ARCHIVE_BYTES:
        raise StateError("State exceeds the free checkpoint request size limit.")
    return value


def restore_archive(root: Path, payload: bytes):
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    total, pending = 0, []
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            for member in archive:
                relative = PurePosixPath(member.name)
                if (not member.isfile() or relative.is_absolute() or ".." in relative.parts
                        or "\\" in member.name or not relative.parts):
                    raise StateError("Stored state contains an unsafe path.")
                target = (root / member.name).resolve()
                if not target.is_relative_to(root):
                    raise StateError("Stored state escapes the state directory.")
                total += member.size
                if total > MAX_RAW_BYTES:
                    raise StateError("Stored state exceeds the restore size limit.")
                source = archive.extractfile(member)
                if source is None:
                    raise StateError("Stored state contains an unreadable file.")
                pending.append((target, source.read()))
    except (tarfile.TarError, OSError):
        raise StateError("Stored state is not a valid checkpoint.") from None
    for target, body in pending:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)


class RemoteState:
    def __init__(self, url: str, token: str, secret: str, namespace: str,
                 client: httpx.AsyncClient | None = None):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".upstash.io")
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise StateError("Use your Upstash database's HTTPS REST URL.")
        self.url, self.token, self.prefix = url.rstrip("/"), token, namespace + ":"
        self.cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))
        self.owner = uuid4().hex
        self.client = client or httpx.AsyncClient(timeout=10, follow_redirects=False)

    async def command(self, *parts):
        try:
            response = await self.client.post(self.url, json=list(parts),
                                              headers={"Authorization": "Bearer " + self.token})
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or "error" in result:
                raise StateError("Storage rejected a command. Check the database's limits.")
            return result.get("result")
        except (httpx.HTTPError, ValueError):
            raise StateError("State storage is unavailable. Check its private Render settings.") from None

    async def acquire(self) -> bool:
        return await self.command("SET", self.prefix + "lease", self.owner, "NX", "EX", 60) == "OK"

    async def renew(self) -> bool:
        return await self.command("EVAL", "if redis.call('get',KEYS[1]) == ARGV[1] then "
                                  "return redis.call('expire',KEYS[1],60) else return 0 end",
                                  1, self.prefix + "lease", self.owner) == 1

    async def release(self):
        await self.command("EVAL", "if redis.call('get',KEYS[1]) == ARGV[1] then "
                           "return redis.call('del',KEYS[1]) else return 0 end",
                           1, self.prefix + "lease", self.owner)

    async def save(self, payload: bytes):
        encrypted = self.cipher.encrypt(payload).decode()
        result = await self.command("EVAL", "if redis.call('get',KEYS[1]) == ARGV[1] then "
                                    "redis.call('set',KEYS[2],ARGV[2]); return 1 else return 0 end",
                                    2, self.prefix + "lease", self.prefix + "snapshot", self.owner, encrypted)
        if result != 1:
            raise StateError("This instance no longer owns the state lease.")

    async def load(self) -> bytes | None:
        value = await self.command("GET", self.prefix + "snapshot")
        if value is None:
            return None
        try:
            return self.cipher.decrypt(value.encode())
        except (InvalidToken, ValueError, AttributeError):
            raise StateError("Stored state cannot be decrypted. Keep the original encryption secret.") from None


class Launcher:
    def __init__(self, root: Path | None = None):
        self.root = root or Path.home() / ".nanobot"
        self.state, self.stop = "starting", asyncio.Event()
        self.child: asyncio.subprocess.Process | None = None
        self.session: ClientSession | None = None
        self.backend = "http://127.0.0.1:8766"

    def application(self):
        app = web.Application(client_max_size=20 * 1024 * 1024)
        app.router.add_get("/healthz", self.health)
        app.router.add_get("/readyz", self.ready)
        app.router.add_route("*", "/{path:.*}", self.proxy)
        return app

    async def health(self, _request):
        return web.json_response({"status": self.state}, status=503 if self.state == "error" else 200)

    async def ready(self, _request):
        return web.json_response({"ready": self.state == "ready"}, status=200 if self.state == "ready" else 503)

    async def proxy(self, request):
        if self.state != "ready" or self.session is None:
            if self.state == "setup_required":
                message = ("Nanobot is deployed. Create a FREE Redis database at https://console.upstash.com, "
                           "then privately add UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN in this "
                           "Render service's Environment settings and redeploy. Keep STATE_ENCRYPTION_KEY "
                           "and STATE_NAMESPACE unchanged. Return here afterward to connect ChatGPT and Telegram.")
            else:
                message = "Your private assistant is starting. Please try again shortly."
            return web.Response(text=message, status=503, headers={"Cache-Control": "no-store"})
        headers = {key: value for key, value in request.headers.items()
                   if key.lower() not in HOP_HEADERS and not key.lower().startswith("sec-websocket-")}
        url = self.backend + str(request.rel_url)
        try:
            if request.headers.get("Upgrade", "").lower() == "websocket":
                protocols = tuple(x.strip() for x in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if x.strip())
                upstream = await self.session.ws_connect(url, headers=headers, protocols=protocols,
                                                        max_msg_size=20 * 1024 * 1024)
                downstream = web.WebSocketResponse(protocols=protocols, max_msg_size=20 * 1024 * 1024)
                await downstream.prepare(request)
                async def copy(source, target):
                    async for message in source:
                        if message.type == WSMsgType.TEXT:
                            await target.send_str(message.data)
                        elif message.type == WSMsgType.BINARY:
                            await target.send_bytes(message.data)
                        elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                            break
                tasks = [asyncio.create_task(copy(upstream, downstream)), asyncio.create_task(copy(downstream, upstream))]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await upstream.close()
                    await downstream.close()
                return downstream
            async with self.session.request(request.method, url, headers=headers, data=await request.read(),
                                            allow_redirects=False) as upstream:
                response = web.StreamResponse(status=upstream.status, headers=[
                    (key, value) for key, value in upstream.headers.items() if key.lower() not in HOP_HEADERS])
                await response.prepare(request)
                async for body in upstream.content.iter_chunked(65536):
                    await response.write(body)
                await response.write_eof()
                return response
        except (ClientError, OSError, asyncio.TimeoutError):
            return web.Response(text="The assistant is restarting. Please try again shortly.", status=502)

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)
        runner = web.AppRunner(self.application(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", "8765"))).start()
        url, token = os.environ.get("UPSTASH_REDIS_REST_URL"), os.environ.get("UPSTASH_REDIS_REST_TOKEN")
        secret = os.environ.get("STATE_ENCRYPTION_KEY")
        if not url or not token or not secret:
            self.state = "setup_required"
            await self.stop.wait()
            await runner.cleanup()
            return
        store = RemoteState(url, token, secret, os.environ.get("STATE_NAMESPACE", "nanobot-telegram"))
        owns_lease = False
        try:
            while not self.stop.is_set():
                if await store.acquire():
                    owns_lease = True
                    break
                try:
                    await asyncio.wait_for(self.stop.wait(), 2)
                except TimeoutError:
                    pass
            if not owns_lease:
                return
            checkpoint = await store.load()
            if checkpoint:
                await asyncio.to_thread(restore_archive, self.root, checkpoint)
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / "auth").mkdir(exist_ok=True)
            child_env = dict(os.environ)
            child_env["NANOBOT_RENDER_FREE_CHILD"] = "1"
            child_env["OAUTH_CLI_KIT_TOKEN_PATH"] = str(self.root / "auth" / "codex.json")
            self.child = await asyncio.create_subprocess_exec("/usr/local/bin/entrypoint.sh", "gateway", env=child_env)
            previous, renewed = None, time.monotonic()
            self.session = ClientSession(timeout=ClientTimeout(total=None, sock_connect=5, sock_read=120), auto_decompress=False)
            while not self.stop.is_set():
                if self.child.returncode is not None:
                    raise StateError("The gateway stopped. Check Render logs for startup errors.")
                if time.monotonic() - renewed >= 10:
                    if not await store.renew():
                        owns_lease = False
                        raise StateError("This gateway's state lease expired.")
                    renewed = time.monotonic()
                current = await asyncio.to_thread(fingerprint, self.root)
                if current != previous:
                    await store.save(await asyncio.to_thread(make_archive, self.root))
                    previous = current
                if self.state != "ready":
                    try:
                        async with self.session.get("http://127.0.0.1:18790/health", timeout=ClientTimeout(total=2)) as response:
                            payload = await response.json()
                            if response.status == 200 and payload.get("ready") is True:
                                self.state = "ready"
                    except (ClientError, OSError, asyncio.TimeoutError):
                        pass
                try:
                    await asyncio.wait_for(self.stop.wait(), 3)
                except TimeoutError:
                    pass
        except (StateError, OSError) as error:
            self.state = "error"
            print(str(error) if isinstance(error, StateError) else "The launcher could not access its state.", flush=True)
            raise SystemExit(1) from None
        finally:
            if self.child and self.child.returncode is None:
                self.child.terminate()
                try:
                    await asyncio.wait_for(self.child.wait(), 8)
                except TimeoutError:
                    self.child.kill()
                    await self.child.wait()
            if owns_lease:
                try:
                    await store.save(await asyncio.to_thread(make_archive, self.root))
                except (StateError, OSError):
                    print("Final checkpoint failed; the last completed checkpoint remains available.", flush=True)
                finally:
                    try:
                        await store.release()
                    except StateError:
                        print("The state lease will expire automatically.", flush=True)
            await store.client.aclose()
            if self.session:
                await self.session.close()
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(Launcher().run())
