from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backend.app.models import (
    AnalysisResponse,
    AudioInsights,
    RecordingMetadata,
    SoundEvent,
    SpeechSegment,
    UsageMetadata,
)


def normalize_cochl_result(
    raw_result: Mapping[str, Any],
    *,
    duration_sec: float | None,
    content_type: str,
    services_used: list[str],
    processing_time_ms: int,
) -> AnalysisResponse:
    return AnalysisResponse(
        recording=RecordingMetadata(
            duration_sec=duration_sec,
            content_type=content_type,
        ),
        sound_events=_normalize_sound_events(raw_result),
        speech_segments=_normalize_speech_segments(raw_result),
        audio_insights=_normalize_audio_insights(raw_result),
        usage=UsageMetadata(
            audio_duration_sec=duration_sec,
            services_used=services_used,
            processing_time_ms=processing_time_ms,
        ),
    )


def normalize_sound_events(raw_result: Mapping[str, Any], *, offset_sec: float = 0.0) -> list[SoundEvent]:
    return _normalize_sound_events(raw_result, offset_sec=offset_sec)


def _normalize_sound_events(raw_result: Mapping[str, Any], *, offset_sec: float = 0.0) -> list[SoundEvent]:
    service = _service_payload(raw_result, "sound_event_detection")
    chunks = _as_list(service.get("results") or service.get("events"))
    events: list[SoundEvent] = []

    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            continue
        start = _float(_first_present(chunk.get("start_time_sec"), chunk.get("start"), 0))
        end = _float(_first_present(chunk.get("end_time_sec"), chunk.get("end"), start))
        classes = _as_list(chunk.get("classes") or chunk.get("labels"))
        for item in classes:
            if not isinstance(item, Mapping):
                continue
            label = _first_present(item.get("class"), item.get("label"), item.get("name"))
            if not label:
                continue
            events.append(
                SoundEvent(
                    start_time_sec=start + offset_sec,
                    end_time_sec=end + offset_sec,
                    label=str(label),
                    confidence=_optional_float(
                        _first_present(item.get("confidence"), item.get("score"))
                    ),
                )
            )

    return events


def _normalize_speech_segments(raw_result: Mapping[str, Any]) -> list[SpeechSegment]:
    service = _service_payload(raw_result, "speech_analysis")
    segments = _as_list(service.get("segments") or service.get("results"))
    normalized: list[SpeechSegment] = []

    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        transcript = segment.get("transcript") or segment.get("text")
        if not transcript:
            continue
        start = _float(_first_present(segment.get("start_time_sec"), segment.get("start"), 0))
        end = _float(_first_present(segment.get("end_time_sec"), segment.get("end"), start))
        normalized.append(
            SpeechSegment(
                start_time_sec=start,
                end_time_sec=end,
                speaker=_optional_str(segment.get("speaker")),
                speaker_name=_optional_str(segment.get("speaker_name")),
                transcript=str(transcript),
            )
        )

    return normalized


def _normalize_audio_insights(raw_result: Mapping[str, Any]) -> AudioInsights | None:
    service = _service_payload(raw_result, "audio_insights")
    payload = service.get("result") if isinstance(service.get("result"), Mapping) else service
    if not payload:
        return None

    return AudioInsights(
        contains_speech=_optional_bool(payload.get("contains_speech")),
        detected_language=_optional_str(payload.get("detected_language")),
        primary_sound_environment=_optional_str(payload.get("primary_sound_environment")),
        situation_summary=_optional_str(
            payload.get("situation_summary") or payload.get("summary")
        ),
        notable_events=[str(item) for item in _as_list(payload.get("notable_events"))],
        keywords=[str(item) for item in _as_list(payload.get("keywords"))],
    )


def _service_payload(raw_result: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw_result.get(key)
    if isinstance(value, Mapping):
        return value
    return {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return _float(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None
