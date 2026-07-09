# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A local demo app for streaming microphone audio to the Cochl.Sense Cloud API and monitoring live sound-event results. The browser records mic input, sends short live WAV chunks to a FastAPI backend (which proxies to Cochl.Sense Cloud), and renders detected events, a live spectrogram, per-chunk request states, and latency metrics in a React dashboard.

Three deployables share one repo: `backend/` (FastAPI proxy), `frontend/` (React + Vite dashboard), and `macos/` (a native Objective-C WKWebView shell that launches the backend and points a window at it).

## Commands

Backend setup (run from repo root; note the specific interpreter and editable install):

```bash
/opt/homebrew/bin/python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e "backend[dev]"
cp .env.example .env   # then set COCHL_PROJECT_KEY
```

Run locally (two processes):

```bash
.venv/bin/uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
cd frontend && nvm use && npm install && npm run dev   # Vite proxies /api -> :8000
```

Tests and build:

```bash
.venv/bin/python -m pytest backend/tests -v          # all backend tests
.venv/bin/python -m pytest backend/tests/test_api.py::<name> -v   # single test
cd frontend && npm test               # vitest watch
cd frontend && npm test -- --run      # vitest once (CI)
cd frontend && npm test -- --run src/liveTimeline.test.ts   # single file
cd frontend && npm run build          # tsc -b && vite build -> frontend/dist
```

macOS app (requires Xcode Command Line Tools; build script runs `npm run build` first):

```bash
scripts/build-macos-app.sh && open CochlSenseCloudLiveDemo.app
```

## Architecture

### Two analysis paths

The app has two distinct request flows against Cochl, and changes usually need to touch both consistently:

- **Live chunking** (`POST /api/analyze-live-chunk`): the primary feature. While recording, the frontend synthesizes 2-second WAV windows (1-second hop) via Web Audio and posts them once per second. Only sound event detection runs for live chunks (`CochlProvider.analyze_live_chunk` hardcodes SED-only regardless of settings). Sound-event timestamps are offset by `window_start_sec` so they land on the correct point of the global timeline.
- **Full recording** (`POST /api/analyze-recording`): after recording stops, the complete file is analyzed once. This path respects the enabled-services config (SED / speech analysis / audio insights) and returns the full `AnalysisResponse`.

### Live data collection

By default (`COCHL_COLLECTION_ENABLED=true`), analyzed live chunks flow into `collection.py` instead of being kept as per-chunk debug MP3s. `SegmentCollector` classifies each chunk (`collected` / `discarded_silent` / `discarded_speech`) and merges kept chunks — trimming the 1-second window overlap — into contiguous WAV segments of `COCHL_COLLECTION_MIN_SEGMENT_SEC`–`COCHL_COLLECTION_MAX_SEGMENT_SEC` (5–20 s) under `recordings/collected/<session-id>/`, each with a metadata JSON. Silent chunks are not deleted immediately: they wait in a context buffer and pad short detections to min-length on **both sides** — `_start_segment_with_context` caps leading pre-roll at half the deficit, `_handle_silent_entry` appends trailing silence, and `_finalize_current_segment` tops the front back up if trailing audio ran out. Only unneeded context is discarded. Segments finalize **in real time**: silence stretching `COCHL_COLLECTION_SILENCE_CLOSE_SEC` (3 s) past the last kept chunk closes the open segment mid-recording (reason `silence`), each finalize rewrites `session.json` (`ended_at: null` until the session ends), and the frontend panel polls every 5 s while recording. Key invariants:

- **Privacy first**: any event whose label matches `COCHL_COLLECTION_EXCLUDE_LABEL_KEYWORDS` (case-insensitive substring, e.g. `Male_speech`) discards the chunk regardless of other events, forces a segment split, and flushes the context buffer — so neither a collected file nor its padding ever spans a speech region.
- **Out-of-order tolerance**: chunk analyses complete out of order (up to `LIVE_MAX_IN_FLIGHT` concurrent), so entries sit in a reorder buffer and are only folded into segments once the watermark (max window end seen − `reorder_hold_back_sec`) passes them. `POST /api/live-session/end` (called by the frontend on 완료/폐기/unmount) flushes everything and returns a `LiveSessionEndResponse` summary.
- Collection failures must never fail the analyze response — the route wraps collection in try/except and returns `collection_status: null`.
- `LiveCollectionManager` (one per app, on `app.state`) keys collectors by *sanitized* session id (`saved_path.parent.name`) and finalizes stale sessions opportunistically.
- **No live leftovers**: ended sessions leave tombstones in the manager, so a chunk whose analysis completes after `end_session` is deleted and returns `discarded_late` instead of respawning a collector; the frontend gives each request a 60-second timeout, waits for all submitted request promises before calling end on 완료, then invalidates the session token; and `cleanup_orphan_live_chunks` (lifespan startup) removes `recordings/live/` entirely when collection is enabled. Provider/normalization failures also delete their staged WAV immediately while collection is enabled. Keep all of these intact when touching the live path.
- **Session naming**: an optional `session_name` form field (chunk + end requests) and collector-side `started_at`/`ended_at` timestamps flow into `session.json`, segment metadata JSONs, and `LiveSessionEndResponse`.
- **Management API**: `GET /api/collected-sessions` lists sessions from disk (`list_collected_sessions` reads segment JSONs, resolves the actual audio extension since MP3 conversion is async); `GET .../files/{filename}` serves audio/metadata; `DELETE` removes a session dir or one segment (audio+json, dropping the session dir when the last segment goes). All paths are validated with `safe_collected_session_dir` — session dirs must resolve strictly one level under `recordings/collected/`.

### Backend (`backend/app/`)

- `main.py` — `create_app()` factory wires routes and app state, and (when `frontend/dist` exists) serves the built SPA with a path-traversal-guarded catch-all. Live chunks are (1) rate-limited to `LIVE_PROVIDER_MAX_CONCURRENCY=10` concurrent Cochl calls via an anyio `CapacityLimiter`, and (2) after analysis, routed into the collection pipeline (or, with collection disabled, converted to MP3 **asynchronously** on a bounded `ThreadPoolExecutor`: `LIVE_CONVERSION_MAX_WORKERS=2`, drop when `>= LIVE_CONVERSION_MAX_PENDING=32` pending; finalized collection segments reuse the same executor). Full-recording conversion and analysis run together in a worker thread behind `RECORDING_PROVIDER_MAX_CONCURRENCY=2`. Uploads stream from `UploadFile.file` to exclusive destination files in 1 MiB chunks so size limits do not require loading the entire file into memory. Live session ids are strictly validated rather than sanitized. Blocking Cochl SDK and ffmpeg calls never run on the event loop.
- `collection.py` — the live data-collection pipeline (see above). Pure stdlib (`wave`), no Cochl SDK; thread-safe via per-collector locks.
- `cochl_provider.py` — the only module that imports the `cochl` SDK (imported lazily inside methods so tests can run without it). Submits a file, extracts a `job_id`, and polls `get_completed_result`. Tests inject a fake provider through `app.state.provider_factory`.
- `config.py` — frozen `Settings` dataclass loaded from env via `Settings.from_env()`. `get_settings()` in `main.py` is `lru_cache`d, so **env changes require a process restart**. `validate_service_combination` enforces that audio insights requires both SED and speech analysis.
- `normalization.py` — defensively maps Cochl's raw (and variably-keyed) JSON into the typed response models. It tolerates alternate field names (`start_time_sec`/`start`, `class`/`label`/`name`, etc.) via `_first_present` — preserve this leniency when editing.
- `audio.py` — content-type/extension mapping, upload size validation, and `ffmpeg` shelling for WAV/MP3 conversion. If a browser recording isn't a Cochl-supported format (e.g. WebM), `prepare_audio_for_cochl` converts it to 16 kHz mono WAV; missing `ffmpeg` raises `AudioConversionError` (surfaced as HTTP 415).
- `models.py` — Pydantic response models (`AnalysisResponse`, `LiveChunkAnalysisResponse`, `SoundEvent`, etc.).

Full recordings persist to `recordings/`. With collection enabled (default), meaningful live audio ends up in `recordings/collected/<session-id>/` and discarded chunks are deleted; with it disabled, every live chunk is kept under `recordings/live/<session-id>/` as MP3. Nothing else is auto-deleted.

### Frontend (`frontend/src/`)

`App.tsx` is the large orchestrator; most logic lives in small, individually-tested pure modules:

- `liveAudio.ts` — `LiveWindowBuffer` slices the mic stream into overlapping windows; `createLiveAudioCapture` wires the Web Audio graph and emits both windows and spectrogram frames; `encodePcm16Wav` builds the WAV blob sent to the backend.
- `liveChunkRecords.ts` — the per-chunk state machine. Each chunk is `PENDING | DETECTED | EMPTY | FAIL | SKIP` and carries latency fields; also derives the render geometry for the chunk timeline.
- `liveTimeline.ts` — merges/lays out detected events into lanes and manages the scrolling viewport.
- `api.ts` — all backend fetch calls (`analyzeRecording`, `analyzeLiveChunk`, `endLiveSession`, collected-session list/delete/file-URL helpers); error bodies are read from `{ detail }`. **User-facing strings are Korean** — match that when adding UI copy.
- `CollectedSessionsPanel.tsx` — self-contained 수집된 데이터 management panel (list, inline audio playback, confirm-guarded deletes). It refetches whenever the `refreshToken` prop changes; `App.tsx` bumps it after each session end.
- `LiveSpectrogramPanel.tsx`, `waveform.ts`, `recorder.ts`, `audioContext.ts` — canvas rendering, waveform helpers, MediaRecorder wrapper, cross-browser AudioContext lookup. Live capture prefers `AudioWorklet` with a ScriptProcessor fallback for old WebViews; spectrogram frames live in a bounded, progressively compacted ref and viewport selection uses binary search.

**Frontend backpressure**: `handleLiveWindow` tracks in-flight requests in `liveInFlightRef` and drops a window as `SKIP` once `LIVE_MAX_IN_FLIGHT` is reached, rather than queuing. A `liveSessionTokenRef` guards every async continuation so stale responses from a previous recording are ignored. This is deliberate — keep new async live-path work token-guarded.

## Conventions

- Backend: `from __future__ import annotations`, dataclasses, and thorough test coverage per module. New Cochl-response fields go through `normalization.py`, never parsed inline in routes.
- The `cochl` SDK is imported lazily only in `cochl_provider.py`; keep it out of other modules so tests and normalization stay SDK-free.
- Node version: `scripts/build-macos-app.sh` pins node `v24.14.0` and `.nvmrc` is `24.14.0`, while `package.json` engines and the README state `>=20.19`. Use `nvm use` to match `.nvmrc`.
