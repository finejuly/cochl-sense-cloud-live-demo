# Cochl.Sense Cloud Live Demo

A local demo app for streaming microphone audio to the Cochl.Sense Cloud API and monitoring live sound-event results. It records microphone input in the browser, sends short live chunks to a FastAPI backend, and shows detected events, spectrogram frames, request states, and latency metrics in a dashboard.

## Project Structure

- `frontend/`: React + Vite live dashboard
- `backend/`: FastAPI proxy for Cochl.Sense Cloud API calls
- `macos/`: macOS wrapper that opens the local server and web UI in one app window
- `scripts/live_chunk_latency_probe.py`: CLI probe for measuring live chunk latency

## Requirements

- Python 3.10 or later
- Node.js 20.19 or later
- Cochl project key
- Optional: `ffmpeg` for WebM conversion
- Xcode Command Line Tools for building the macOS wrapper

## Setup

```bash
/opt/homebrew/bin/python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e "backend[dev]"
cp .env.example .env
```

Set `COCHL_PROJECT_KEY` in `.env`.

## Run Locally

Start the backend:

```bash
.venv/bin/uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

Start the frontend:

```bash
cd frontend
nvm use
npm install
npm run dev
```

The Vite dev server proxies `/api` requests to `http://127.0.0.1:8000`.

## Live Dashboard

While recording, the frontend uses Web Audio to create 2-second WAV chunks and sends them to `/api/analyze-live-chunk` every second. The dashboard displays the live spectrogram, detected event markers, per-chunk states (`PENDING`, `DETECTED`, `EMPTY`, `FAIL`, `SKIP`), and request/server/window latency.

After recording, the full audio file can be analyzed through `/api/analyze-recording` and shown as a separate result timeline. Live chunk records can also be exported as CSV for latency analysis.

## Data Collection

While streaming, the backend collects only the chunks that contain meaningful sound:

- Chunks whose events all fall below `COCHL_COLLECTION_CONFIDENCE_THRESHOLD` (or that have no events) are treated as silence and deleted.
- Chunks containing privacy-sensitive labels (`COCHL_COLLECTION_EXCLUDE_LABEL_KEYWORDS`, e.g. speech, whispering, singing) are deleted, and a collected segment never spans across a speech region.
- Kept chunks are merged (window overlap removed) into contiguous segments of at most `COCHL_COLLECTION_MAX_SEGMENT_SEC` (default 20 s) so each file stays a context-sized unit.
- Segments shorter than `COCHL_COLLECTION_MIN_SEGMENT_SEC` (default 5 s) are padded with surrounding silent chunks on both sides: leading pre-roll is capped at half the deficit so trailing background fills the rest and the detection sits roughly centered (the front is topped up if the recording ends before enough trailing audio arrives). Padding never crosses a speech boundary.
- Segment files are written **in real time**: once silence stretches `COCHL_COLLECTION_SILENCE_CLOSE_SEC` (default 3 s) past the last detection, the open segment is finalized immediately — no need to wait for the recording to end. Brief one-window lulls do not split an ongoing sound. `session.json` is refreshed on every finalize, and the dashboard's 수집된 데이터 panel auto-refreshes every 5 s while recording so new files appear live.
- Segments are saved under `recordings/collected/<session-id>/` as audio (`segment-XXX-<start>-<end>.wav`, converted to MP3 when `ffmpeg` is available) plus a metadata JSON with the detected events, chunk sequence ids, timing, session name, and timestamps. A `session.json` summary (name, started/ended timestamps, stats) is written when the session ends.
- `recordings/live/` is only a staging area while collection is enabled: chunks are deleted or merged as they are classified, the frontend waits for in-flight analyses to drain before ending the session, late responses for ended sessions are discarded (tombstones), and any orphans from a crashed process are removed at server startup.

An optional session name can be entered before recording; it is stored in all collection metadata alongside the recording date/time. The dashboard shows live counts of collected/excluded chunks during recording, and a collection summary (name, date, segments, durations, labels) after pressing 완료, which calls `POST /api/live-session/end`.

Collected data can be browsed and managed in the 수집된 데이터 panel at the bottom of the dashboard: sessions are listed with their name, date, and segments; each segment can be played inline, and segments or whole sessions can be deleted. The backing endpoints are `GET /api/collected-sessions`, `GET /api/collected-sessions/{id}/files/{filename}`, and `DELETE /api/collected-sessions/{id}[/segments/{filename}]`.

Set `COCHL_COLLECTION_ENABLED=false` to restore the previous behavior of keeping every live chunk as MP3 debug files.

## macOS App

```bash
scripts/build-macos-app.sh
open CochlSenseCloudLiveDemo.app
```

## Tests

```bash
.venv/bin/python -m pytest backend/tests -v
cd frontend
npm test -- --run
npm run build
```

## Notes

- Recordings are saved under `recordings/` and are not deleted automatically.
- Collected live segments are saved under `recordings/collected/<session-id>/`; discarded (silent/speech) live chunks are deleted.
- With `COCHL_COLLECTION_ENABLED=false`, live chunk debug files are kept under `recordings/live/<session-id>/` instead.
- If the browser records WebM, the backend tries to convert it to WAV with `ffmpeg`.

## License

MIT
