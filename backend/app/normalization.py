from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any

from backend.app.models import (
    AnalysisResponse,
    AudioInsights,
    RecordingMetadata,
    SoundEvent,
    SpeechSegment,
    UsageMetadata,
)


class CochlContractError(ValueError):
    """Raised when Cochl returns a response that cannot satisfy the request."""


_SERVICE_ALIASES: dict[str, tuple[str, ...]] = {
    # Cochl's Integration API documentation has used both names for this
    # payload. Accept both so an SDK/backend rollout cannot silently erase SED
    # results.
    "sound_event_detection": ("sound_event_detection", "sense"),
    "speech_analysis": ("speech_analysis",),
    "audio_insights": ("audio_insights",),
}


def normalize_cochl_result(
    raw_result: Mapping[str, Any],
    *,
    duration_sec: float | None,
    content_type: str,
    services_used: list[str],
    processing_time_ms: int,
) -> AnalysisResponse:
    _validate_requested_services(raw_result, services_used)
    usage = raw_result.get("usage")
    usage_payload = usage if isinstance(usage, Mapping) else {}

    return AnalysisResponse(
        recording=RecordingMetadata(
            duration_sec=_optional_finite_float(
                _first_present(
                    usage_payload.get("audio_duration_sec"),
                    usage_payload.get("duration_sec"),
                    duration_sec,
                )
            ),
            content_type=content_type,
        ),
        sound_events=_normalize_sound_events(raw_result),
        speech_segments=_normalize_speech_segments(raw_result),
        audio_insights=_normalize_audio_insights(raw_result),
        usage=UsageMetadata(
            audio_duration_sec=_optional_finite_float(
                _first_present(
                    usage_payload.get("audio_duration_sec"),
                    usage_payload.get("duration_sec"),
                    duration_sec,
                )
            ),
            services_used=_string_list(usage_payload.get("services_used")) or services_used,
            processing_time_ms=_non_negative_int(
                _first_present(usage_payload.get("processing_time_ms"), processing_time_ms)
            ),
        ),
    )


def normalize_sound_events(raw_result: Mapping[str, Any], *, offset_sec: float = 0.0) -> list[SoundEvent]:
    _validate_requested_services(raw_result, ["sound_event_detection"])
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
    for alias in _SERVICE_ALIASES.get(key, (key,)):
        value = raw_result.get(alias)
        if isinstance(value, Mapping):
            return value
    return {}


def _validate_requested_services(
    raw_result: Mapping[str, Any], services_used: list[str]
) -> None:
    for service_name in services_used:
        aliases = _SERVICE_ALIASES.get(service_name, (service_name,))
        present_key = next((key for key in aliases if key in raw_result), None)
        if present_key is None:
            raise CochlContractError(
                f"Cochl response is missing requested service '{service_name}'."
            )

        payload = raw_result[present_key]
        if not isinstance(payload, Mapping):
            raise CochlContractError(
                f"Cochl service '{service_name}' returned an invalid payload."
            )

        error = payload.get("error")
        if error:
            raise CochlContractError(
                f"Cochl service '{service_name}' failed: {error}"
            )

        status = payload.get("status")
        if isinstance(status, str) and status.strip().lower() in {
            "error",
            "fail",
            "failed",
            "failure",
        }:
            detail = payload.get("message") or payload.get("detail") or status
            raise CochlContractError(
                f"Cochl service '{service_name}' failed: {detail}"
            )

        if service_name in {"sound_event_detection", "speech_analysis"}:
            result_field = next(
                (
                    field
                    for field in ("results", "events", "segments")
                    if field in payload
                ),
                None,
            )
            if result_field is None:
                raise CochlContractError(
                    f"Cochl service '{service_name}' did not include a results field."
                )
            if not isinstance(payload[result_field], list):
                raise CochlContractError(
                    f"Cochl service '{service_name}' returned invalid results."
                )

        if service_name == "audio_insights":
            if "result" not in payload:
                raise CochlContractError(
                    "Cochl service 'audio_insights' did not include a result field."
                )
            if not isinstance(payload["result"], Mapping):
                raise CochlContractError(
                    "Cochl service 'audio_insights' returned an invalid result."
                )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if isfinite(parsed) else 0.0


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return _float(value)


def _optional_finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def _non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, parsed)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None
