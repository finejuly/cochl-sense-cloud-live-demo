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


def test_cochl_timeout_defaults_and_environment_overrides(monkeypatch):
    defaults = Settings(cochl_project_key="key")
    defaults.validate_timeouts()
    assert defaults.cochl_live_timeout_sec == 20.0
    assert defaults.cochl_recording_timeout_sec == 900.0
    assert defaults.cochl_socket_timeout_sec == 30.0
    assert defaults.cochl_live_transport_compression is True
    assert defaults.cochl_live_persistent_connections is True

    monkeypatch.setenv("COCHL_LIVE_TIMEOUT_SEC", "12.5")
    monkeypatch.setenv("COCHL_RECORDING_TIMEOUT_SEC", "120")
    monkeypatch.setenv("COCHL_SOCKET_TIMEOUT_SEC", "8")
    monkeypatch.setenv("COCHL_LIVE_TRANSPORT_COMPRESSION", "false")
    monkeypatch.setenv("COCHL_LIVE_PERSISTENT_CONNECTIONS", "false")
    configured = Settings.from_env()

    assert configured.cochl_live_timeout_sec == 12.5
    assert configured.cochl_recording_timeout_sec == 120.0
    assert configured.cochl_socket_timeout_sec == 8.0
    assert configured.cochl_live_transport_compression is False
    assert configured.cochl_live_persistent_connections is False


@pytest.mark.parametrize("value", [0.0, -1.0, float("inf"), float("nan")])
def test_cochl_timeouts_must_be_finite_and_positive(value):
    with pytest.raises(ValueError, match="timeouts"):
        Settings(cochl_project_key="key", cochl_live_timeout_sec=value).validate_timeouts()


def test_collection_defaults_are_enabled_with_privacy_keywords():
    settings = Settings(cochl_project_key="key")

    settings.validate_collection()

    assert settings.collection_enabled is True
    assert settings.collection_confidence_threshold == 0.5
    assert settings.collection_min_segment_sec == 5.0
    assert settings.collection_max_segment_sec == 20.0
    assert settings.collection_silence_close_sec == 3.0
    assert settings.collection_max_selected_segments == 600
    assert settings.collection_max_selected_duration_sec == 3600.0
    assert settings.collection_max_selected_audio_mb == 512
    assert settings.collection_repeat_cooldown_sec == 600.0
    assert settings.collection_max_quota_label_share == 0.30
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
        Settings(cochl_project_key="key", max_upload_mb=0).validate_upload()
    with pytest.raises(ValueError):
        Settings(
            cochl_project_key="key",
            collection_max_segment_sec=float("nan"),
        ).validate_collection()
    with pytest.raises(ValueError):
        Settings(
            cochl_project_key="key",
            collection_max_segment_sec=0,
        ).validate_collection()
    with pytest.raises(ValueError):
        Settings(
            cochl_project_key="key",
            collection_min_segment_sec=-1,
        ).validate_collection()
    with pytest.raises(ValueError):
        Settings(
            cochl_project_key="key",
            collection_min_segment_sec=25,
            collection_max_segment_sec=20,
        ).validate_collection()
    with pytest.raises(ValueError):
        Settings(
            cochl_project_key="key",
            collection_max_selected_segments=0,
        ).validate_collection()
    with pytest.raises(ValueError):
        Settings(
            cochl_project_key="key",
            collection_max_selected_duration_sec=float("inf"),
        ).validate_collection()
    with pytest.raises(ValueError):
        Settings(
            cochl_project_key="key",
            collection_max_selected_audio_mb=0,
        ).validate_collection()
    with pytest.raises(ValueError):
        Settings(
            cochl_project_key="key",
            collection_repeat_cooldown_sec=-1,
        ).validate_collection()
    with pytest.raises(ValueError):
        Settings(
            cochl_project_key="key",
            collection_max_quota_label_share=1.1,
        ).validate_collection()


def test_collection_curation_settings_are_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("COCHL_COLLECTION_MAX_SELECTED_SEGMENTS", "12")
    monkeypatch.setenv("COCHL_COLLECTION_MAX_SELECTED_DURATION_SEC", "34.5")
    monkeypatch.setenv("COCHL_COLLECTION_MAX_SELECTED_AUDIO_MB", "67")
    monkeypatch.setenv("COCHL_COLLECTION_REPEAT_COOLDOWN_SEC", "89.5")
    monkeypatch.setenv("COCHL_COLLECTION_MAX_QUOTA_LABEL_SHARE", "0.4")

    settings = Settings.from_env()

    assert settings.collection_max_selected_segments == 12
    assert settings.collection_max_selected_duration_sec == 34.5
    assert settings.collection_max_selected_audio_mb == 67
    assert settings.collection_repeat_cooldown_sec == 89.5
    assert settings.collection_max_quota_label_share == 0.4


def test_gcs_upload_configuration_requires_identity_and_valid_uploader_id():
    settings = Settings(
        cochl_project_key="key",
        gcs_project_id="test-project",
        gcs_bucket_name="test-bucket",
        gcs_object_prefix="test-prefix",
        gcs_uploader_id="workstation-01",
    )

    settings.require_gcs_upload()
    assert settings.gcs_object_prefix == "test-prefix"
    assert Settings(cochl_project_key="key").gcs_project_id is None
    assert Settings(cochl_project_key="key").gcs_bucket_name is None
    assert Settings(cochl_project_key="key").gcs_object_prefix == ""

    with pytest.raises(ValueError, match="Missing GCS upload settings"):
        Settings(
            cochl_project_key="key",
            gcs_project_id=None,
            gcs_bucket_name=None,
        ).require_gcs_upload()
    with pytest.raises(ValueError, match="GCS_OBJECT_PREFIX"):
        Settings(
            cochl_project_key="key",
            gcs_project_id="project",
            gcs_bucket_name="bucket",
        ).require_gcs_upload()
    with pytest.raises(ValueError, match="GCS uploader id"):
        Settings(
            cochl_project_key="key",
            gcs_project_id="project",
            gcs_bucket_name="bucket",
            gcs_object_prefix="prefix",
            gcs_uploader_id="invalid/id",
        ).require_gcs_upload()


def test_gcs_target_is_loaded_only_from_environment(monkeypatch):
    monkeypatch.setattr("backend.app.config.load_dotenv", lambda: None)
    for name in ("GCS_PROJECT_ID", "GCS_BUCKET_NAME", "GCS_OBJECT_PREFIX"):
        monkeypatch.delenv(name, raising=False)

    defaults = Settings.from_env()

    assert defaults.gcs_project_id is None
    assert defaults.gcs_bucket_name is None
    assert defaults.gcs_object_prefix == ""

    monkeypatch.setenv("GCS_PROJECT_ID", "test-project")
    monkeypatch.setenv("GCS_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("GCS_OBJECT_PREFIX", "test-prefix")

    configured = Settings.from_env()

    assert configured.gcs_project_id == "test-project"
    assert configured.gcs_bucket_name == "test-bucket"
    assert configured.gcs_object_prefix == "test-prefix"
