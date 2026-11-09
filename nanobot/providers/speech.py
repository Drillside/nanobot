from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from nanobot.security.network import validate_url_target


class SpeechError(Exception):
    """A speech failure that contains no private text or credentials."""


async def synthesize_speech(text: str, voice: str, target: Path, *, proxy: str | None = None) -> None:
    """Produce an Opus voice note; the caller chooses a private, generated path."""
    import edge_tts

    allowed, _reason = validate_url_target("https://speech.platform.bing.com")
    if not allowed:
        raise SpeechError("Speech service is blocked by the network policy.")
    with tempfile.TemporaryDirectory(prefix="speech-", dir=target.parent) as temporary:
        source = Path(temporary) / "input.mp3"
        encoded = Path(temporary) / "voice.ogg"
        try:
            async with asyncio.timeout(60):
                await edge_tts.Communicate(text, voice, proxy=proxy).save(str(source))
                process = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(source), "-c:a", "libopus", "-b:a", "32k", str(encoded),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    return_code = await process.wait()
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                if return_code != 0 or not encoded.is_file() or encoded.stat().st_size == 0:
                    raise SpeechError("Speech audio could not be encoded.")
                encoded.replace(target)
        except asyncio.CancelledError:
            raise
        except SpeechError:
            raise
        except Exception:
            raise SpeechError("Speech service is unavailable. The assistant can still reply in text.") from None
