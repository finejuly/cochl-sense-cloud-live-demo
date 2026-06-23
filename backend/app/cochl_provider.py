from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.app.config import Settings


class CochlProvider:
    def __init__(self, settings: Settings):
        if not settings.cochl_project_key:
            raise ValueError("COCHL_PROJECT_KEY is required.")
        self.settings = settings

    def analyze_file(self, path: Path) -> dict[str, Any]:
        return self._analyze_path(
            path,
            sound_event_detection=self.settings.enable_sound_event_detection,
            speech_analysis=self.settings.enable_speech_analysis,
            audio_insights=self.settings.enable_audio_insights,
        )

    def analyze_live_chunk(self, path: Path) -> dict[str, Any]:
        return self._analyze_path(
            path,
            sound_event_detection=True,
            speech_analysis=False,
            audio_insights=False,
        )

    def _analyze_path(
        self,
        path: Path,
        *,
        sound_event_detection: bool,
        speech_analysis: bool,
        audio_insights: bool,
    ) -> dict[str, Any]:
        from cochl.sense import IntegratedApi, IntegratedApiOptions
        from cochl.sense.exception import CochlSenseException

        api = IntegratedApi(project_key=self.settings.cochl_project_key)
        options = IntegratedApiOptions(
            sound_event_detection=sound_event_detection,
            speech_analysis=speech_analysis,
            audio_insights=audio_insights,
        )
        options.caption = audio_insights
        if not speech_analysis:
            options.speaker_diarization = False
            options.speaker_profile = False

        try:
            submitted = api.analyze_file(str(path), options)
            job_id = _extract_job_id(submitted)
            if not job_id:
                return submitted
            return api.get_completed_result(job_id)
        except CochlSenseException as exc:
            raise RuntimeError(str(exc)) from exc


def _extract_job_id(response: dict[str, Any]) -> str | None:
    value = response.get("job_id") or response.get("id")
    if value is None:
        return None
    return str(value)
