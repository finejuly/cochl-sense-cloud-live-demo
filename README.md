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
- Live chunk debug files are saved under `recordings/live/<session-id>/`.
- If the browser records WebM, the backend tries to convert it to WAV with `ffmpeg`.

## License

MIT
