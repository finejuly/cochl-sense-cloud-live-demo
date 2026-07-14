from __future__ import annotations

import os
import re
from dataclasses import dataclass
from math import isfinite

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

GCS_UPLOADER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


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
    cochl_live_timeout_sec: float = 20.0
    cochl_recording_timeout_sec: float = 900.0
    cochl_socket_timeout_sec: float = 30.0
    cochl_live_transport_compression: bool = True
    cochl_live_persistent_connections: bool = True
    enable_sound_event_detection: bool = True
    enable_speech_analysis: bool = False
    enable_audio_insights: bool = False
    max_upload_mb: int = 25
    collection_enabled: bool = True
    collection_confidence_threshold: float = 0.5
    collection_min_segment_sec: float = 5.0
    collection_max_segment_sec: float = 20.0
    collection_silence_close_sec: float = 3.0
    collection_max_selected_segments: int = 600
    collection_max_selected_duration_sec: float = 3600.0
    collection_max_selected_audio_mb: int = 512
    collection_repeat_cooldown_sec: float = 600.0
    collection_max_quota_label_share: float = 0.30
    collection_exclude_label_keywords: tuple[str, ...] = (
        DEFAULT_COLLECTION_EXCLUDE_LABEL_KEYWORDS
    )
    gcs_project_id: str | None = None
    gcs_bucket_name: str | None = None
    gcs_object_prefix: str = ""
    gcs_uploader_id: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        settings = cls(
            cochl_project_key=os.getenv("COCHL_PROJECT_KEY"),
            cochl_live_timeout_sec=float(
                os.getenv("COCHL_LIVE_TIMEOUT_SEC", "20")
            ),
            cochl_recording_timeout_sec=float(
                os.getenv("COCHL_RECORDING_TIMEOUT_SEC", "900")
            ),
            cochl_socket_timeout_sec=float(
                os.getenv("COCHL_SOCKET_TIMEOUT_SEC", "30")
            ),
            cochl_live_transport_compression=parse_bool(
                os.getenv("COCHL_LIVE_TRANSPORT_COMPRESSION"), default=True
            ),
            cochl_live_persistent_connections=parse_bool(
                os.getenv("COCHL_LIVE_PERSISTENT_CONNECTIONS"), default=True
            ),
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
            collection_max_selected_segments=int(
                os.getenv("COCHL_COLLECTION_MAX_SELECTED_SEGMENTS", "600")
            ),
            collection_max_selected_duration_sec=float(
                os.getenv("COCHL_COLLECTION_MAX_SELECTED_DURATION_SEC", "3600")
            ),
            collection_max_selected_audio_mb=int(
                os.getenv("COCHL_COLLECTION_MAX_SELECTED_AUDIO_MB", "512")
            ),
            collection_repeat_cooldown_sec=float(
                os.getenv("COCHL_COLLECTION_REPEAT_COOLDOWN_SEC", "600")
            ),
            collection_max_quota_label_share=float(
                os.getenv("COCHL_COLLECTION_MAX_QUOTA_LABEL_SHARE", "0.30")
            ),
            collection_exclude_label_keywords=parse_keyword_list(
                os.getenv("COCHL_COLLECTION_EXCLUDE_LABEL_KEYWORDS"),
                DEFAULT_COLLECTION_EXCLUDE_LABEL_KEYWORDS,
            ),
            gcs_project_id=os.getenv("GCS_PROJECT_ID"),
            gcs_bucket_name=os.getenv("GCS_BUCKET_NAME"),
            gcs_object_prefix=os.getenv("GCS_OBJECT_PREFIX", ""),
            gcs_uploader_id=os.getenv("GCS_UPLOADER_ID"),
        )
        settings.validate_service_combination()
        settings.validate_timeouts()
        settings.validate_upload()
        settings.validate_collection()
        settings.validate_gcs()
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

    def validate_timeouts(self) -> None:
        timeouts = (
            self.cochl_live_timeout_sec,
            self.cochl_recording_timeout_sec,
            self.cochl_socket_timeout_sec,
        )
        if not all(isfinite(timeout) and timeout > 0 for timeout in timeouts):
            raise ValueError("Cochl timeouts must be finite and positive.")

    def validate_upload(self) -> None:
        if self.max_upload_mb <= 0:
            raise ValueError("Upload size limit must be positive.")

    def validate_collection(self) -> None:
        numeric_values = (
            self.collection_confidence_threshold,
            self.collection_min_segment_sec,
            self.collection_max_segment_sec,
            self.collection_silence_close_sec,
            self.collection_max_selected_segments,
            self.collection_max_selected_duration_sec,
            self.collection_max_selected_audio_mb,
            self.collection_repeat_cooldown_sec,
            self.collection_max_quota_label_share,
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
        if self.collection_max_selected_segments <= 0:
            raise ValueError("Collection max selected segments must be positive.")
        if self.collection_max_selected_duration_sec <= 0:
            raise ValueError("Collection max selected duration must be positive.")
        if self.collection_max_selected_audio_mb <= 0:
            raise ValueError("Collection max selected audio size must be positive.")
        if self.collection_repeat_cooldown_sec < 0:
            raise ValueError("Collection repeat cooldown cannot be negative.")
        if not 0 < self.collection_max_quota_label_share <= 1:
            raise ValueError(
                "Collection max quota label share must be greater than 0 and at most 1."
            )

    def validate_gcs(self) -> None:
        if self.gcs_object_prefix and not self.gcs_object_prefix.strip(" /"):
            raise ValueError("GCS object prefix cannot be empty.")
        if self.gcs_uploader_id and not GCS_UPLOADER_ID_PATTERN.fullmatch(
            self.gcs_uploader_id
        ):
            raise ValueError(
                "GCS uploader id must use letters, numbers, dots, underscores, or hyphens."
            )

    def require_gcs_upload(self) -> None:
        missing = [
            name
            for name, value in (
                ("GCS_PROJECT_ID", self.gcs_project_id),
                ("GCS_BUCKET_NAME", self.gcs_bucket_name),
                ("GCS_OBJECT_PREFIX", self.gcs_object_prefix),
            )
            if not value or not value.strip()
        ]
        if missing:
            raise ValueError(f"Missing GCS upload settings: {', '.join(missing)}")
        self.validate_gcs()

    def enabled_services(self) -> list[str]:
        services: list[str] = []
        if self.enable_sound_event_detection:
            services.append("sound_event_detection")
        if self.enable_speech_analysis:
            services.append("speech_analysis")
        if self.enable_audio_insights:
            services.append("audio_insights")
        return services
