from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from time import monotonic
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import urlopen

from backend.app.audio import prepare_live_audio_for_cochl
from backend.app.cochl_live_client import (
    CochlLiveClientError,
    CochlLiveClientTimeoutError,
    get_thread_live_client,
)
from backend.app.config import Settings

logger = logging.getLogger(__name__)


class CochlProviderTimeoutError(TimeoutError):
    pass


class CochlProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        integrated_api_factory: Callable[[str], Any] | None = None,
        live_client_factory: Callable[[str], Any] | None = None,
        persistent_live_client_factory: Callable[[str, float], Any] | None = None,
    ):
        if not settings.cochl_project_key:
            raise ValueError("COCHL_PROJECT_KEY is required.")
        self.settings = settings
        self._integrated_api_factory = integrated_api_factory
        self._live_client_factory = live_client_factory
        self._persistent_live_client_factory = persistent_live_client_factory

    def analyze_file(self, path: Path) -> dict[str, Any]:
        if (
            self.settings.enable_sound_event_detection
            and not self.settings.enable_speech_analysis
            and not self.settings.enable_audio_insights
        ):
            return self._analyze_sound_events(path)
        return self._analyze_path(
            path,
            sound_event_detection=self.settings.enable_sound_event_detection,
            speech_analysis=self.settings.enable_speech_analysis,
            audio_insights=self.settings.enable_audio_insights,
            total_timeout_sec=self.settings.cochl_recording_timeout_sec,
        )

    def analyze_live_chunk(self, path: Path) -> dict[str, Any]:
        # Live windows are exactly two seconds long. The Integration API can
        # currently return success with an empty results list for a known-
        # positive two-second input, while the still-supported SED-only client
        # detects it. Keep multi-service recordings on IntegratedApi and use
        # the dedicated SED endpoint for live windows.
        analyze = (
            self._analyze_sound_events
            if self._live_client_factory is not None
            or not self.settings.cochl_live_persistent_connections
            else self._analyze_persistent_live_sound_events
        )
        if (
            self._live_client_factory is not None
            or not self.settings.cochl_live_transport_compression
        ):
            return analyze(path)

        prepared = prepare_live_audio_for_cochl(path)
        try:
            return analyze(prepared.path)
        finally:
            if prepared.path != path:
                try:
                    prepared.path.unlink(missing_ok=True)
                except OSError:
                    logger.exception(
                        "Could not remove live provider transport %s.", prepared.path
                    )

    def _analyze_persistent_live_sound_events(self, path: Path) -> dict[str, Any]:
        try:
            client = (
                self._persistent_live_client_factory(
                    self.settings.cochl_project_key,
                    self.settings.cochl_socket_timeout_sec,
                )
                if self._persistent_live_client_factory is not None
                else get_thread_live_client(
                    self.settings.cochl_project_key,
                    socket_timeout_sec=self.settings.cochl_socket_timeout_sec,
                )
            )
            payload = _require_mapping(
                client.predict(
                    path,
                    timeout_sec=self.settings.cochl_live_timeout_sec,
                ),
                stage="persistent live SED result",
            )
            return {
                "sound_event_detection": {
                    "status": "success",
                    "results": _legacy_sound_event_results(payload),
                }
            }
        except CochlLiveClientTimeoutError as exc:
            raise CochlProviderTimeoutError(str(exc)) from exc
        except (CochlLiveClientError, OSError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    def _analyze_sound_events(self, path: Path) -> dict[str, Any]:
        from cochl.sense import Client, TimeoutException
        from cochl.sense.exception import CochlSenseException
        from cochl.sense.http_request import HttpRequestException

        client = (
            self._live_client_factory(self.settings.cochl_project_key)
            if self._live_client_factory is not None
            else Client(self.settings.cochl_project_key)
        )
        try:
            result = client.predict(
                str(path),
                timeout=self.settings.cochl_live_timeout_sec,
            )
            events = getattr(result, "events", None)
            to_dict = getattr(events, "to_dict", None)
            if not callable(to_dict):
                raise RuntimeError("Cochl legacy SED returned an invalid response.")
            payload = _require_mapping(
                to_dict(client.config),
                stage="legacy SED result",
            )
            return {
                "sound_event_detection": {
                    "status": "success",
                    "results": _legacy_sound_event_results(payload),
                }
            }
        except TimeoutException as exc:
            raise CochlProviderTimeoutError(str(exc)) from exc
        except (CochlSenseException, HttpRequestException, OSError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

    def _analyze_path(
        self,
        path: Path,
        *,
        sound_event_detection: bool,
        speech_analysis: bool,
        audio_insights: bool,
        total_timeout_sec: float,
    ) -> dict[str, Any]:
        from cochl.sense import IntegratedApi, IntegratedApiOptions
        from cochl.sense.exception import CochlSenseException

        api = (
            self._integrated_api_factory(self.settings.cochl_project_key)
            if self._integrated_api_factory is not None
            else IntegratedApi(project_key=self.settings.cochl_project_key)
        )
        options = IntegratedApiOptions(
            sound_event_detection=sound_event_detection,
            speech_analysis=speech_analysis,
            audio_insights=audio_insights,
        )
        # The Integration API currently requires this compatibility field even
        # though cochl-sense-sdk 2.x does not declare it on
        # IntegratedApiOptions. Omitting it makes otherwise valid uploads fail
        # at submission with HTTP 400 Bad Request.
        options.caption = audio_insights
        if not speech_analysis:
            options.speaker_diarization = False
            options.speaker_profile = False
        deadline_at = monotonic() + total_timeout_sec

        try:
            if self._integrated_api_factory is None:
                submitted = _submit_with_socket_timeout(
                    api,
                    path,
                    options,
                    _remaining_socket_timeout(
                        deadline_at,
                        self.settings.cochl_socket_timeout_sec,
                    ),
                )
            else:
                submitted = api.analyze_file(str(path), options)
            submitted = _require_mapping(submitted, stage="submission")
            job_id = _extract_job_id(submitted)
            if not job_id:
                raise RuntimeError("Cochl submission did not return a job id.")
            if self._integrated_api_factory is None:
                completed = _get_completed_result_with_socket_timeout(
                    api,
                    job_id,
                    _remaining_socket_timeout(
                        deadline_at,
                        self.settings.cochl_socket_timeout_sec,
                    ),
                    deadline_at=deadline_at,
                )
            else:
                completed = api.get_completed_result(job_id)
            return _require_mapping(completed, stage="completed result")
        except TimeoutError as exc:
            raise CochlProviderTimeoutError(
                str(exc) or "Cochl analysis timed out."
            ) from exc
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise CochlProviderTimeoutError(
                    str(exc.reason) or "Cochl analysis timed out."
                ) from exc
            raise RuntimeError(str(exc)) from exc
        except (CochlSenseException, OSError) as exc:
            raise RuntimeError(str(exc)) from exc


def _extract_job_id(response: Mapping[str, Any]) -> str | None:
    value = response.get("job_id") or response.get("id")
    if value is None:
        return None
    return str(value)


def _require_mapping(value: Any, *, stage: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"Cochl {stage} returned an invalid response.")
    return dict(value)


def _legacy_sound_event_results(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    windows = payload.get("window_results")
    if not isinstance(windows, list):
        raise RuntimeError("Cochl legacy SED returned invalid window results.")

    results: list[dict[str, Any]] = []
    for window in windows:
        if not isinstance(window, Mapping):
            raise RuntimeError("Cochl legacy SED returned an invalid window.")
        tags = window.get("sound_tags")
        if not isinstance(tags, list):
            raise RuntimeError("Cochl legacy SED returned invalid sound tags.")

        classes = []
        for tag in tags:
            if not isinstance(tag, Mapping):
                raise RuntimeError("Cochl legacy SED returned an invalid sound tag.")
            label = tag.get("name")
            if not label or str(label).strip().lower() == "others":
                continue
            classes.append(
                {
                    "class": str(label),
                    "confidence": tag.get("probability"),
                }
            )

        if classes:
            results.append(
                {
                    "start_time_sec": window.get("start_time"),
                    "end_time_sec": window.get("end_time"),
                    "classes": classes,
                }
            )
    return results


def _submit_with_socket_timeout(api: Any, path: Path, options: Any, timeout: float):
    from cochl.sense.http_request import HttpRequest

    with path.open("rb") as audio_file:
        response = HttpRequest.post(
            f"{api._host}/analyze_file",
            {"options": json.dumps(options.__dict__)},
            headers={"X-Api-Key": api._project_key},
            files={"audio": audio_file},
            timeout=timeout,
        )
    return api._result(response)


def _get_completed_result_with_socket_timeout(
    api: Any,
    job_id: str,
    timeout: float,
    *,
    deadline_at: float | None = None,
):
    request = api.create_event_stream_request(job_id)
    current_event: str | None = None

    with urlopen(request, timeout=timeout) as response:
        for raw_line in response:
            if deadline_at is not None and monotonic() >= deadline_at:
                raise TimeoutError("Cochl analysis deadline exceeded.")
            line = raw_line.decode("utf-8").strip()
            if line.startswith("event: "):
                current_event = line.removeprefix("event: ")
                continue
            if not line.startswith("data: "):
                continue
            data_text = line.removeprefix("data: ")
            try:
                payload = json.loads(data_text)
            except json.JSONDecodeError as exc:
                if current_event in {"completed", "error"}:
                    raise RuntimeError("Cochl stream returned invalid JSON.") from exc
                continue
            if current_event == "error":
                detail = (
                    payload.get("error") or payload.get("message")
                    if isinstance(payload, Mapping)
                    else payload
                )
                raise RuntimeError(f"Cochl stream failed: {detail}")
            if current_event == "completed":
                return payload

    raise RuntimeError("Cochl stream ended without a completed result.")


def _remaining_socket_timeout(deadline_at: float, socket_timeout: float) -> float:
    remaining = deadline_at - monotonic()
    if remaining <= 0:
        raise TimeoutError("Cochl analysis deadline exceeded.")
    return max(0.001, min(socket_timeout, remaining))
