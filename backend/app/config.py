from __future__ import annotations

import os
from math import isfinite
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


DEFAULT_COLLECTION_EXCLUDE_LABEL_KEYWORDS = (
    "speech",
    "whisper",
    "sing",
    "conversation",
    "narration",
    "talk",
)


def parse_keyword_list(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    keywords = tuple(
        keyword.strip().lower() for keyword in value.split(",") if keyword.strip()
    )
    return keywords or default


@dataclass(frozen=True)
class Settings:
    cochl_project_key: str | None = None
    enable_sound_event_detection: bool = True
    enable_speech_analysis: bool = False
    enable_audio_insights: bool = False
    max_upload_mb: int = 25
    collection_enabled: bool = True
    collection_confidence_threshold: float = 0.5
    collection_min_segment_sec: float = 5.0
    collection_max_segment_sec: float = 20.0
    collection_silence_close_sec: float = 3.0
    collection_exclude_label_keywords: tuple[str, ...] = (
        DEFAULT_COLLECTION_EXCLUDE_LABEL_KEYWORDS
    )

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
            collection_enabled=parse_bool(
                os.getenv("COCHL_COLLECTION_ENABLED"), default=True
            ),
            collection_confidence_threshold=float(
                os.getenv("COCHL_COLLECTION_CONFIDENCE_THRESHOLD", "0.5")
            ),
            collection_min_segment_sec=float(
                os.getenv("COCHL_COLLECTION_MIN_SEGMENT_SEC", "5")
            ),
            collection_max_segment_sec=float(
                os.getenv("COCHL_COLLECTION_MAX_SEGMENT_SEC", "20")
            ),
            collection_silence_close_sec=float(
                os.getenv("COCHL_COLLECTION_SILENCE_CLOSE_SEC", "3")
            ),
            collection_exclude_label_keywords=parse_keyword_list(
                os.getenv("COCHL_COLLECTION_EXCLUDE_LABEL_KEYWORDS"),
                DEFAULT_COLLECTION_EXCLUDE_LABEL_KEYWORDS,
            ),
        )
        settings.validate_service_combination()
        settings.validate_upload()
        settings.validate_collection()
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

    def validate_upload(self) -> None:
        if self.max_upload_mb <= 0:
            raise ValueError("Upload size limit must be positive.")

    def validate_collection(self) -> None:
        numeric_values = (
            self.collection_confidence_threshold,
            self.collection_min_segment_sec,
            self.collection_max_segment_sec,
            self.collection_silence_close_sec,
        )
        if not all(isfinite(value) for value in numeric_values):
            raise ValueError("Collection numeric settings must be finite.")
        if not 0.0 <= self.collection_confidence_threshold <= 1.0:
            raise ValueError("Collection confidence threshold must be between 0 and 1.")
        if self.collection_max_segment_sec <= 0:
            raise ValueError("Collection max segment length must be positive.")
        if self.collection_min_segment_sec < 0:
            raise ValueError("Collection min segment length cannot be negative.")
        if self.collection_min_segment_sec > self.collection_max_segment_sec:
            raise ValueError(
                "Collection min segment length cannot exceed the max segment length."
            )
        if self.collection_silence_close_sec <= 0:
            raise ValueError("Collection silence close time must be positive.")

    def enabled_services(self) -> list[str]:
        services: list[str] = []
        if self.enable_sound_event_detection:
            services.append("sound_event_detection")
        if self.enable_speech_analysis:
            services.append("speech_analysis")
        if self.enable_audio_insights:
            services.append("audio_insights")
        return services
