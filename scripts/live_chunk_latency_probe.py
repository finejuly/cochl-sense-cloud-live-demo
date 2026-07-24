#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor, as_completed, wait
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.cochl_provider import CochlProvider
from backend.app.config import Settings

DEFAULT_LOCAL_URL = "http://127.0.0.1:8000/api/analyze-live-chunk"


@dataclass
class ProbeRow:
    mode: str
    session_id: str
    sequence_id: int
    status: str
    window_start_sec: float
    window_end_sec: float
    scheduled_at_iso: str
    phase: str = "measurement"
    request_started_at_iso: str = ""
    response_received_at_iso: str = ""
    schedule_lag_ms: int | None = None
    window_end_delay_ms: int | None = None
    request_ms: int | None = None
    backend_ms: int | None = None
    backend_upload_ms: int | None = None
    backend_provider_ms: int | None = None
    backend_normalization_ms: int | None = None
    backend_collection_ms: int | None = None
    backend_total_ms: int | None = None
    submit_ms: int | None = None
    result_wait_ms: int | None = None
    in_flight_at_dispatch: int | None = None
    status_code: int | None = None
    event_count: int = 0
    detected_labels: str = ""
    error: str = ""


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    load_env_file(repo_root / ".env")

    audio_path = args.audio_path.expanduser().resolve()
    if not audio_path.is_file():
        print(f"Audio file not found: {audio_path}", file=sys.stderr)
        return 2
    if audio_path.suffix.lower() != ".wav":
        print(
            f"Expected a WAV file for live chunk parity, got: {audio_path.name}",
            file=sys.stderr,
        )
        return 2

    session_id = args.session_id or f"latency-probe-{int(time.time())}"
    runner = (
        DirectCochlRunner(audio_path, args.timeout)
        if args.mode == "direct"
        else LocalApiRunner(audio_path, args.url, args.timeout)
    )

    print(
        "Starting probe: "
        f"mode={args.mode} measured={args.count} warmup={args.warmup_count} "
        f"interval={args.interval_sec}s "
        f"max_in_flight={args.max_in_flight} audio={audio_path}"
    )
    rows: list[ProbeRow] = []
    finish_error = ""
    try:
        rows = run_schedule(args, runner, session_id)
    finally:
        try:
            runner.finish(session_id)
        except Exception as exc:
            finish_error = str(exc)
            print(f"Could not finalize probe session: {finish_error}", file=sys.stderr)
    rows.sort(key=lambda row: row.sequence_id)

    csv_path = args.csv_path
    if csv_path:
        write_csv(csv_path.expanduser().resolve(), rows)
        print(f"Wrote CSV: {csv_path.expanduser().resolve()}")

    print_summary(rows)
    return 0 if not finish_error and all(row.status == "OK" for row in rows) else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Send the same live chunk WAV on a fixed schedule and record latency. "
            "Use --mode direct for the same Cochl live-provider path as the backend, "
            "or --mode local to hit the local FastAPI endpoint."
        )
    )
    parser.add_argument("audio_path", type=Path, help="WAV file to send repeatedly.")
    parser.add_argument(
        "--mode",
        choices=("direct", "local"),
        default="local",
        help="direct calls the backend's Cochl live provider; local posts to /api/analyze-live-chunk.",
    )
    parser.add_argument("--url", default=DEFAULT_LOCAL_URL, help="Local API URL for --mode local.")
    parser.add_argument(
        "--count",
        type=int,
        default=60,
        help="Number of measured chunks, excluding warmup chunks.",
    )
    parser.add_argument(
        "--warmup-count",
        type=int,
        default=0,
        help="Number of scheduled warmup chunks excluded from summary statistics.",
    )
    parser.add_argument(
        "--interval-sec",
        type=float,
        default=1.0,
        help="Seconds between scheduled chunk requests.",
    )
    parser.add_argument(
        "--window-sec",
        type=float,
        default=2.0,
        help="Reported live chunk window length in seconds.",
    )
    parser.add_argument(
        "--max-in-flight",
        type=int,
        default=10,
        help="Maximum concurrent requests before recording SKIP.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout in seconds for --mode local.",
    )
    parser.add_argument("--session-id", default="", help="Session id sent to local API.")
    parser.add_argument("--csv", dest="csv_path", type=Path, help="Optional CSV output path.")
    args = parser.parse_args()
    if args.count <= 0:
        parser.error("--count must be positive")
    if args.warmup_count < 0:
        parser.error("--warmup-count must be zero or positive")
    if args.interval_sec <= 0:
        parser.error("--interval-sec must be positive")
    if args.window_sec <= 0:
        parser.error("--window-sec must be positive")
    if args.max_in_flight <= 0:
        parser.error("--max-in-flight must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def run_schedule(args: argparse.Namespace, runner: "ProbeRunner", session_id: str) -> list[ProbeRow]:
    rows: list[ProbeRow] = []
    pending: dict[Future[ProbeRow], str] = {}
    start_monotonic = time.monotonic()
    start_wall = time.time()
    total_count = args.warmup_count + args.count

    with ThreadPoolExecutor(max_workers=args.max_in_flight) as executor:
        for sequence_id in range(1, total_count + 1):
            scheduled_monotonic = start_monotonic + ((sequence_id - 1) * args.interval_sec)
            scheduled_wall = start_wall + ((sequence_id - 1) * args.interval_sec)
            sleep_until(scheduled_monotonic)
            drain_completed(pending, rows)

            window_start_sec = (sequence_id - 1) * args.interval_sec
            window_end_sec = window_start_sec + args.window_sec
            scheduled_at_iso = iso_from_epoch(scheduled_wall)
            phase = "warmup" if sequence_id <= args.warmup_count else "measurement"

            in_flight = len(pending)
            if in_flight >= args.max_in_flight:
                row = ProbeRow(
                    mode=args.mode,
                    session_id=session_id,
                    sequence_id=sequence_id,
                    status="SKIP",
                    window_start_sec=window_start_sec,
                    window_end_sec=window_end_sec,
                    scheduled_at_iso=scheduled_at_iso,
                    phase=phase,
                    in_flight_at_dispatch=in_flight,
                    error=f"max_in_flight={args.max_in_flight}",
                )
                rows.append(row)
                print_row(row)
                continue

            future = executor.submit(
                runner.send,
                session_id,
                sequence_id,
                window_start_sec,
                window_end_sec,
                scheduled_wall,
                scheduled_at_iso,
                in_flight,
            )
            pending[future] = phase

        for future in as_completed(pending):
            row = future.result()
            row.phase = pending[future]
            rows.append(row)
            print_row(row)

    return rows


def drain_completed(pending: dict[Future[ProbeRow], str], rows: list[ProbeRow]) -> None:
    if not pending:
        return
    done, _ = wait(pending, timeout=0)
    for future in done:
        phase = pending.pop(future)
        row = future.result()
        row.phase = phase
        rows.append(row)
        print_row(row)


def sleep_until(target_monotonic: float) -> None:
    remaining = target_monotonic - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


class ProbeRunner:
    def send(
        self,
        session_id: str,
        sequence_id: int,
        window_start_sec: float,
        window_end_sec: float,
        scheduled_wall: float,
        scheduled_at_iso: str,
        in_flight_at_dispatch: int,
    ) -> ProbeRow:
        raise NotImplementedError

    def finish(self, session_id: str) -> None:
        return None


class DirectCochlRunner(ProbeRunner):
    def __init__(self, audio_path: Path, timeout: float):
        self.audio_path = audio_path
        settings = Settings.from_env()
        if not settings.cochl_project_key:
            raise SystemExit("COCHL_PROJECT_KEY is required for --mode direct.")
        self.provider = CochlProvider(
            replace(settings, cochl_live_timeout_sec=timeout)
        )

    def send(
        self,
        session_id: str,
        sequence_id: int,
        window_start_sec: float,
        window_end_sec: float,
        scheduled_wall: float,
        scheduled_at_iso: str,
        in_flight_at_dispatch: int,
    ) -> ProbeRow:
        row = ProbeRow(
            mode="direct",
            session_id=session_id,
            sequence_id=sequence_id,
            status="ERROR",
            window_start_sec=window_start_sec,
            window_end_sec=window_end_sec,
            scheduled_at_iso=scheduled_at_iso,
            in_flight_at_dispatch=in_flight_at_dispatch,
        )
        request_start_wall = time.time()
        request_start = time.perf_counter()
        row.request_started_at_iso = iso_from_epoch(request_start_wall)
        row.schedule_lag_ms = round_ms((request_start_wall - scheduled_wall) * 1000)
        try:
            result = self.provider.analyze_live_chunk(self.audio_path)

            response_wall = time.time()
            row.response_received_at_iso = iso_from_epoch(response_wall)
            row.request_ms = round_ms((time.perf_counter() - request_start) * 1000)
            row.window_end_delay_ms = round_ms(
                (response_wall - scheduled_wall) * 1000
            )
            row.status = "OK"
            labels = labels_from_raw_result(result)
            row.event_count = len(labels)
            row.detected_labels = "; ".join(labels)
        except Exception as exc:
            response_wall = time.time()
            row.response_received_at_iso = iso_from_epoch(response_wall)
            row.request_ms = round_ms((time.perf_counter() - request_start) * 1000)
            row.window_end_delay_ms = round_ms(
                (response_wall - scheduled_wall) * 1000
            )
            row.error = str(exc)
        return row


class LocalApiRunner(ProbeRunner):
    def __init__(self, audio_path: Path, url: str, timeout: float):
        self.audio_path = audio_path
        self.audio_bytes = audio_path.read_bytes()
        self.url = validated_http_url(url)
        self.end_url = live_session_end_url(self.url)
        self.timeout = timeout

    def send(
        self,
        session_id: str,
        sequence_id: int,
        window_start_sec: float,
        window_end_sec: float,
        scheduled_wall: float,
        scheduled_at_iso: str,
        in_flight_at_dispatch: int,
    ) -> ProbeRow:
        row = ProbeRow(
            mode="local",
            session_id=session_id,
            sequence_id=sequence_id,
            status="ERROR",
            window_start_sec=window_start_sec,
            window_end_sec=window_end_sec,
            scheduled_at_iso=scheduled_at_iso,
            in_flight_at_dispatch=in_flight_at_dispatch,
        )
        request_start_wall = time.time()
        request_start = time.perf_counter()
        row.request_started_at_iso = iso_from_epoch(request_start_wall)
        row.schedule_lag_ms = round_ms((request_start_wall - scheduled_wall) * 1000)

        fields = {
            "session_id": session_id,
            "sequence_id": str(sequence_id),
            "window_start_sec": f"{window_start_sec:.3f}",
            "window_end_sec": f"{window_end_sec:.3f}",
        }

        try:
            body, content_type = multipart_body(
                fields,
                file_field="file",
                filename=(
                    f"chunk-{sequence_id:06d}-{window_start_sec:.3f}-{window_end_sec:.3f}.wav"
                ),
                file_content_type="audio/wav",
                file_bytes=self.audio_bytes,
            )
            request = urllib.request.Request(
                self.url,
                data=body,
                headers={
                    "Content-Type": content_type,
                    "Content-Length": str(len(body)),
                },
                method="POST",
            )
            # LocalApiRunner validates the URL at initialization.
            with urllib.request.urlopen(  # nosec B310
                request,
                timeout=self.timeout,
            ) as response:
                payload = response.read()
                row.status_code = response.status

            response_wall = time.time()
            row.response_received_at_iso = iso_from_epoch(response_wall)
            row.request_ms = round_ms((time.perf_counter() - request_start) * 1000)
            row.window_end_delay_ms = round_ms(
                (response_wall - scheduled_wall) * 1000
            )

            data = json.loads(payload.decode("utf-8"))
            row.status = "OK"
            row.backend_ms = maybe_int(data.get("processing_time_ms"))
            timings = data.get("timings")
            if isinstance(timings, dict):
                row.backend_upload_ms = maybe_int(timings.get("upload_ms"))
                row.backend_provider_ms = maybe_int(timings.get("provider_ms"))
                row.backend_normalization_ms = maybe_int(
                    timings.get("normalization_ms")
                )
                row.backend_collection_ms = maybe_int(timings.get("collection_ms"))
                row.backend_total_ms = maybe_int(timings.get("total_ms"))
            labels = labels_from_local_response(data)
            row.event_count = len(labels)
            row.detected_labels = "; ".join(labels)
        except urllib.error.HTTPError as exc:
            row.status_code = exc.code
            response_wall = time.time()
            row.response_received_at_iso = iso_from_epoch(response_wall)
            row.request_ms = round_ms((time.perf_counter() - request_start) * 1000)
            row.window_end_delay_ms = round_ms(
                (response_wall - scheduled_wall) * 1000
            )
            row.error = decode_error_body(exc)
        except Exception as exc:
            response_wall = time.time()
            row.response_received_at_iso = iso_from_epoch(response_wall)
            row.request_ms = round_ms((time.perf_counter() - request_start) * 1000)
            row.window_end_delay_ms = round_ms(
                (response_wall - scheduled_wall) * 1000
            )
            row.error = str(exc)
        return row

    def finish(self, session_id: str) -> None:
        body = urllib.parse.urlencode({"session_id": session_id}).encode("utf-8")
        request = urllib.request.Request(
            self.end_url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        # LocalApiRunner validates the URL at initialization.
        with urllib.request.urlopen(  # nosec B310
            request,
            timeout=self.timeout,
        ) as response:
            response.read()
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(
                    f"Live session finalization returned HTTP {response.status}."
                )


def live_session_end_url(analyze_url: str) -> str:
    parsed = urllib.parse.urlsplit(analyze_url)
    api_root = parsed.path.rstrip("/").rsplit("/", 1)[0]
    end_path = f"{api_root}/live-session/end"
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, end_path, "", "")
    )


def validated_http_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(
            "Local API URL must use HTTP(S), include a host, and omit credentials."
        )
    return url


def multipart_body(
    fields: dict[str, str],
    *,
    file_field: str,
    filename: str,
    file_content_type: str,
    file_bytes: bytes,
) -> tuple[bytes, str]:
    boundary = f"----cochl-sense-cloud-live-demo-probe-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {file_content_type}\r\n\r\n".encode("utf-8"),
            file_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def labels_from_raw_result(raw_result: dict[str, Any]) -> list[str]:
    service = raw_result.get("sound_event_detection")
    if service is None:
        service = raw_result.get("sense")
    if not isinstance(service, dict):
        raise ValueError("Cochl response is missing sound-event results.")
    if service.get("error") or str(service.get("status", "")).lower() in {
        "error",
        "fail",
        "failed",
        "failure",
    }:
        detail = service.get("error") or service.get("status")
        raise ValueError(f"Cochl sound-event analysis failed: {detail}")
    chunks = service.get("results")
    if chunks is None:
        chunks = service.get("events")
    if not isinstance(chunks, list):
        raise ValueError("Cochl sound-event results are invalid.")

    labels: list[str] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        classes = chunk.get("classes") or chunk.get("labels") or []
        if not isinstance(classes, list):
            classes = [classes]
        for item in classes:
            if not isinstance(item, dict):
                continue
            label = item.get("class") or item.get("label") or item.get("name")
            if label:
                confidence = (
                    item.get("confidence")
                    if item.get("confidence") is not None
                    else item.get("score")
                )
                labels.append(label_with_confidence(str(label), confidence))
    return labels


def labels_from_local_response(data: dict[str, Any]) -> list[str]:
    events = data.get("sound_events")
    if not isinstance(events, list):
        return []
    labels: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        label = event.get("label")
        if label:
            labels.append(label_with_confidence(str(label), event.get("confidence")))
    return labels


def label_with_confidence(label: str, confidence: Any) -> str:
    try:
        return f"{label} {float(confidence) * 100:.0f}%"
    except (TypeError, ValueError):
        return label


def decode_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8")
        data = json.loads(body)
        detail = data.get("detail")
        if detail:
            return str(detail)
        return body
    except Exception:
        return str(exc)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def write_csv(path: Path, rows: list[ProbeRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(asdict(rows[0]).keys()) if rows else list(ProbeRow.__dataclass_fields__)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def print_row(row: ProbeRow) -> None:
    if row.status == "OK":
        backend = f" backend={row.backend_ms}ms" if row.backend_ms is not None else ""
        backend_total = (
            f" backend_total={row.backend_total_ms}ms"
            if row.backend_total_ms is not None
            else ""
        )
        submit = f" submit={row.submit_ms}ms wait={row.result_wait_ms}ms" if row.submit_ms is not None else ""
        print(
            f"{row.sequence_id:04d} {row.phase.upper()} OK "
            f"window_end={row.window_end_delay_ms}ms request={row.request_ms}ms"
            f"{backend}{backend_total}{submit} "
            f"in_flight={row.in_flight_at_dispatch} events={row.event_count}"
        )
    elif row.status == "SKIP":
        print(f"{row.sequence_id:04d} SKIP in_flight={row.in_flight_at_dispatch}")
    else:
        print(
            f"{row.sequence_id:04d} ERROR request={row.request_ms}ms "
            f"status={row.status_code} error={row.error}"
        )


def print_summary(rows: list[ProbeRow]) -> None:
    measured_rows = [row for row in rows if row.phase == "measurement"]
    warmup_rows = [row for row in rows if row.phase == "warmup"]
    ok_rows = [
        row
        for row in measured_rows
        if row.status == "OK" and row.request_ms is not None
    ]
    errors = [row for row in measured_rows if row.status == "ERROR"]
    skips = [row for row in measured_rows if row.status == "SKIP"]

    print("\nSummary")
    print(
        f"  measured={len(measured_rows)} warmup={len(warmup_rows)} "
        f"ok={len(ok_rows)} skip={len(skips)} error={len(errors)}"
    )
    print_latency_stats(
        "window_end_delay_ms",
        [row.window_end_delay_ms for row in ok_rows],
    )
    print_latency_stats("request_ms", [row.request_ms for row in ok_rows])
    print_latency_stats("backend_ms", [row.backend_ms for row in ok_rows])
    print_latency_stats(
        "backend_provider_ms", [row.backend_provider_ms for row in ok_rows]
    )
    print_latency_stats(
        "backend_collection_ms", [row.backend_collection_ms for row in ok_rows]
    )
    print_latency_stats(
        "backend_total_ms", [row.backend_total_ms for row in ok_rows]
    )
    print_latency_stats("submit_ms", [row.submit_ms for row in ok_rows])
    print_latency_stats("result_wait_ms", [row.result_wait_ms for row in ok_rows])

    request_slope = slope_ms_per_request(ok_rows, "request_ms")
    backend_slope = slope_ms_per_request(ok_rows, "backend_ms")
    if request_slope is not None:
        print(f"  request_ms slope={request_slope:.1f} ms/request")
    if backend_slope is not None:
        print(f"  backend_ms slope={backend_slope:.1f} ms/request")

    window_end_delays = [
        row.window_end_delay_ms
        for row in ok_rows
        if row.window_end_delay_ms is not None
    ]
    if window_end_delays:
        crossings = sum(value >= 2_000 for value in window_end_delays)
        print(
            f"  window_end_delay_ms >=2000ms: {crossings}/{len(window_end_delays)} "
            f"({crossings / len(window_end_delays):.1%})"
        )


def print_latency_stats(name: str, values: list[int | None]) -> None:
    samples = sorted(value for value in values if value is not None)
    if not samples:
        return
    p50 = percentile(samples, 50)
    p95 = percentile(samples, 95)
    print(
        f"  {name}: min={samples[0]}ms p50={p50}ms p95={p95}ms max={samples[-1]}ms"
    )


def slope_ms_per_request(rows: list[ProbeRow], field: str) -> float | None:
    points = [
        (row.sequence_id, getattr(row, field))
        for row in rows
        if getattr(row, field) is not None
    ]
    if len(points) < 2:
        return None
    mean_x = sum(point[0] for point in points) / len(points)
    mean_y = sum(point[1] for point in points) / len(points)
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    if denominator == 0:
        return None
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in points)
    return numerator / denominator


def percentile(samples: list[int], pct: int) -> int:
    if not samples:
        return 0
    index = round((pct / 100) * (len(samples) - 1))
    return samples[index]


def maybe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def round_ms(value: float) -> int:
    return int(round(value))


def iso_from_epoch(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat(timespec="milliseconds")


if __name__ == "__main__":
    raise SystemExit(main())
