import pytest

from backend.app.config import (
    DEFAULT_COLLECTION_EXCLUDE_LABEL_KEYWORDS,
    Settings,
    parse_bool,
    parse_keyword_list,
)


def test_parse_bool_accepts_common_truthy_values():
    assert parse_bool("true") is True
    assert parse_bool("1") is True
    assert parse_bool("yes") is True


def test_parse_bool_accepts_common_falsey_values():
    assert parse_bool("false") is False
    assert parse_bool("0") is False
    assert parse_bool("no") is False


def test_audio_insights_requires_both_base_services():
    settings = Settings(
        cochl_project_key="key",
        enable_sound_event_detection=True,
        enable_speech_analysis=False,
        enable_audio_insights=True,
    )

    try:
        settings.validate_service_combination()
    except ValueError as exc:
        assert "Audio Insights" in str(exc)
    else:
        raise AssertionError("Expected invalid service combination")


def test_sound_event_detection_only_is_valid():
    settings = Settings(cochl_project_key="key")

    settings.validate_service_combination()

    assert settings.enabled_services() == ["sound_event_detection"]


def test_collection_defaults_are_enabled_with_privacy_keywords():
    settings = Settings(cochl_project_key="key")

    settings.validate_collection()

    assert settings.collection_enabled is True
    assert settings.collection_confidence_threshold == 0.5
    assert settings.collection_max_segment_sec == 20.0
    assert "speech" in settings.collection_exclude_label_keywords


def test_parse_keyword_list_normalizes_and_falls_back():
    assert parse_keyword_list(" Speech , WHISPER ", ("x",)) == ("speech", "whisper")
    assert parse_keyword_list(None, ("x",)) == ("x",)
    assert parse_keyword_list(" , ", ("x",)) == ("x",)
    assert parse_keyword_list(
        None, DEFAULT_COLLECTION_EXCLUDE_LABEL_KEYWORDS
    ) == DEFAULT_COLLECTION_EXCLUDE_LABEL_KEYWORDS


def test_collection_validation_rejects_out_of_range_values():
    with pytest.raises(ValueError):
        Settings(
            cochl_project_key="key",
            collection_confidence_threshold=1.5,
        ).validate_collection()
    with pytest.raises(ValueError):
        Settings(
            cochl_project_key="key",
            collection_max_segment_sec=0,
        ).validate_collection()
