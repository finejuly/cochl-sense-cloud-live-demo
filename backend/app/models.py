from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

LiveChunkCollectionStatus = Literal[
    "collected",
    "discarded_silent",
    "discarded_speech",
    "discarded_late",
]


class RecordingMetadata(BaseModel):
    duration_sec: float | None = None
    content_type: str


class SoundEvent(BaseModel):
    start_time_sec: float
    end_time_sec: float
    label: str
    confidence: float | None = None


class SpeechSegment(BaseModel):
    start_time_sec: float
    end_time_sec: float
    speaker: str | None = None
    speaker_name: str | None = None
    transcript: str


class AudioInsights(BaseModel):
    contains_speech: bool | None = None
    detected_language: str | None = None
    primary_sound_environment: str | None = None
    situation_summary: str | None = None
    notable_events: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class UsageMetadata(BaseModel):
    audio_duration_sec: float | None = None
    services_used: list[str] = Field(default_factory=list)
    processing_time_ms: int


class AnalysisResponse(BaseModel):
    recording: RecordingMetadata
    sound_events: list[SoundEvent] = Field(default_factory=list)
    speech_segments: list[SpeechSegment] = Field(default_factory=list)
    audio_insights: AudioInsights | None = None
    usage: UsageMetadata


class LiveCurationProgress(BaseModel):
    candidate_segment_count: int = 0
    selected_segment_count: int = 0
    rejected_repetitive_count: int = 0
    rejected_class_balance_count: int = 0
    rejected_session_budget_count: int = 0
    invalid_audio_count: int = 0
    write_error_count: int = 0


class LiveChunkProcessingTimings(BaseModel):
    upload_ms: int
    provider_ms: int
    normalization_ms: int
    collection_ms: int
    total_ms: int


class LiveChunkAnalysisResponse(BaseModel):
    sequence_id: int
    window_start_sec: float
    window_end_sec: float
    sound_events: list[SoundEvent] = Field(default_factory=list)
    processing_time_ms: int
    timings: LiveChunkProcessingTimings
    collection_status: LiveChunkCollectionStatus | None = None
    curation_progress: LiveCurationProgress | None = None


class CollectedSegmentSummary(BaseModel):
    segment_index: int
    start_sec: float
    end_sec: float
    duration_sec: float
    event_count: int
    labels: list[str] = Field(default_factory=list)
    audio_filename: str
    metadata_filename: str
    primary_label: str | None = None
    quota_label: str | None = None
    selection_reason: str | None = None


class CurationAggregateMixin(BaseModel):
    candidate_segment_count: int = 0
    policy_selected_segment_count: int = 0
    policy_selected_duration_sec: float = 0.0
    policy_selected_audio_bytes: int = 0
    rejected_repetitive_count: int = 0
    rejected_class_balance_count: int = 0
    rejected_session_budget_count: int = 0
    invalid_audio_count: int = 0
    write_error_count: int = 0
    selected_label_segment_counts: dict[str, int] = Field(default_factory=dict)
    selected_quota_duration_sec: dict[str, float] = Field(default_factory=dict)
    policy_version: int | None = None


class LiveSessionEndResponse(CurationAggregateMixin):
    session_id: str
    session_name: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    segment_count: int
    total_collected_duration_sec: float
    kept_chunk_count: int
    discarded_silent_chunk_count: int
    discarded_speech_chunk_count: int
    segments: list[CollectedSegmentSummary] = Field(default_factory=list)


class GcsUploadStatus(BaseModel):
    status: Literal["uploaded"] = "uploaded"
    object_prefix: str
    snapshot_id: str
    uploaded_at: str


class CollectedSessionInfo(CurationAggregateMixin):
    session_id: str
    session_name: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    segment_count: int
    total_collected_duration_sec: float
    segments: list[CollectedSegmentSummary] = Field(default_factory=list)
    gcs_upload: GcsUploadStatus | None = None


class CollectedSessionsResponse(BaseModel):
    sessions: list[CollectedSessionInfo] = Field(default_factory=list)


class DeletionResponse(BaseModel):
    status: Literal["deleted"] = "deleted"


class RuntimeCapabilities(BaseModel):
    gcs: bool


class RuntimeConfigResponse(BaseModel):
    collection_confidence_threshold: float = Field(ge=0.0, le=1.0)
    api_token: str = Field(min_length=1)
    capabilities: RuntimeCapabilities


class ReadinessResponse(BaseModel):
    status: Literal["ready"] = "ready"
    capabilities: RuntimeCapabilities


class GcsSessionUploadResponse(BaseModel):
    status: Literal["uploaded"] = "uploaded"
    session_id: str
    object_prefix: str
    snapshot_id: str
    uploaded_file_count: int
    existing_file_count: int
    total_size_bytes: int
