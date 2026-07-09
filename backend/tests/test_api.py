import asyncio
import io
import json
import struct
import threading
import time
import wave
from concurrent.futures import Future

import httpx
from anyio import CapacityLimiter
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import (
    LIVE_CONVERSION_MAX_PENDING,
    app,
    create_app,
    get_settings,
    schedule_live_chunk_conversion,
)


class FakeProvider:
    def __init__(self, settings):
        self.settings = settings

    def analyze_file(self, path):
        return {
            "sound_event_detection": {
                "status": "success",
                "results": [
                    {
                        "start_time_sec": 0.0,
                        "end_time_sec": 1.0,
                        "classes": [{"class": "Speech", "confidence": 0.9}],
                    }
                ],
            }
        }


def override_settings():
    return Settings(cochl_project_key="test-key")


def override_settings_without_collection():
    return Settings(cochl_project_key="test-key", collection_enabled=False)


def make_wav_bytes(start_sec: float, end_sec: float, framerate: int = 100) -> bytes:
    start_frame = round(start_sec * framerate)
    frame_count = round((end_sec - start_sec) * framerate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(framerate)
        writer.writeframes(
            struct.pack(f"<{frame_count}h", *range(start_frame, start_frame + frame_count))
        )
    return buffer.getvalue()


def test_health_returns_ready():
    client = TestClient(app)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_analyze_recording_returns_normalized_result():
    app.dependency_overrides[get_settings] = override_settings
    app.state.provider_factory = lambda settings: FakeProvider(settings)
    client = TestClient(app)

    response = client.post(
        "/api/analyze-recording",
        files={"file": ("clip.ogg", b"fake-audio", "audio/ogg")},
    )

    app.dependency_overrides.clear()
    app.state.provider_factory = None

    assert response.status_code == 200
    assert response.json()["sound_events"][0]["label"] == "Speech"


def test_empty_uploads_return_bad_request(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", tmp_path / "recordings")
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: FakeProvider(settings)
    client = TestClient(created_app)

    recording_response = client.post(
        "/api/analyze-recording",
        files={"file": ("empty.wav", b"", "audio/wav")},
    )
    live_response = client.post(
        "/api/analyze-live-chunk",
        data={
            "session_id": "empty-test",
            "sequence_id": "1",
            "window_start_sec": "0",
            "window_end_sec": "2",
        },
        files={"file": ("empty.wav", b"", "audio/wav")},
    )

    assert recording_response.status_code == 400
    assert live_response.status_code == 400
    assert not any((tmp_path / "recordings").rglob("*.wav"))


def test_recording_provider_does_not_block_other_requests(tmp_path, monkeypatch):
    delay_sec = 0.3

    class SlowRecordingProvider(FakeProvider):
        def analyze_file(self, path):
            time.sleep(delay_sec)
            return {}

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", tmp_path / "recordings")
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: SlowRecordingProvider(settings)

    async def run_requests():
        transport = httpx.ASGITransport(app=created_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            started_at = time.perf_counter()
            recording_request = asyncio.create_task(
                client.post(
                    "/api/analyze-recording",
                    files={"file": ("clip.wav", b"wav-audio", "audio/wav")},
                )
            )
            await asyncio.sleep(0.02)
            health_response = await client.get("/api/health")
            health_elapsed_sec = time.perf_counter() - started_at
            recording_response = await recording_request
        return health_elapsed_sec, health_response, recording_response

    health_elapsed_sec, health_response, recording_response = asyncio.run(run_requests())

    assert health_response.status_code == 200
    assert health_elapsed_sec < delay_sec / 2
    assert recording_response.status_code == 200


def test_failed_live_analysis_removes_staged_audio_when_collection_is_enabled(
    tmp_path,
    monkeypatch,
):
    recordings_dir = tmp_path / "recordings"

    class FailingProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            raise RuntimeError("provider failed")

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: FailingProvider(settings)
    client = TestClient(created_app)

    response = client.post(
        "/api/analyze-live-chunk",
        data={
            "session_id": "failure-cleanup-test",
            "sequence_id": "1",
            "window_start_sec": "0",
            "window_end_sec": "2",
        },
        files={"file": ("chunk.wav", b"wav-audio", "audio/wav")},
    )

    assert response.status_code == 502
    assert not any(recordings_dir.rglob("*.wav"))


def test_analyze_recording_uses_provider_factory_from_created_app():
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: FakeProvider(settings)
    client = TestClient(created_app)

    try:
        response = client.post(
            "/api/analyze-recording",
            files={"file": ("clip.ogg", b"fake-audio", "audio/ogg")},
        )
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None

    assert response.status_code == 200
    assert response.json()["sound_events"][0]["label"] == "Speech"


def test_analyze_recording_preserves_upload_and_converted_audio(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    provider_paths = []

    class CapturingProvider(FakeProvider):
        def analyze_file(self, path):
            provider_paths.append(path)
            return super().analyze_file(path)

    def fake_convert_to_wav(input_path, output_path):
        output_path.write_bytes(b"wav-audio")

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr("backend.app.audio.convert_to_wav", fake_convert_to_wav)
    app.dependency_overrides[get_settings] = override_settings
    app.state.provider_factory = lambda settings: CapturingProvider(settings)
    client = TestClient(app)

    try:
        response = client.post(
            "/api/analyze-recording",
            files={"file": ("clip.mp3", b"webm-audio", "audio/webm")},
        )
    finally:
        app.dependency_overrides.clear()
        app.state.provider_factory = None

    saved_files = sorted(path.name for path in recordings_dir.iterdir())
    assert response.status_code == 200
    assert saved_files == ["clip.wav", "clip.webm"]
    assert (recordings_dir / "clip.webm").read_bytes() == b"webm-audio"
    assert (recordings_dir / "clip.wav").read_bytes() == b"wav-audio"
    assert provider_paths == [recordings_dir / "clip.wav"]


def test_analyze_live_chunk_returns_offset_sound_events_and_preserves_debug_audio(
    tmp_path,
    monkeypatch,
):
    recordings_dir = tmp_path / "recordings"
    provider_paths = []
    conversion_calls = []

    class CapturingProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            provider_paths.append(path)
            return {
                "sound_event_detection": {
                    "status": "success",
                    "results": [
                        {
                            "start_time_sec": 0.25,
                            "end_time_sec": 1.25,
                            "classes": [{"class": "Keyboard", "confidence": 0.72}],
                        }
                    ],
                }
            }

    def fake_convert_live_chunk_to_mp3(wav_path):
        conversion_calls.append(wav_path)
        wav_path.with_suffix(".mp3").write_bytes(b"mp3-audio")
        wav_path.unlink()

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(
        "backend.app.main.convert_live_chunk_to_mp3",
        fake_convert_live_chunk_to_mp3,
        raising=False,
    )
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings_without_collection
    created_app.state.provider_factory = lambda settings: CapturingProvider(settings)
    client = TestClient(created_app)

    try:
        response = client.post(
            "/api/analyze-live-chunk",
            data={
                "session_id": "session-abc",
                "sequence_id": "12",
                "window_start_sec": "12.0",
                "window_end_sec": "14.0",
            },
            files={"file": ("chunk.wav", b"wav-audio", "audio/wav")},
        )
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None

    expected_wav_path = recordings_dir / "live" / "session-abc" / "chunk-000012-12.000-14.000.wav"
    expected_mp3_path = expected_wav_path.with_suffix(".mp3")
    body = response.json()
    assert response.status_code == 200
    assert body["sequence_id"] == 12
    assert body["window_start_sec"] == 12.0
    assert body["window_end_sec"] == 14.0
    assert body["sound_events"] == [
        {
            "start_time_sec": 12.25,
            "end_time_sec": 13.25,
            "label": "Keyboard",
            "confidence": 0.72,
        }
    ]
    assert provider_paths == [expected_wav_path]

    deadline = time.perf_counter() + 1.0
    while time.perf_counter() < deadline and not expected_mp3_path.exists():
        time.sleep(0.01)

    assert conversion_calls == [expected_wav_path]
    assert expected_mp3_path.read_bytes() == b"mp3-audio"
    assert not expected_wav_path.exists()


def test_analyze_live_chunk_does_not_wait_for_debug_audio_conversion(
    tmp_path,
    monkeypatch,
):
    recordings_dir = tmp_path / "recordings"
    conversion_calls = []

    class CapturingProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            return {"sound_event_detection": {"status": "success", "results": []}}

    def slow_convert_live_chunk_to_mp3(wav_path):
        conversion_calls.append(wav_path)
        time.sleep(0.5)
        wav_path.with_suffix(".mp3").write_bytes(b"mp3-audio")
        wav_path.unlink()

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(
        "backend.app.main.convert_live_chunk_to_mp3",
        slow_convert_live_chunk_to_mp3,
        raising=False,
    )
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings_without_collection
    created_app.state.provider_factory = lambda settings: CapturingProvider(settings)
    client = TestClient(created_app)

    try:
        started_at = time.perf_counter()
        response = client.post(
            "/api/analyze-live-chunk",
            data={
                "session_id": "conversion-response-test",
                "sequence_id": "1",
                "window_start_sec": "0.0",
                "window_end_sec": "2.0",
            },
            files={"file": ("chunk.wav", b"wav-audio", "audio/wav")},
        )
        elapsed_sec = time.perf_counter() - started_at
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None

    expected_wav_path = (
        recordings_dir
        / "live"
        / "conversion-response-test"
        / "chunk-000001-0.000-2.000.wav"
    )
    expected_mp3_path = expected_wav_path.with_suffix(".mp3")
    assert response.status_code == 200
    assert elapsed_sec < 0.25

    deadline = time.perf_counter() + 1.5
    while time.perf_counter() < deadline and not expected_mp3_path.exists():
        time.sleep(0.01)

    assert conversion_calls == [expected_wav_path]
    assert expected_mp3_path.read_bytes() == b"mp3-audio"
    assert not expected_wav_path.exists()


def test_schedule_live_chunk_conversion_skips_when_backlog_is_full(tmp_path):
    created_app = create_app(frontend_dist=None)

    class FailingExecutor:
        def submit(self, *args, **kwargs):
            raise AssertionError("conversion should not be submitted when backlog is full")

    created_app.state.live_conversion_executor = FailingExecutor()
    created_app.state.live_conversion_futures = {
        Future() for _ in range(LIVE_CONVERSION_MAX_PENDING)
    }

    scheduled = schedule_live_chunk_conversion(created_app, tmp_path / "chunk.wav")

    assert scheduled is False
    assert len(created_app.state.live_conversion_futures) == LIVE_CONVERSION_MAX_PENDING


def test_analyze_live_chunk_rejects_ambiguous_session_id(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    provider_paths = []

    class CapturingProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            provider_paths.append(path)
            return {"sound_event_detection": {"status": "success", "results": []}}

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(
        "backend.app.main.convert_live_chunk_to_mp3",
        lambda wav_path: None,
        raising=False,
    )
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: CapturingProvider(settings)
    client = TestClient(created_app)

    try:
        response = client.post(
            "/api/analyze-live-chunk",
            data={
                "session_id": "../bad session",
                "sequence_id": "1",
                "window_start_sec": "0.0",
                "window_end_sec": "2.0",
            },
            files={"file": ("chunk.wav", b"wav-audio", "audio/wav")},
        )
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None

    assert response.status_code == 422
    assert provider_paths == []
    assert not recordings_dir.exists()


def test_analyze_live_chunk_rejects_invalid_window_metadata(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: FakeProvider(settings)
    client = TestClient(created_app)

    response = client.post(
        "/api/analyze-live-chunk",
        data={
            "session_id": "metadata-test",
            "sequence_id": "0",
            "window_start_sec": "2",
            "window_end_sec": "1",
        },
        files={"file": ("chunk.wav", b"wav-audio", "audio/wav")},
    )

    assert response.status_code == 422
    assert not recordings_dir.exists()


def test_oversized_upload_is_streamed_and_partial_file_is_removed(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = lambda: Settings(
        cochl_project_key="test-key",
        max_upload_mb=1,
    )
    created_app.state.provider_factory = lambda settings: FakeProvider(settings)
    client = TestClient(created_app)

    response = client.post(
        "/api/analyze-recording",
        files={"file": ("large.wav", b"x" * (1024 * 1024 + 1), "audio/wav")},
    )

    assert response.status_code == 413
    assert not any(recordings_dir.iterdir())


def test_analyze_live_chunk_handles_concurrent_provider_calls(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    delay_sec = 0.2
    request_count = 4

    class SlowProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            time.sleep(delay_sec)
            return {"sound_event_detection": {"status": "success", "results": []}}

    async def post_chunk(client, sequence_id):
        return await client.post(
            "/api/analyze-live-chunk",
            data={
                "session_id": "concurrency-test",
                "sequence_id": str(sequence_id),
                "window_start_sec": f"{sequence_id - 1}.0",
                "window_end_sec": f"{sequence_id + 1}.0",
            },
            files={"file": ("chunk.wav", b"wav-audio", "audio/wav")},
        )

    async def run_requests():
        transport = httpx.ASGITransport(app=created_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            started_at = time.perf_counter()
            responses = await asyncio.gather(
                *(post_chunk(client, sequence_id) for sequence_id in range(1, request_count + 1))
            )
            return time.perf_counter() - started_at, responses

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(
        "backend.app.main.convert_live_chunk_to_mp3",
        lambda wav_path: None,
        raising=False,
    )
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: SlowProvider(settings)

    try:
        elapsed_sec, responses = asyncio.run(run_requests())
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None

    assert [response.status_code for response in responses] == [200] * request_count
    assert elapsed_sec < delay_sec * 2


def test_analyze_live_chunk_limits_server_side_provider_concurrency(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    delay_sec = 0.1
    request_count = 5
    active_calls = 0
    max_active_calls = 0
    lock = threading.Lock()

    class TrackingProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            nonlocal active_calls, max_active_calls
            with lock:
                active_calls += 1
                max_active_calls = max(max_active_calls, active_calls)
            try:
                time.sleep(delay_sec)
                return {"sound_event_detection": {"status": "success", "results": []}}
            finally:
                with lock:
                    active_calls -= 1

    async def post_chunk(client, sequence_id):
        return await client.post(
            "/api/analyze-live-chunk",
            data={
                "session_id": "concurrency-limit-test",
                "sequence_id": str(sequence_id),
                "window_start_sec": f"{sequence_id - 1}.0",
                "window_end_sec": f"{sequence_id + 1}.0",
            },
            files={"file": ("chunk.wav", b"wav-audio", "audio/wav")},
        )

    async def run_requests():
        transport = httpx.ASGITransport(app=created_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await asyncio.gather(
                *(post_chunk(client, sequence_id) for sequence_id in range(1, request_count + 1))
            )

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(
        "backend.app.main.convert_live_chunk_to_mp3",
        lambda wav_path: None,
        raising=False,
    )
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: TrackingProvider(settings)
    created_app.state.live_provider_limiter = CapacityLimiter(2)

    try:
        responses = asyncio.run(run_requests())
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None

    assert [response.status_code for response in responses] == [200] * request_count
    assert max_active_calls == 2


def post_live_chunk(client, session_id, sequence_id, start_sec, end_sec):
    return client.post(
        "/api/analyze-live-chunk",
        data={
            "session_id": session_id,
            "sequence_id": str(sequence_id),
            "window_start_sec": f"{start_sec:.1f}",
            "window_end_sec": f"{end_sec:.1f}",
        },
        files={
            "file": ("chunk.wav", make_wav_bytes(start_sec, end_sec), "audio/wav"),
        },
    )


def test_analyze_live_chunk_collects_meaningful_audio_into_segments(
    tmp_path,
    monkeypatch,
):
    recordings_dir = tmp_path / "recordings"

    class DetectingProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            return {
                "sound_event_detection": {
                    "status": "success",
                    "results": [
                        {
                            "start_time_sec": 0.25,
                            "end_time_sec": 1.25,
                            "classes": [{"class": "Keyboard", "confidence": 0.72}],
                        }
                    ],
                }
            }

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(
        "backend.app.main.convert_live_chunk_to_mp3",
        lambda wav_path: None,
        raising=False,
    )
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: DetectingProvider(settings)
    client = TestClient(created_app)

    try:
        responses = [
            post_live_chunk(client, "collect-test", sequence_id, start, end)
            for sequence_id, (start, end) in enumerate(
                [(0, 2), (1, 3), (2, 4)], start=1
            )
        ]
        end_response = client.post(
            "/api/live-session/end",
            data={"session_id": "collect-test"},
        )
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert [response.json()["collection_status"] for response in responses] == [
        "collected",
        "collected",
        "collected",
    ]

    summary = end_response.json()
    assert end_response.status_code == 200
    assert summary["session_id"] == "collect-test"
    assert summary["segment_count"] == 1
    assert summary["kept_chunk_count"] == 3
    assert summary["total_collected_duration_sec"] == 4.0
    segment = summary["segments"][0]
    assert segment["labels"] == ["Keyboard"]

    collected_dir = recordings_dir / "collected" / "collect-test"
    assert (collected_dir / segment["audio_filename"]).exists()
    metadata = json.loads(
        (collected_dir / segment["metadata_filename"]).read_text("utf-8")
    )
    assert metadata["chunk_sequence_ids"] == [1, 2, 3]
    assert not (recordings_dir / "live" / "collect-test").exists()


def test_analyze_live_chunk_discards_speech_chunks_for_privacy(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: FakeProvider(settings)
    client = TestClient(created_app)

    class SpeechProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            return {
                "sound_event_detection": {
                    "status": "success",
                    "results": [
                        {
                            "start_time_sec": 0.0,
                            "end_time_sec": 1.0,
                            "classes": [{"class": "Male_speech", "confidence": 0.9}],
                        }
                    ],
                }
            }

    created_app.state.provider_factory = lambda settings: SpeechProvider(settings)

    try:
        chunk_response = post_live_chunk(client, "speech-test", 1, 0, 2)
        end_response = client.post(
            "/api/live-session/end",
            data={"session_id": "speech-test"},
        )
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None

    assert chunk_response.status_code == 200
    assert chunk_response.json()["collection_status"] == "discarded_speech"
    summary = end_response.json()
    assert summary["segment_count"] == 0
    assert summary["discarded_speech_chunk_count"] == 1
    assert not (recordings_dir / "live" / "speech-test").exists()
    assert not (recordings_dir / "collected" / "speech-test").exists()


def test_live_session_records_name_and_timestamps(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"

    class DetectingProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            return {
                "sound_event_detection": {
                    "status": "success",
                    "results": [
                        {
                            "start_time_sec": 0.0,
                            "end_time_sec": 1.0,
                            "classes": [{"class": "Knock", "confidence": 0.8}],
                        }
                    ],
                }
            }

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(
        "backend.app.main.convert_live_chunk_to_mp3",
        lambda wav_path: None,
        raising=False,
    )
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: DetectingProvider(settings)
    client = TestClient(created_app)

    try:
        chunk_response = client.post(
            "/api/analyze-live-chunk",
            data={
                "session_id": "named-test",
                "sequence_id": "1",
                "window_start_sec": "0.0",
                "window_end_sec": "2.0",
                "session_name": "  사무실 소음  ",
            },
            files={"file": ("chunk.wav", make_wav_bytes(0, 2), "audio/wav")},
        )
        end_response = client.post(
            "/api/live-session/end",
            data={"session_id": "named-test", "session_name": "사무실 소음"},
        )
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None

    assert chunk_response.status_code == 200
    summary = end_response.json()
    assert summary["session_name"] == "사무실 소음"
    assert summary["started_at"] is not None
    assert summary["ended_at"] is not None
    session_json = json.loads(
        (recordings_dir / "collected" / "named-test" / "session.json").read_text("utf-8")
    )
    assert session_json["session_name"] == "사무실 소음"


def test_collected_sessions_can_be_listed_served_and_deleted(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"

    class DetectingProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            return {
                "sound_event_detection": {
                    "status": "success",
                    "results": [
                        {
                            "start_time_sec": 0.0,
                            "end_time_sec": 1.0,
                            "classes": [{"class": "Knock", "confidence": 0.8}],
                        }
                    ],
                }
            }

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(
        "backend.app.main.convert_live_chunk_to_mp3",
        lambda wav_path: None,
        raising=False,
    )
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: DetectingProvider(settings)
    client = TestClient(created_app)

    try:
        post_live_chunk(client, "manage-test", 1, 0, 2)
        client.post(
            "/api/live-session/end",
            data={"session_id": "manage-test", "session_name": "관리 테스트"},
        )

        listing = client.get("/api/collected-sessions")
        sessions = listing.json()["sessions"]
        assert listing.status_code == 200
        assert len(sessions) == 1
        session = sessions[0]
        assert session["session_id"] == "manage-test"
        assert session["session_name"] == "관리 테스트"
        segment = session["segments"][0]

        audio_response = client.get(
            f"/api/collected-sessions/manage-test/files/{segment['audio_filename']}"
        )
        assert audio_response.status_code == 200
        assert audio_response.headers["content-type"].startswith("audio/")

        traversal_response = client.get(
            "/api/collected-sessions/..%2F..%2Fmanage-test/files/secret.wav"
        )
        assert traversal_response.status_code == 404

        segment_delete = client.delete(
            f"/api/collected-sessions/manage-test/segments/{segment['audio_filename']}"
        )
        assert segment_delete.status_code == 200
        assert client.get("/api/collected-sessions").json()["sessions"] == []

        missing_delete = client.delete("/api/collected-sessions/manage-test")
        assert missing_delete.status_code == 404
    finally:
        created_app.dependency_overrides.clear()
        created_app.state.provider_factory = None


def test_collected_file_falls_back_to_converted_extension(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    session_dir = recordings_dir / "collected" / "session-a"
    session_dir.mkdir(parents=True)
    # The WAV was already replaced by the async MP3 conversion.
    (session_dir / "segment-001-0.000-4.000.mp3").write_bytes(b"mp3-audio")
    (session_dir / "segment-001-0.000-4.000.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    client = TestClient(created_app)

    stale_wav = client.get(
        "/api/collected-sessions/session-a/files/segment-001-0.000-4.000.wav"
    )
    metadata = client.get(
        "/api/collected-sessions/session-a/files/segment-001-0.000-4.000.json"
    )
    missing = client.get(
        "/api/collected-sessions/session-a/files/segment-999-0.000-4.000.wav"
    )

    assert stale_wav.status_code == 200
    assert stale_wav.headers["content-type"].startswith("audio/mpeg")
    assert stale_wav.content == b"mp3-audio"
    assert metadata.status_code == 200
    assert missing.status_code == 404


def test_delete_collected_session_endpoint_removes_directory(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    session_dir = recordings_dir / "collected" / "session-a"
    session_dir.mkdir(parents=True)
    (session_dir / "segment-001-0.000-2.000.wav").write_bytes(b"wav")
    (session_dir / "segment-001-0.000-2.000.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    client = TestClient(created_app)

    response = client.delete("/api/collected-sessions/session-a")

    assert response.status_code == 200
    assert not session_dir.exists()


def test_startup_removes_orphaned_live_chunks_when_collection_enabled(
    tmp_path,
    monkeypatch,
):
    recordings_dir = tmp_path / "recordings"
    orphan = recordings_dir / "live" / "old-session" / "chunk-000001.wav"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"wav")

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr("backend.app.main.get_settings", override_settings)
    created_app = create_app(frontend_dist=None)

    with TestClient(created_app):
        pass

    assert not (recordings_dir / "live").exists()


def test_startup_keeps_live_chunks_when_collection_disabled(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"
    orphan = recordings_dir / "live" / "old-session" / "chunk-000001.wav"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"wav")

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    monkeypatch.setattr(
        "backend.app.main.get_settings",
        override_settings_without_collection,
    )
    created_app = create_app(frontend_dist=None)

    with TestClient(created_app):
        pass

    assert orphan.exists()


def test_end_live_session_for_unknown_session_returns_empty_summary():
    created_app = create_app(frontend_dist=None)
    client = TestClient(created_app)

    response = client.post(
        "/api/live-session/end",
        data={"session_id": "missing-session"},
    )

    summary = response.json()
    assert response.status_code == 200
    assert summary["session_id"] == "missing-session"
    assert summary["segment_count"] == 0
    assert summary["kept_chunk_count"] == 0
    assert summary["segments"] == []


def test_late_chunk_is_reported_as_discarded_after_session_end(tmp_path, monkeypatch):
    recordings_dir = tmp_path / "recordings"

    class EmptyProvider(FakeProvider):
        def analyze_live_chunk(self, path):
            return {"sound_event_detection": {"status": "success", "results": []}}

    monkeypatch.setattr("backend.app.main.DEFAULT_RECORDINGS_DIR", recordings_dir)
    created_app = create_app(frontend_dist=None)
    created_app.dependency_overrides[get_settings] = override_settings
    created_app.state.provider_factory = lambda settings: EmptyProvider(settings)
    client = TestClient(created_app)

    end_response = client.post(
        "/api/live-session/end",
        data={"session_id": "late-status-test"},
    )
    chunk_response = client.post(
        "/api/analyze-live-chunk",
        data={
            "session_id": "late-status-test",
            "sequence_id": "1",
            "window_start_sec": "0",
            "window_end_sec": "2",
        },
        files={"file": ("chunk.wav", make_wav_bytes(0, 2), "audio/wav")},
    )

    assert end_response.status_code == 200
    assert chunk_response.status_code == 200
    assert chunk_response.json()["collection_status"] == "discarded_late"
    assert not any(recordings_dir.rglob("*.wav"))


def test_end_live_session_rejects_invalid_session_id():
    client = TestClient(create_app(frontend_dist=None))

    response = client.post(
        "/api/live-session/end",
        data={"session_id": "../ambiguous"},
    )

    assert response.status_code == 422


def test_create_app_serves_built_frontend(tmp_path):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><title>Cochl.Sense Cloud Live Demo</title>",
        encoding="utf-8",
    )
    (assets / "app.js").write_text("console.log('ok')", encoding="utf-8")
    client = TestClient(create_app(frontend_dist=dist))

    index_response = client.get("/")
    asset_response = client.get("/assets/app.js")

    assert index_response.status_code == 200
    assert "Cochl.Sense Cloud Live Demo" in index_response.text
    assert asset_response.status_code == 200
    assert "console.log" in asset_response.text


def test_create_app_does_not_serve_files_outside_frontend_dist(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<!doctype html><title>Cochl.Sense Cloud Live Demo</title>",
        encoding="utf-8",
    )
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    client = TestClient(create_app(frontend_dist=dist))

    response = client.get("/%2e%2e/secret.txt")

    assert response.status_code == 200
    assert "Cochl.Sense Cloud Live Demo" in response.text
    assert "secret" not in response.text
