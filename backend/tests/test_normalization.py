import pytest

from backend.app.normalization import (
    CochlContractError,
    normalize_cochl_result,
    normalize_sound_events,
)


def test_normalizes_sound_events_from_integrated_api_result():
    raw = {
        "sound_event_detection": {
            "status": "success",
            "results": [
                {
                    "start_time_sec": 0.0,
                    "end_time_sec": 1.0,
                    "classes": [
                        {"class": "Speech", "confidence": 0.94},
                        {"class": "Male_speech", "confidence": 0.81},
                    ],
                }
            ],
        }
    }

    result = normalize_cochl_result(
        raw,
        duration_sec=None,
        content_type="audio/ogg",
        services_used=["sound_event_detection"],
        processing_time_ms=123,
    )

    assert result.sound_events[0].label == "Speech"
    assert result.sound_events[0].confidence == 0.94
    assert result.usage.processing_time_ms == 123


def test_preserves_zero_confidence_sound_event():
    raw = {
        "sound_event_detection": {
            "results": [
                {
                    "start_time_sec": 0.0,
                    "end_time_sec": 0.5,
                    "classes": [{"class": "Silence", "confidence": 0.0}],
                }
            ],
        }
    }

    result = normalize_cochl_result(
        raw,
        duration_sec=None,
        content_type="audio/ogg",
        services_used=["sound_event_detection"],
        processing_time_ms=1,
    )

    assert result.sound_events[0].confidence == 0.0


def test_missing_requested_service_is_a_contract_error():
    with pytest.raises(CochlContractError, match="missing requested service"):
        normalize_cochl_result(
            {},
            duration_sec=None,
            content_type="audio/ogg",
            services_used=["sound_event_detection"],
            processing_time_ms=1,
        )


def test_unrequested_services_remain_optional():
    result = normalize_cochl_result(
        {"sound_event_detection": {"status": "success", "results": []}},
        duration_sec=None,
        content_type="audio/ogg",
        services_used=["sound_event_detection"],
        processing_time_ms=1,
    )

    assert result.speech_segments == []
    assert result.audio_insights is None


def test_normalizes_string_boolean_values_without_truthiness_bug():
    result = normalize_cochl_result(
        {"audio_insights": {"result": {"contains_speech": "false"}}},
        duration_sec=None,
        content_type="audio/wav",
        services_used=["audio_insights"],
        processing_time_ms=1,
    )

    assert result.audio_insights is not None
    assert result.audio_insights.contains_speech is False


def test_replaces_non_finite_numeric_values():
    events = normalize_sound_events(
        {
            "sound_event_detection": {
                "results": [
                    {
                        "start_time_sec": "nan",
                        "end_time_sec": "inf",
                        "classes": [{"class": "Noise", "confidence": "nan"}],
                    }
                ]
            }
        }
    )

    assert events[0].start_time_sec == 0.0
    assert events[0].end_time_sec == 0.0
    assert events[0].confidence == 0.0


def test_normalizes_live_sound_events_with_window_offset():
    raw = {
        "sound_event_detection": {
            "results": [
                {
                    "start_time_sec": 0.25,
                    "end_time_sec": 1.25,
                    "classes": [{"class": "Keyboard", "confidence": 0.72}],
                }
            ],
        }
    }

    events = normalize_sound_events(raw, offset_sec=12.0)

    assert events[0].start_time_sec == 12.25
    assert events[0].end_time_sec == 13.25
    assert events[0].label == "Keyboard"
    assert events[0].confidence == 0.72


def test_normalizes_documented_sense_alias():
    events = normalize_sound_events(
        {
            "sense": {
                "status": "success",
                "results": [
                    {
                        "start_time_sec": 1,
                        "end_time_sec": 2,
                        "classes": [{"class": "Cough", "confidence": 0.8}],
                    }
                ],
            }
        }
    )

    assert [event.label for event in events] == ["Cough"]


def test_rejects_failed_or_malformed_requested_service():
    with pytest.raises(CochlContractError, match="failed"):
        normalize_sound_events(
            {"sound_event_detection": {"status": "failed", "message": "upstream"}}
        )

    with pytest.raises(CochlContractError, match="results field"):
        normalize_sound_events({"sound_event_detection": {"status": "success"}})

    with pytest.raises(CochlContractError, match="invalid results"):
        normalize_sound_events(
            {"sound_event_detection": {"status": "success", "results": {}}}
        )


def test_prefers_valid_provider_usage_metadata():
    result = normalize_cochl_result(
        {
            "sound_event_detection": {"status": "success", "results": []},
            "usage": {
                "audio_duration_sec": 2.5,
                "services_used": ["sound_event_detection"],
                "processing_time_ms": 99,
            },
        },
        duration_sec=3.0,
        content_type="audio/wav",
        services_used=["sound_event_detection"],
        processing_time_ms=120,
    )

    assert result.recording.duration_sec == 2.5
    assert result.usage.audio_duration_sec == 2.5
    assert result.usage.processing_time_ms == 99
