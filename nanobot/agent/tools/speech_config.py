from pydantic import Field

from nanobot.config_base import Base


class SpeechToolConfig(Base):
    """Optional spoken replies using Microsoft's best-effort Edge TTS service."""

    enabled: bool = False
    voice: str = Field(default="en-US-AriaNeural", pattern=r"^[a-z]{2,3}-[A-Z]{2}-[A-Za-z]+Neural$")
    max_chars: int = Field(default=4000, ge=1, le=4000)
    use_proxy: bool = False
