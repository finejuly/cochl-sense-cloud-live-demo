from __future__ import annotations

import base64
import json
import ssl
import time
from collections.abc import Callable, Mapping
from http.client import HTTPException, HTTPSConnection
from math import isfinite
from pathlib import Path
from threading import local
from typing import Any
from urllib.parse import urlencode, urlparse

import soundfile


DEFAULT_COCHL_SED_HOST = "https://api.cochl.ai/sense/api/v1"
LIVE_UPLOAD_CHUNK_BYTES = 5 * 10**6
LIVE_RESULT_POLL_INTERVAL_SEC = 0.1
LIVE_CONNECTION_MAX_IDLE_SEC = 10.0


class CochlLiveClientError(RuntimeError):
    pass


class CochlLiveClientTimeoutError(TimeoutError):
    pass


class CochlLiveClient:
    """Minimal live SED client with one persistent HTTPS connection per worker."""

    def __init__(
        self,
        project_key: str,
        *,
        host: str = DEFAULT_COCHL_SED_HOST,
        socket_timeout_sec: float = 30.0,
        poll_interval_sec: float = LIVE_RESULT_POLL_INTERVAL_SEC,
        connection_factory: Callable[[float], Any] | None = None,
    ):
        if not project_key:
            raise ValueError("Cochl project key is required.")
        if not isfinite(socket_timeout_sec) or socket_timeout_sec <= 0:
            raise ValueError("Cochl socket timeout must be finite and positive.")
        if not isfinite(poll_interval_sec) or poll_interval_sec <= 0:
            raise ValueError("Cochl result poll interval must be finite and positive.")

        parsed = urlparse(host)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError("Cochl live SED host must be an HTTPS URL.")

        self.project_key = project_key
        self.host = host
        self.socket_timeout_sec = socket_timeout_sec
        self.poll_interval_sec = poll_interval_sec
        self._hostname = parsed.hostname
        self._port = parsed.port
        self._base_path = parsed.path.rstrip("/")
        self._ssl_context = ssl.create_default_context()
        self._connection_factory = connection_factory or self._new_connection
        self._connection: Any | None = None
        self._last_used_at: float | None = None

    def close(self) -> None:
        connection = self._connection
        self._connection = None
        self._last_used_at = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def predict(self, audio_path: Path, *, timeout_sec: float) -> dict[str, Any]:
        if not isfinite(timeout_sec) or timeout_sec <= 0:
            raise ValueError("Cochl live timeout must be finite and positive.")

        path = Path(audio_path)
        metadata = _audio_metadata(path)
        deadline_at = time.monotonic() + timeout_sec

        session = self._request_json(
            "POST",
            "/audio_sessions/",
            {
                "type": "file",
                "total_size": metadata["total_size"],
                "content_type": metadata["content_type"],
                "file_name": path.name,
                "file_length": metadata["file_length"],
                "default_sensitivity": 0,
                "tags_sensitivity": {},
            },
            deadline_at=deadline_at,
        )
        session_id = session.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise CochlLiveClientError(
                "Cochl live SED session response did not include a session id."
            )

        with path.open("rb") as audio_file:
            sequence = 0
            while chunk := audio_file.read(LIVE_UPLOAD_CHUNK_BYTES):
                self._request_json(
                    "PUT",
                    f"/audio_sessions/{session_id}/chunks/{sequence}",
                    {"data": base64.b64encode(chunk).decode("ascii")},
                    deadline_at=deadline_at,
                )
                sequence += 1

        return {
            "session_id": session_id,
            "window_results": self._get_results(session_id, deadline_at),
        }

    def _get_results(
        self,
        session_id: str,
        deadline_at: float,
    ) -> list[dict[str, Any]]:
        offset = 0
        windows: list[dict[str, Any]] = []

        while True:
            query = urlencode({"offset": offset, "limit": 1024})
            result = self._request_json(
                "GET",
                f"/audio_sessions/{session_id}/results?{query}",
                deadline_at=deadline_at,
            )
            state = result.get("state")
            if state in {"pending", "in-progress"}:
                remaining = _remaining_seconds(deadline_at)
                time.sleep(min(self.poll_interval_sec, remaining))
                continue
            if state == "error":
                detail = result.get("error") or "Cochl live SED returned an error."
                raise CochlLiveClientError(str(detail))
            if state != "done":
                raise CochlLiveClientError(
                    f"Cochl live SED returned invalid result state {state!r}."
                )

            has_more = result.get("has_more")
            batch = result.get("data")
            if not isinstance(has_more, bool) or not isinstance(batch, list):
                raise CochlLiveClientError(
                    "Cochl live SED returned an invalid completed result."
                )
            converted = [_window_result(item) for item in batch]
            windows.extend(converted)
            offset += len(converted)
            if not has_more:
                return windows

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        deadline_at: float,
    ) -> dict[str, Any]:
        request_timeout = min(
            self.socket_timeout_sec,
            _remaining_seconds(deadline_at),
        )
        connection = self._get_connection(request_timeout)
        body = None
        headers = {"X-Api-Key": self.project_key}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers.update(
                {
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Length": str(len(body)),
                }
            )

        try:
            connection.request(
                method,
                self._base_path + path,
                body=body,
                headers=headers,
            )
            response = connection.getresponse()
            raw = response.read()
        except TimeoutError as exc:
            self.close()
            raise CochlLiveClientTimeoutError(
                "Cochl live SED socket operation timed out."
            ) from exc
        except (OSError, HTTPException, ssl.SSLError) as exc:
            self.close()
            raise CochlLiveClientError(
                f"Cochl live SED connection failed: {type(exc).__name__}."
            ) from exc

        self._last_used_at = time.monotonic()
        if getattr(response, "will_close", False):
            self.close()

        if response.status not in {200, 201}:
            detail = raw.decode("utf-8", errors="replace")[:500]
            raise CochlLiveClientError(
                f"Cochl live SED returned HTTP {response.status}: {detail}"
            )
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CochlLiveClientError(
                "Cochl live SED returned invalid JSON."
            ) from exc
        if not isinstance(decoded, Mapping):
            raise CochlLiveClientError(
                "Cochl live SED returned a non-object response."
            )
        if decoded.get("error"):
            raise CochlLiveClientError(str(decoded["error"]))
        return dict(decoded)

    def _get_connection(self, timeout_sec: float):
        if (
            self._connection is not None
            and self._last_used_at is not None
            and time.monotonic() - self._last_used_at >= LIVE_CONNECTION_MAX_IDLE_SEC
        ):
            self.close()
        if self._connection is None:
            self._connection = self._connection_factory(timeout_sec)

        self._connection.timeout = timeout_sec
        socket = getattr(self._connection, "sock", None)
        if socket is not None:
            socket.settimeout(timeout_sec)
        return self._connection

    def _new_connection(self, timeout_sec: float) -> HTTPSConnection:
        return HTTPSConnection(
            self._hostname,
            self._port,
            timeout=timeout_sec,
            context=self._ssl_context,
        )


_thread_clients = local()


def get_thread_live_client(
    project_key: str,
    *,
    socket_timeout_sec: float,
    host: str = DEFAULT_COCHL_SED_HOST,
) -> CochlLiveClient:
    clients = getattr(_thread_clients, "clients", None)
    if clients is None:
        clients = {}
        _thread_clients.clients = clients
    key = (project_key, host, float(socket_timeout_sec))
    client = clients.get(key)
    if client is None:
        client = CochlLiveClient(
            project_key,
            host=host,
            socket_timeout_sec=socket_timeout_sec,
        )
        clients[key] = client
    return client


def _remaining_seconds(deadline_at: float) -> float:
    remaining = deadline_at - time.monotonic()
    if remaining <= 0:
        raise CochlLiveClientTimeoutError(
            "Cochl live SED exceeded its total deadline."
        )
    return remaining


def _audio_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size <= 0:
        raise ValueError("Cochl live audio file is empty.")
    try:
        info = soundfile.info(str(path))
    except RuntimeError as exc:
        raise ValueError("Cochl live audio format is invalid.") from exc
    if info.samplerate <= 0:
        raise ValueError("Cochl live audio sample rate is invalid.")
    return {
        "total_size": path.stat().st_size,
        "content_type": f"audio/{info.format.lower()}",
        "file_length": info.frames / info.samplerate,
    }


def _window_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CochlLiveClientError("Cochl live SED returned an invalid window.")
    tags = value.get("tags")
    if not isinstance(tags, list):
        raise CochlLiveClientError("Cochl live SED returned invalid window tags.")
    sound_tags = []
    for tag in tags:
        if not isinstance(tag, Mapping) or not tag.get("name"):
            raise CochlLiveClientError("Cochl live SED returned an invalid tag.")
        sound_tags.append(
            {
                "name": str(tag["name"]),
                "probability": tag.get("probability"),
            }
        )
    return {
        "start_time": value.get("start_time"),
        "end_time": value.get("end_time"),
        "sound_tags": sound_tags,
    }
