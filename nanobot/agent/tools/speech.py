from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.agent.tools.speech_config import SpeechToolConfig
from nanobot.config.paths import get_media_dir
from nanobot.providers.speech import SpeechError, synthesize_speech

if TYPE_CHECKING:
    from nanobot.agent.tools.context import ToolContext


@tool_parameters(tool_parameters_schema(
    text=StringSchema("Text to speak, up to 4,000 characters.", min_length=1, max_length=4000),
    required=["text"],
))
class SpeechTool(Tool):
    """Create a voice note without changing the agent's normal text delivery."""

    config_key = "speech"

    @classmethod
    def config_cls(cls):
        return SpeechToolConfig

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return ctx.config.speech.enabled

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(ctx.config.speech, proxy=ctx.network.resolve_proxy(ctx.config.speech.use_proxy))

    def __init__(self, config: SpeechToolConfig, *, proxy: str | None = None):
        self.config = config
        self.proxy = proxy

    @property
    def name(self) -> str:
        return "speak"

    @property
    def description(self) -> str:
        return (
            "Create a spoken reply when the user requests audio or a voice note. "
            "Uses Microsoft's online neural voice service. Returns a local audio artifact. "
            "Deliver it using the message tool's media parameter."
        )

    async def execute(self, text: str, **kwargs: Any) -> str:  # pyright: ignore[reportIncompatibleMethodOverride]
        if not text.strip() or len(text) > self.config.max_chars:
            return ToolResult.error(f"Speech text must contain 1–{self.config.max_chars} characters.")
        media_root = get_media_dir().resolve()
        target_dir = (media_root / "speech").resolve()
        try:
            target_dir.relative_to(media_root)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / (uuid4().hex + ".ogg")
            await synthesize_speech(text, self.config.voice, target, proxy=self.proxy)
        except (SpeechError, OSError, ValueError) as error:
            message = str(error) if isinstance(error, SpeechError) else "Speech artifact could not be saved safely."
            return ToolResult.error(message)
        return json.dumps({
            "audio_path": str(target),
            "next_step": "Call message with this path in media to deliver the voice note. Keep the raw path internal.",
        })
