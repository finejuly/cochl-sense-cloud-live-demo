from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def parse_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


@dataclass(frozen=True)
class Settings:
    cochl_project_key: str | None = None
    enable_sound_event_detection: bool = True
    enable_speech_analysis: bool = False
    enable_audio_insights: bool = False
    max_upload_mb: int = 25

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        settings = cls(
            cochl_project_key=os.getenv("COCHL_PROJECT_KEY"),
            enable_sound_event_detection=parse_bool(
                os.getenv("COCHL_ENABLE_SOUND_EVENT_DETECTION"), default=True
            ),
            enable_speech_analysis=parse_bool(
                os.getenv("COCHL_ENABLE_SPEECH_ANALYSIS"), default=False
            ),
            enable_audio_insights=parse_bool(
                os.getenv("COCHL_ENABLE_AUDIO_INSIGHTS"), default=False
            ),
            max_upload_mb=int(os.getenv("MAX_UPLOAD_MB", "25")),
        )
        settings.validate_service_combination()
        return settings

    def validate_service_combination(self) -> None:
        if not self.enabled_services():
            raise ValueError("At least one Cochl service must be enabled.")
        if self.enable_audio_insights and not (
            self.enable_sound_event_detection and self.enable_speech_analysis
        ):
            raise ValueError(
                "Audio Insights requires both Sound Event Detection and Speech Analysis."
            )

    def enabled_services(self) -> list[str]:
        services: list[str] = []
        if self.enable_sound_event_detection:
            services.append("sound_event_detection")
        if self.enable_speech_analysis:
            services.append("speech_analysis")
        if self.enable_audio_insights:
            services.append("audio_insights")
        return services
