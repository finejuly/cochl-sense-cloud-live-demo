import base64
import json
import wave

import pytest

from backend.app.cochl_live_client import (
    CochlLiveClient,
    CochlLiveClientError,
    CochlLiveClientTimeoutError,
)


class FakeResponse:
    def __init__(self, payload, *, status=200, will_close=False):
        self.payload = payload
        self.status = status
        self.will_close = will_close

    def read(self):
        return json.dumps(self.payload).encode()


class FakeConnection:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.timeout = None
        self.sock = None
        self.close_count = 0

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        return self.responses.pop(0)

    def close(self):
        self.close_count += 1


def write_wav(path):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\0\0" * 16_000)


def response_sequence(session_id):
    return [
        FakeResponse({"session_id": session_id}),
        FakeResponse({"status": "ok"}),
        FakeResponse({"state": "pending", "has_more": False, "data": []}),
        FakeResponse(
            {
                "state": "done",
                "has_more": False,
                "data": [
                    {
                        "start_time": 0,
                        "end_time": 2,
                        "tags": [{"name": "Cough", "probability": 0.93}],
                    }
                ],
            }
        ),
    ]


def test_live_client_reuses_connection_and_returns_legacy_payload(
    tmp_path, monkeypatch
):
    audio_path = tmp_path / "chunk.wav"
    write_wav(audio_path)
    connection = FakeConnection(
        response_sequence("session-1") + response_sequence("session-2")
    )
    factory_calls = []
    monkeypatch.setattr("backend.app.cochl_live_client.time.sleep", lambda _: None)

    def connection_factory(timeout):
        factory_calls.append(timeout)
        return connection

    client = CochlLiveClient(
        "test-key",
        host="https://example.test/sense/api/v1",
        socket_timeout_sec=7.0,
        connection_factory=connection_factory,
    )

    first = client.predict(audio_path, timeout_sec=5.0)
    second = client.predict(audio_path, timeout_sec=5.0)

    assert factory_calls == [pytest.approx(5.0)]
    assert first == {
        "session_id": "session-1",
        "window_results": [
            {
                "start_time": 0,
                "end_time": 2,
                "sound_tags": [{"name": "Cough", "probability": 0.93}],
            }
        ],
    }
    assert second["session_id"] == "session-2"
    assert [request[0] for request in connection.requests] == [
        "POST",
        "PUT",
        "GET",
        "GET",
        "POST",
        "PUT",
        "GET",
        "GET",
    ]
    create_body = json.loads(connection.requests[0][2])
    assert create_body["content_type"] == "audio/wav"
    assert create_body["file_length"] == 2.0
    upload_body = json.loads(connection.requests[1][2])
    assert base64.b64decode(upload_body["data"]) == audio_path.read_bytes()
    assert connection.requests[0][3]["X-Api-Key"] == "test-key"
    assert connection.requests[2][1].endswith(
        "/audio_sessions/session-1/results?offset=0&limit=1024"
    )


def test_live_client_closes_connection_after_timeout(tmp_path):
    audio_path = tmp_path / "chunk.wav"
    write_wav(audio_path)

    class TimingOutConnection(FakeConnection):
        def request(self, method, path, body=None, headers=None):
            raise TimeoutError("slow")

    connection = TimingOutConnection([])
    client = CochlLiveClient(
        "test-key",
        host="https://example.test/api/v1",
        connection_factory=lambda timeout: connection,
    )

    with pytest.raises(CochlLiveClientTimeoutError, match="timed out"):
        client.predict(audio_path, timeout_sec=1.0)

    assert connection.close_count == 1


def test_live_client_rejects_malformed_completed_results(tmp_path):
    audio_path = tmp_path / "chunk.wav"
    write_wav(audio_path)
    connection = FakeConnection(
        [
            FakeResponse({"session_id": "session-1"}),
            FakeResponse({}),
            FakeResponse({"state": "done", "has_more": "no", "data": []}),
        ]
    )
    client = CochlLiveClient(
        "test-key",
        host="https://example.test/api/v1",
        connection_factory=lambda timeout: connection,
    )

    with pytest.raises(CochlLiveClientError, match="invalid completed result"):
        client.predict(audio_path, timeout_sec=1.0)


def test_live_client_rejects_non_https_host():
    with pytest.raises(ValueError, match="HTTPS"):
        CochlLiveClient("test-key", host="http://example.test/api/v1")
