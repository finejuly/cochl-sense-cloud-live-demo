# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

A local demo app for streaming microphone audio to the Cochl.Sense Cloud API and monitoring live sound-event results. The browser records mic input, sends short live WAV chunks to a FastAPI backend (which proxies to Cochl.Sense Cloud), and renders detected events, a live spectrogram, per-chunk request states, and latency metrics in a React dashboard.

Three deployables share one repo: `backend/` (FastAPI proxy), `frontend/` (React + Vite dashboard), and `macos/` (a native Objective-C WKWebView shell that launches the backend and points a window at it).

## Commands

Backend setup (run from repo root; note the specific interpreter and editable install):

```bash
/opt/homebrew/bin/python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip "setuptools>=83.0.0"
.venv/bin/python -m pip install -c backend/constraints.txt -e "backend[dev]"
cp .env.example .env   # then set COCHL_PROJECT_KEY
```

Run locally (two processes):

```bash
.venv/bin/python -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
cd frontend && nvm use && npm install && npm run dev   # Vite proxies /api -> :8000
```

Provider timing defaults are `COCHL_LIVE_TIMEOUT_SEC=20` for an entire live
worker, `COCHL_RECORDING_TIMEOUT_SEC=900` for an entire standalone-recording
worker, and `COCHL_SOCKET_TIMEOUT_SEC=30` for each Cochl HTTP/SSE connect/read
operation. They must be finite positive seconds. Settings are cached, so env
changes require a backend restart; a timed-out blocking worker is not forcibly
cancelled but remains bounded by its executor capacity.

Tests and build:

```bash
.venv/bin/python -m pytest backend/tests -v          # all backend tests
.venv/bin/python -m pytest backend/tests/test_api.py::<name> -v   # single test
cd frontend && npm test               # vitest watch
cd frontend && npm test -- --run      # vitest once (CI)
cd frontend && npm test -- --run src/liveTimeline.test.ts   # single file
cd frontend && npm run build          # tsc -b && vite build -> frontend/dist
```

macOS app (requires Xcode Command Line Tools; exact `.nvmrc` Node version):

```bash
scripts/build-macos-app.sh && open CochlSenseCloudLiveDemo.app
scripts/build-macos-app.sh --clean   # npm ci + clean frontend/app output
scripts/verify-macos-app.sh          # plist, architectures, Mach-O minos
COCHL_MACOS_ARCHS="arm64 x86_64" scripts/build-macos-app.sh --clean
COCHL_CODESIGN_IDENTITY="Apple Development: ..." scripts/build-macos-app.sh
```

The macOS target is 13.0. The output is a repo-dependent development wrapper,
not a self-contained distribution: it launches this checkout's `.venv`, source,
`.env`, and `frontend/dist`. It does not bundle Python/ffmpeg or implement code
Developer ID signing, hardened runtime, notarization, packaging, or updates.
The local build still ad-hoc signs and verifies the complete bundle so a
universal2 wrapper has one coherent macOS identity. Universal2 only
describes the thin native wrapper executable; external runtime compatibility
must be checked separately. `run_macos_server.py` owns one reserved loopback
socket through Uvicorn startup, waits for `/api/ready`, and forms a process group;
the native wrapper uses bounded TERM/graceful/KILL/reap shutdown semantics.

## Architecture

### Analysis paths

The backend exposes two request flows against Cochl, but the dashboard intentionally uses only live chunking so a long session never accumulates one full recording in browser memory:

- **Live chunking** (`POST /api/analyze-live-chunk`): the primary feature. The frontend requests mono 48 kHz microphone input with echo cancellation, noise suppression, automatic gain control, and Voice Isolation disabled, and rejects startup when reported track settings show any supported processor still enabled. Its `AudioContext` is also required to run at 48 kHz, then it synthesizes 16-bit PCM mono 2-second WAV windows (1-second hop) and posts them once per second. Only sound event detection runs for live chunks. `CochlProvider` intentionally uses the dedicated SED-only endpoint: the currently pinned Integration API returns success with an empty SED result for a known-positive exact two-second fixture, while the dedicated endpoint detects it. The live-only `CochlLiveClient` reuses one HTTPS connection per worker and polls pending results every 100 ms; `COCHL_LIVE_PERSISTENT_CONNECTIONS=false` restores the pinned SDK `Client` transport. When `ffmpeg` is available and `COCHL_LIVE_TRANSPORT_COMPRESSION=true`, live analysis sends a temporary sample-rate-preserving mono Ogg Vorbis copy to reduce provider upload latency while retaining the original 48 kHz WAV for collection; conversion failure falls back to WAV and the temporary is always removed. SED-only explicit file analysis stays on the SDK `Client`, and multi-service full-recording analysis stays on `IntegratedApi`; re-test this compatibility boundary before removing the dedicated endpoint or changing the SDK major version. Sound-event timestamps are offset by `window_start_sec` so they land on the correct point of the global timeline.
- **Full recording API** (`POST /api/analyze-recording`): retained for explicit standalone clients only; the dashboard does not call it. This path respects the enabled-services config (SED / speech analysis / audio insights) and returns the full `AnalysisResponse`.

### Live data collection

By default (`COCHL_COLLECTION_ENABLED=true`), analyzed live chunks flow into `collection.py` instead of being kept as per-chunk debug MP3s. `SegmentCollector` classifies each chunk (`collected` / `discarded_silent` / `discarded_speech`) and merges kept chunks — trimming the 1-second window overlap — into contiguous WAV segments of `COCHL_COLLECTION_MIN_SEGMENT_SEC`–`COCHL_COLLECTION_MAX_SEGMENT_SEC` (5–20 s) under `recordings/collected/<session-id>/`, each with a metadata JSON. Silent chunks are not deleted immediately: they wait in a context buffer, bridge nearby detections into the same segment, and pad short detections to min-length on **both sides** — `_start_segment_with_context` caps leading pre-roll at half the deficit, `_handle_silent_entry` appends trailing silence, and `_finalize_current_segment` tops the front back up if trailing audio ran out. If safe context is unavailable at a privacy boundary or session end, the final actual-audio duration check discards the segment instead of saving a file shorter than the configured minimum. Only unneeded context is discarded. Segments finalize **in real time**: silence stretching `COCHL_COLLECTION_SILENCE_CLOSE_SEC` (3 s) past the last kept chunk closes the open segment mid-recording (reason `silence`), including on the chunk that first satisfies minimum padding, and each finalize rewrites `session.json` (`ended_at: null` until the session ends). Every live response carries a bounded `curation_progress` count snapshot so the frontend can show final candidate/selected/rejected progress without polling the full collected-file inventory; it still refreshes that inventory after session end or manually. Key invariants:

`curation.py` is the deep policy module between candidate assembly and persistence. It consolidates overlapping absolute-time observations into event tracks, owns cooldown/class-balance/hard-budget sequencing, and exposes `evaluate` plus terminal record methods. Only selected candidates get an audio/metadata pair; repetitive, class-balance, session-budget, invalid-audio, and write-error outcomes are counted in `session.json`, while rejected details use one append-only `decisions.jsonl`. Default session caps are 600 files, 3,600 seconds (60 minutes), and 512 MiB estimated PCM; the repeat cooldown is 600 seconds. `session.json` is aggregate-only and atomically replaced, metadata JSON is the selected pair's discovery commit marker, and `segment_files.py` centralizes numeric index ordering for collection and GCS.

- **Privacy first**: any event whose label matches `COCHL_COLLECTION_EXCLUDE_LABEL_KEYWORDS` discards the chunk regardless of other events, forces a segment split, and flushes the context buffer — so neither a collected file nor its padding ever spans a speech region. Matching is case-insensitive taxonomy-token matching (for example `Male_speech`) with a deliberately small inflection alias table (`whisper` → `whispering`, `sing` → `singing`, etc.), never an arbitrary substring; `Reversing_beep` must not match `sing`.
- **Out-of-order tolerance**: chunk analyses complete out of order (up to `LIVE_MAX_IN_FLIGHT` concurrent), so entries sit in a reorder buffer and are only folded into segments once the watermark (max window end seen − `reorder_hold_back_sec`) passes them. `POST /api/live-session/end` (called by the frontend on 완료/폐기/unmount) flushes everything and returns a `LiveSessionEndResponse` summary.
- Collection failures must never fail the analyze response — the route wraps collection in try/except and returns `collection_status: null` and `curation_progress: null`.
- `LiveCollectionManager` (one per app, on `app.state`) keys collectors by *sanitized* session id (`saved_path.parent.name`) and finalizes stale sessions opportunistically.
- **No live leftovers**: ended sessions leave tombstones in the manager, so a chunk whose analysis completes after `end_session` is deleted and returns `discarded_late` instead of respawning a collector; the frontend gives each request a 60-second timeout, waits for all submitted request promises before calling end on 완료, then invalidates the session token; and `cleanup_orphan_live_chunks` (lifespan startup) removes `recordings/live/` entirely when collection is enabled. Provider/normalization failures also delete their staged WAV immediately while collection is enabled. Keep all of these intact when touching the live path.
- **Session naming**: an optional `session_name` form field (chunk + end requests) and collector-side `started_at`/`ended_at` timestamps flow into `session.json`, segment metadata JSONs, and `LiveSessionEndResponse`.
- **Management API**: `GET /api/collected-sessions` lists sessions from disk (`list_collected_sessions` reads segment JSONs, resolves the actual audio extension since MP3 conversion is async); `GET .../files/{filename}` serves audio/metadata. Session/segment `DELETE` returns 409 while `ended_at` is null. Deleting the last selected pair preserves the zero-segment curated session and historical aggregate until explicit session deletion. All paths are validated with `safe_collected_session_dir`.
### Backend (`backend/app/`)

- `main.py` — `create_app()` factory wires routes and app state, and (when `frontend/dist` exists) serves the built SPA with a path-traversal-guarded catch-all. Live chunks are (1) rate-limited to `LIVE_PROVIDER_MAX_CONCURRENCY=10` concurrent Cochl calls via an anyio `CapacityLimiter`, and (2) after analysis, routed into the collection pipeline (or, with collection disabled, converted to MP3 **asynchronously** on a bounded `ThreadPoolExecutor`: `LIVE_CONVERSION_MAX_WORKERS=2`, drop when `>= LIVE_CONVERSION_MAX_PENDING=32` pending; finalized collection segments reuse the same executor). Full-recording conversion and analysis run together in a worker thread behind `RECORDING_PROVIDER_MAX_CONCURRENCY=2`. Uploads stream from `UploadFile.file` to exclusive destination files in 1 MiB chunks so size limits do not require loading the entire file into memory. Live session ids are strictly validated rather than sanitized. Blocking Cochl SDK and ffmpeg calls never run on the event loop.
- `collection.py` — timeline ordering, context padding, WAV assembly, candidate/persistence sequencing, and rejected decision journal I/O. Pure stdlib (`wave`), no Cochl SDK; thread-safe via per-collector locks.
- `curation.py` — pure, stateful session selection policy and event-track consolidation; no Pydantic, filesystem, or Cochl dependency.
- `segment_files.py` — segment stem creation, numeric metadata ordering, and sibling audio resolution shared by collection and GCS.
- `cochl_provider.py` — the only module that imports the `cochl` SDK (imported lazily inside methods so tests can run without it). Routes live SED through `CochlLiveClient`, explicit SED-only files through the SDK `Client`, and multi-service files through `IntegratedApi`. Tests inject a fake provider through `app.state.provider_factory`.
- `cochl_live_client.py` — a minimal live-only client for the same dedicated SED session/chunk/result endpoints used by the pinned SDK. It owns thread-local persistent HTTPS connections, a total deadline plus per-socket timeouts, 100 ms pending-result polling, and strict response validation. Preserve its legacy-shaped result mapping because `cochl_provider.py` shares normalization with the SDK path.
- `config.py` — frozen `Settings` dataclass loaded from env via `Settings.from_env()`. `get_settings()` in `main.py` is `lru_cache`d, so **env changes require a process restart**. `validate_service_combination` enforces that audio insights requires both SED and speech analysis.
- `normalization.py` — defensively maps Cochl's raw (and variably-keyed) JSON into the typed response models. It tolerates alternate field names (`start_time_sec`/`start`, `class`/`label`/`name`, etc.) via `_first_present` — preserve this leniency when editing.
- `audio.py` — content-type/extension mapping, upload size validation, Finder-safe `ffmpeg` discovery, and WAV/MP3/Ogg conversion. Live provider transport may use a temporary sample-rate-preserving mono Ogg while the original collected 48 kHz WAV remains unchanged; unsupported standalone full-recording uploads are converted to 16 kHz mono WAV for Cochl; collected MP3 output is 44.1 kHz mono at 128 kbps. Missing `ffmpeg` raises `AudioConversionError` where conversion is required and makes the live provider path fall back to its original WAV.
- `models.py` — Pydantic response models (`AnalysisResponse`, `LiveChunkAnalysisResponse`, `SoundEvent`, etc.). Every successful live response includes upload/provider/normalization/collection/total stage timings in addition to the legacy `processing_time_ms` field.

The dashboard never creates a full-session recording. Explicit standalone uploads to `/api/analyze-recording` persist under `recordings/`. With collection enabled (default), meaningful live audio ends up in `recordings/collected/<session-id>/` and discarded chunks are deleted; with it disabled, every live chunk is kept under `recordings/live/<session-id>/` as MP3. Nothing else is auto-deleted.

### Frontend (`frontend/src/`)

`App.tsx` is the large orchestrator; most logic lives in small, individually-tested pure modules:

- `liveAudio.ts` — `LiveWindowBuffer` slices the mic stream into overlapping windows; `createLiveAudioCapture` wires the Web Audio graph, records the actual graph-start wall time used only to diagnose capture-clock drift, enforces the requested 48 kHz processing rate, and emits both analysis windows and visualization-only mel-spectrogram frames (2,048-point FFT, 64 bands from 50 Hz–16 kHz, fixed −95 to −25 dB normalization); `encodePcm16Wav` builds the unchanged 16-bit mono WAV blob sent to the backend.
- `liveChunkRecords.ts` — the per-chunk state machine. Each chunk is `PENDING | DETECTED | EMPTY | FAIL | SKIP` and carries latency fields; also derives the render geometry for the chunk timeline. Response tails use each window's actual emission callback as their zero point so audio-device/system-clock drift cannot accumulate into apparent API latency. The dashboard retains 60 minutes of completed rows and never evicts `PENDING` rows, while `App.tsx` keeps one compact non-rendered record per sequence in a map so the post-session diagnostic CSV covers the entire session.
- `liveTimeline.ts` — merges/lays out detected events into lanes, manages the scrolling viewport, and limits display-only event history to the same 60-minute window. This pruning never affects backend collection.
- `api.ts` — dashboard fetch calls (`analyzeLiveChunk`, `endLiveSession`, collected-session list/delete/file-URL helpers); error bodies are read from `{ detail }`. **User-facing strings are Korean** — match that when adding UI copy.
- `CollectedSessionsPanel.tsx` — self-contained 수집된 데이터 management panel (list, inline audio playback, confirm-guarded deletes). It refetches whenever the `refreshToken` prop changes; `App.tsx` bumps it after each session end and deliberately does not enable periodic full-list polling while recording. Expanded long sessions render the newest 100 segment rows first and reveal older rows in 100-item pages.
- `LiveSpectrogramPanel.tsx`, `time.ts`, `audioContext.ts` — mel-scaled canvas rendering with frequency guides and a continuous energy palette, shared timestamp formatting, and cross-browser AudioContext lookup. Live capture prefers `AudioWorklet` with a ScriptProcessor fallback for old WebViews; spectrogram frames live in a bounded, progressively compacted ref and viewport selection uses binary search.

**Frontend backpressure**: `handleLiveWindow` tracks in-flight requests in `liveInFlightRef` and drops a window as `SKIP` once `LIVE_MAX_IN_FLIGHT` is reached, rather than queuing. A `liveSessionTokenRef` guards every async continuation so stale responses from a previous recording are ignored. This is deliberate — keep new async live-path work token-guarded.

## Conventions

- Backend: `from __future__ import annotations`, dataclasses, and thorough test coverage per module. New Cochl-response fields go through `normalization.py`, never parsed inline in routes.
- The `cochl` SDK is imported lazily only in `cochl_provider.py`; keep it out of other modules so tests and normalization stay SDK-free.
- Node version: `.nvmrc` is the single exact version for reproducible builds. `scripts/build-macos-app.sh` reads and verifies it rather than duplicating the version; `package.json` keeps the wider supported runtime range. Use `nvm install && nvm use` to match `.nvmrc`.
- Python dependency policy: `pyproject.toml` declares compatible ranges and limits the Cochl SDK to tested major version 2; `backend/constraints.txt` pins CI/local direct dependencies. Use the constraints command above for verification and update the two files together after an upgrade review.
