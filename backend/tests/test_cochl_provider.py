import pytest
from cochl.sense import TimeoutException
from cochl.sense.exception import CochlSenseException

from backend.app.cochl_provider import (
    CochlProvider,
    CochlProviderTimeoutError,
    _get_completed_result_with_socket_timeout,
    _legacy_sound_event_results,
    _submit_with_socket_timeout,
)
from backend.app.config import Settings


def test_multi_service_recording_uses_only_supported_options(tmp_path):
    captured_options = []

    class FakeIntegratedApi:
        def __init__(self, project_key):
            self.project_key = project_key

        def analyze_file(self, audio_file_path, options):
            captured_options.append(options)
            return {"job_id": "job-1"}

        def get_completed_result(self, job_id):
            return {"sound_event_detection": {"results": []}}

    provider = CochlProvider(
        Settings(
            cochl_project_key="test-key",
            enable_sound_event_detection=True,
            enable_speech_analysis=True,
            enable_audio_insights=False,
        ),
        integrated_api_factory=FakeIntegratedApi,
    )
    audio_path = tmp_path / "chunk.wav"
    audio_path.write_bytes(b"wav")

    provider.analyze_file(audio_path)

    assert len(captured_options) == 1
    options = captured_options[0]
    assert options.sound_event_detection is True
    assert options.speech_analysis is True
    assert options.audio_insights is False
    assert options.caption is False
    assert options.speaker_diarization is True
    assert options.speaker_profile is True


@pytest.mark.parametrize("method_name", ["analyze_live_chunk", "analyze_file"])
def test_sed_only_requests_use_client_and_map_legacy_results(tmp_path, method_name):
    captured = {}

    class FakeEvents:
        @staticmethod
        def to_dict(config):
            captured["config"] = config
            return {
                "session_id": "session-1",
                "window_results": [
                    {
                        "start_time": 0,
                        "end_time": 2,
                        "sound_tags": [
                            {"name": "Baby_cry", "probability": 0.91},
                        ],
                    },
                ],
            }

    class FakeResult:
        events = FakeEvents()

    class FakeClient:
        def __init__(self, project_key):
            captured["project_key"] = project_key
            self.config = object()

        @staticmethod
        def predict(audio_file_path, timeout):
            captured.update(audio_file_path=audio_file_path, timeout=timeout)
            return FakeResult()

    audio_path = tmp_path / "chunk.wav"
    audio_path.write_bytes(b"wav")
    provider = CochlProvider(
        Settings(cochl_project_key="test-key", cochl_live_timeout_sec=7.5),
        live_client_factory=FakeClient,
    )

    result = getattr(provider, method_name)(audio_path)

    assert captured["project_key"] == "test-key"
    assert captured["audio_file_path"] == str(audio_path)
    assert captured["timeout"] == 7.5
    assert result == {
        "sound_event_detection": {
            "status": "success",
            "results": [
                {
                    "start_time_sec": 0,
                    "end_time_sec": 2,
                    "classes": [
                        {"class": "Baby_cry", "confidence": 0.91},
                    ],
                }
            ],
        }
    }


def test_live_chunk_preserves_legacy_timeout_type(tmp_path):
    class FakeClient:
        config = object()

        def __init__(self, project_key):
            self.project_key = project_key

        @staticmethod
        def predict(audio_file_path, timeout):
            raise TimeoutException("session-1", timeout)

    audio_path = tmp_path / "chunk.wav"
    audio_path.write_bytes(b"wav")
    provider = CochlProvider(
        Settings(cochl_project_key="test-key", cochl_live_timeout_sec=7.5),
        live_client_factory=FakeClient,
    )

    with pytest.raises(CochlProviderTimeoutError, match="timed out"):
        provider.analyze_live_chunk(audio_path)


def test_legacy_sound_event_results_rejects_malformed_payload():
    with pytest.raises(RuntimeError, match="window results"):
        _legacy_sound_event_results({"window_results": {}})

    with pytest.raises(RuntimeError, match="sound tags"):
        _legacy_sound_event_results(
            {"window_results": [{"start_time": 0, "end_time": 2}]}
        )


def test_provider_wraps_cochl_base_exception(tmp_path):
    class FakeIntegratedApi:
        def __init__(self, project_key):
            self.project_key = project_key

        def analyze_file(self, audio_file_path, options):
            raise CochlSenseException("Bad Request")

    provider = CochlProvider(
        Settings(cochl_project_key="test-key", enable_speech_analysis=True),
        integrated_api_factory=FakeIntegratedApi,
    )
    audio_path = tmp_path / "chunk.wav"
    audio_path.write_bytes(b"wav")

    try:
        provider.analyze_file(audio_path)
    except RuntimeError as exc:
        assert "Bad Request" in str(exc)
    else:
        raise AssertionError("Expected recording analysis failure")


def test_provider_wraps_cochl_base_exception_from_completed_result(tmp_path):
    class FakeIntegratedApi:
        def __init__(self, project_key):
            self.project_key = project_key

        def analyze_file(self, audio_file_path, options):
            return {"job_id": "job-1"}

        def get_completed_result(self, job_id):
            raise CochlSenseException("stream failed")

    provider = CochlProvider(
        Settings(cochl_project_key="test-key", enable_speech_analysis=True),
        integrated_api_factory=FakeIntegratedApi,
    )
    audio_path = tmp_path / "chunk.wav"
    audio_path.write_bytes(b"wav")

    try:
        provider.analyze_file(audio_path)
    except RuntimeError as exc:
        assert "stream failed" in str(exc)
    else:
        raise AssertionError("Expected completed result failure")


def test_provider_preserves_timeout_as_typed_error(tmp_path):
    class FakeIntegratedApi:
        def __init__(self, project_key):
            self.project_key = project_key

        def analyze_file(self, audio_file_path, options):
            raise TimeoutError("socket timed out")

    provider = CochlProvider(
        Settings(cochl_project_key="test-key", enable_speech_analysis=True),
        integrated_api_factory=FakeIntegratedApi,
    )
    audio_path = tmp_path / "chunk.wav"
    audio_path.write_bytes(b"wav")

    with pytest.raises(CochlProviderTimeoutError, match="socket timed out"):
        provider.analyze_file(audio_path)


@pytest.mark.parametrize("completed", [None, [], "not-json"])
def test_provider_rejects_invalid_completed_result(tmp_path, completed):
    class FakeIntegratedApi:
        def __init__(self, project_key):
            self.project_key = project_key

        def analyze_file(self, audio_file_path, options):
            return {"job_id": "job-1"}

        def get_completed_result(self, job_id):
            return completed

    provider = CochlProvider(
        Settings(cochl_project_key="test-key", enable_speech_analysis=True),
        integrated_api_factory=FakeIntegratedApi,
    )
    audio_path = tmp_path / "chunk.wav"
    audio_path.write_bytes(b"wav")

    with pytest.raises(RuntimeError, match="invalid response"):
        provider.analyze_file(audio_path)


def test_provider_rejects_submission_without_job_id(tmp_path):
    class FakeIntegratedApi:
        def __init__(self, project_key):
            self.project_key = project_key

        def analyze_file(self, audio_file_path, options):
            return {"status": "queued"}

    provider = CochlProvider(
        Settings(cochl_project_key="test-key", enable_speech_analysis=True),
        integrated_api_factory=FakeIntegratedApi,
    )
    audio_path = tmp_path / "chunk.wav"
    audio_path.write_bytes(b"wav")

    with pytest.raises(RuntimeError, match="job id"):
        provider.analyze_file(audio_path)


def test_submission_passes_socket_timeout_to_sdk_transport(monkeypatch, tmp_path):
    captured = {}

    class FakeApi:
        _host = "https://example.invalid/api/v1"
        _project_key = "project-key"

        @staticmethod
        def _result(response):
            return response

    class FakeOptions:
        enabled = True

        def __init__(self):
            self.enabled = True

    def fake_post(url, data, **kwargs):
        captured.update(url=url, data=data, kwargs=kwargs)
        return {"job_id": "job-1"}

    monkeypatch.setattr("cochl.sense.http_request.HttpRequest.post", fake_post)
    audio_path = tmp_path / "clip.wav"
    audio_path.write_bytes(b"wav")

    result = _submit_with_socket_timeout(FakeApi(), audio_path, FakeOptions(), 7.5)

    assert result == {"job_id": "job-1"}
    assert captured["kwargs"]["timeout"] == 7.5
    assert captured["kwargs"]["headers"] == {"X-Api-Key": "project-key"}


def test_completed_stream_passes_socket_timeout_and_requires_completion(monkeypatch):
    captured = {}

    class FakeApi:
        @staticmethod
        def create_event_stream_request(job_id):
            return f"request:{job_id}"

    class FakeResponse:
        def __enter__(self):
            return iter(
                [
                    b"event: completed\n",
                    b'data: {"sound_event_detection": {"results": []}}\n',
                ]
            )

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout):
        captured.update(request=request, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr("backend.app.cochl_provider.urlopen", fake_urlopen)

    result = _get_completed_result_with_socket_timeout(FakeApi(), "job-1", 4.0)

    assert result == {"sound_event_detection": {"results": []}}
    assert captured == {"request": "request:job-1", "timeout": 4.0}
