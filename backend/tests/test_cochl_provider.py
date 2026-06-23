import cochl.sense
from cochl.sense.exception import CochlSenseException

from backend.app.cochl_provider import CochlProvider
from backend.app.config import Settings


def test_live_chunk_disables_caption_and_speaker_options(monkeypatch, tmp_path):
    captured_options = []

    class FakeIntegratedApi:
        def __init__(self, project_key):
            self.project_key = project_key

        def analyze_file(self, audio_file_path, options):
            captured_options.append(options)
            return {"job_id": "job-1"}

        def get_completed_result(self, job_id):
            return {"sound_event_detection": {"results": []}}

    monkeypatch.setattr(cochl.sense, "IntegratedApi", FakeIntegratedApi)
    provider = CochlProvider(Settings(cochl_project_key="test-key"))
    audio_path = tmp_path / "chunk.wav"
    audio_path.write_bytes(b"wav")

    provider.analyze_live_chunk(audio_path)

    assert len(captured_options) == 1
    options = captured_options[0]
    assert options.sound_event_detection is True
    assert options.speech_analysis is False
    assert options.audio_insights is False
    assert options.caption is False
    assert options.speaker_diarization is False
    assert options.speaker_profile is False


def test_provider_wraps_cochl_base_exception(monkeypatch, tmp_path):
    class FakeIntegratedApi:
        def __init__(self, project_key):
            self.project_key = project_key

        def analyze_file(self, audio_file_path, options):
            raise CochlSenseException("Bad Request")

    monkeypatch.setattr(cochl.sense, "IntegratedApi", FakeIntegratedApi)
    provider = CochlProvider(Settings(cochl_project_key="test-key"))
    audio_path = tmp_path / "chunk.wav"
    audio_path.write_bytes(b"wav")

    try:
        provider.analyze_live_chunk(audio_path)
    except RuntimeError as exc:
        assert "Bad Request" in str(exc)
    else:
        raise AssertionError("Expected live chunk analysis failure")


def test_provider_wraps_cochl_base_exception_from_completed_result(monkeypatch, tmp_path):
    class FakeIntegratedApi:
        def __init__(self, project_key):
            self.project_key = project_key

        def analyze_file(self, audio_file_path, options):
            return {"job_id": "job-1"}

        def get_completed_result(self, job_id):
            raise CochlSenseException("stream failed")

    monkeypatch.setattr(cochl.sense, "IntegratedApi", FakeIntegratedApi)
    provider = CochlProvider(Settings(cochl_project_key="test-key"))
    audio_path = tmp_path / "chunk.wav"
    audio_path.write_bytes(b"wav")

    try:
        provider.analyze_live_chunk(audio_path)
    except RuntimeError as exc:
        assert "stream failed" in str(exc)
    else:
        raise AssertionError("Expected completed result failure")
