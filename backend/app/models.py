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


class LiveChunkAnalysisResponse(BaseModel):
    sequence_id: int
    window_start_sec: float
    window_end_sec: float
    sound_events: list[SoundEvent] = Field(default_factory=list)
    processing_time_ms: int
    collection_status: LiveChunkCollectionStatus | None = None


class CollectedSegmentSummary(BaseModel):
    segment_index: int
    start_sec: float
    end_sec: float
    duration_sec: float
    event_count: int
    labels: list[str] = Field(default_factory=list)
    audio_filename: str
    metadata_filename: str


class LiveSessionEndResponse(BaseModel):
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


class CollectedSessionInfo(BaseModel):
    session_id: str
    session_name: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    segment_count: int
    total_collected_duration_sec: float
    segments: list[CollectedSegmentSummary] = Field(default_factory=list)


class CollectedSessionsResponse(BaseModel):
    sessions: list[CollectedSessionInfo] = Field(default_factory=list)


class DeletionResponse(BaseModel):
    status: str = "deleted"
