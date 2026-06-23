from backend.app.config import Settings, parse_bool


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
