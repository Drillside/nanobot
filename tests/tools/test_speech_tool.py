from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.speech import SpeechTool
from nanobot.agent.tools.speech_config import SpeechToolConfig
from nanobot.config.schema import ToolsConfig
from nanobot.providers.speech import SpeechError, synthesize_speech


def test_speech_is_opt_in_and_has_bounded_input(tmp_path):
    ctx = ToolContext(config=ToolsConfig(), workspace=str(tmp_path))
    assert not SpeechTool.enabled(ctx)
    ctx.config.speech.enabled = True
    assert SpeechTool.enabled(ctx)
    tool = SpeechTool.create(ctx)
    assert tool.name == "speak"
    assert tool.validate_params({"text": "hello"}) == []
    assert tool.validate_params({"text": "x" * 4001})


async def test_speech_returns_a_deliverable_voice_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr("nanobot.agent.tools.speech.get_media_dir", lambda: tmp_path)
    async def generate(text, voice, target, *, proxy):
        target.write_bytes(b"OggS-test-audio")
    monkeypatch.setattr("nanobot.agent.tools.speech.synthesize_speech", generate)
    result = await SpeechTool(SpeechToolConfig(enabled=True)).execute("Hello")
    payload = json.loads(result)
    target = Path(payload["audio_path"])
    assert target.is_relative_to(tmp_path)
    assert target.suffix == ".ogg"
    assert target.read_bytes() == b"OggS-test-audio"
    assert "message" in payload["next_step"]


async def test_speech_failure_remains_an_actionable_text_error(tmp_path, monkeypatch):
    monkeypatch.setattr("nanobot.agent.tools.speech.get_media_dir", lambda: tmp_path)
    failure = AsyncMock(side_effect=SpeechError("Speech service is unavailable."))
    monkeypatch.setattr("nanobot.agent.tools.speech.synthesize_speech", failure)
    result = await SpeechTool(SpeechToolConfig(enabled=True)).execute("Hello")
    assert result.is_error
    assert "unavailable" in result
    assert not list(tmp_path.rglob("*.ogg"))


async def test_media_symlink_cannot_escape_the_private_directory(tmp_path, monkeypatch):
    media, outside = tmp_path / "media", tmp_path / "outside"
    media.mkdir()
    outside.mkdir()
    (media / "speech").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr("nanobot.agent.tools.speech.get_media_dir", lambda: media)
    generate = AsyncMock()
    monkeypatch.setattr("nanobot.agent.tools.speech.synthesize_speech", generate)
    result = await SpeechTool(SpeechToolConfig(enabled=True)).execute("Hello")
    assert result.is_error
    generate.assert_not_called()


async def test_speech_network_policy_is_applied_before_synthesis(tmp_path, monkeypatch):
    monkeypatch.setattr("nanobot.providers.speech.validate_url_target", lambda url: (False, "blocked"))
    with pytest.raises(SpeechError, match="blocked"):
        await synthesize_speech("Hello", "en-US-AriaNeural", tmp_path / "audio.ogg")
