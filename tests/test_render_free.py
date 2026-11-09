from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from pathlib import Path

import httpx
import pytest
from aiohttp import ClientSession, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from deploy.render_free import Launcher, RemoteState, StateError, make_archive, restore_archive


def test_checkpoint_restores_auth_and_committed_sqlite_wal(tmp_path):
    original = tmp_path / "original"
    (original / "auth").mkdir(parents=True)
    (original / "auth" / "codex.json").write_text('{"refresh":"dummy-not-a-real-token"}')
    with sqlite3.connect(original / "sessions.sqlite3") as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE chat (text TEXT)")
        connection.execute("INSERT INTO chat VALUES ('recent conversation')")
        connection.commit()
        checkpoint = make_archive(original)
    restored = tmp_path / "restored"
    restore_archive(restored, checkpoint)
    assert json.loads((restored / "auth" / "codex.json").read_text())["refresh"] == "dummy-not-a-real-token"
    with sqlite3.connect(restored / "sessions.sqlite3") as connection:
        assert connection.execute("SELECT text FROM chat").fetchone() == ("recent conversation",)


def test_restore_rejects_unsafe_archive_members_before_writing(tmp_path):
    for name, kind in [("../escaped", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)]:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = "../escaped"
            archive.addfile(info, io.BytesIO())
        with pytest.raises(StateError):
            restore_archive(tmp_path / "state", output.getvalue())
    assert not (tmp_path / "escaped").exists()


def test_checkpoint_does_not_follow_workspace_symlinks(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    private = tmp_path / "outside"
    private.write_text("outside directory")
    (state / "symlink").symlink_to(private)
    (state / "config.json").write_text("{}")
    restore_archive(tmp_path / "restored", make_archive(state))
    assert (tmp_path / "restored" / "config.json").exists()
    assert not (tmp_path / "restored" / "symlink").exists()


async def test_encryption_lease_handover_and_stale_writer_rejection():
    values = {}
    def redis(request):
        command = json.loads(request.content)
        if command[0] == "SET":
            if command[1] in values:
                return httpx.Response(200, json={"result": None})
            values[command[1]] = command[2]
            result = "OK"
        elif command[0] == "GET":
            result = values.get(command[1])
        elif command[0] == "EVAL":
            script, key_count = command[1:3]
            keys = command[3:3 + key_count]
            args = command[3 + key_count:]
            result = 0
            if values.get(keys[0]) == args[0]:
                result = 1
                if key_count == 2:
                    values[keys[1]] = args[1]
                elif "'del'" in script:
                    values.pop(keys[0], None)
        else:
            raise AssertionError(command[0])
        return httpx.Response(200, json={"result": result})
    async with httpx.AsyncClient(transport=httpx.MockTransport(redis)) as client:
        one = RemoteState("https://test.upstash.io", "dummy-token", "dummy-encryption-key", "test", client)
        two = RemoteState("https://test.upstash.io", "dummy-token", "dummy-encryption-key", "test", client)
        assert await one.acquire()
        assert not await two.acquire()
        assert await one.renew()
        await one.save(b"private state")
        assert "private state" not in json.dumps(values)
        await one.release()
        assert await two.acquire()
        assert await two.load() == b"private state"
        with pytest.raises(StateError, match="lease"):
            await one.save(b"stale state")
        assert await two.load() == b"private state"


async def test_launcher_distinguishes_setup_from_agent_readiness():
    launcher = Launcher(Path("/unused-in-this-test"))
    launcher.state = "setup_required"
    async with TestClient(TestServer(launcher.application())) as client:
        assert (await client.get("/healthz")).status == 200
        assert (await (await client.get("/readyz")).json()) == {"ready": False}
        response = await client.get("/")
        assert response.status == 503
        assert "UPSTASH_REDIS_REST_URL" in await response.text()


async def test_proxy_preserves_private_ui_auth_and_websocket_messages():
    backend = web.Application()
    async def protected(request):
        if request.headers.get("Authorization") != "Bearer fixture":
            return web.Response(status=401)
        return web.Response(text="private ui")
    async def socket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                await ws.send_str(message.data)
        return ws
    backend.router.add_get("/", protected)
    backend.router.add_get("/socket", socket)
    async with TestServer(backend) as server:
        launcher = Launcher(Path("/unused-in-this-test"))
        launcher.state = "ready"
        launcher.backend = str(server.make_url("/")).rstrip("/")
        async with ClientSession() as session:
            launcher.session = session
            async with TestClient(TestServer(launcher.application())) as client:
                assert (await client.get("/")).status == 401
                response = await client.get("/", headers={"Authorization": "Bearer fixture"})
                assert await response.text() == "private ui"
                async with client.ws_connect("/socket") as ws:
                    await ws.send_str("a streamed chat reply")
                    assert (await ws.receive()).data == "a streamed chat reply"
