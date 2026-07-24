#!/usr/bin/env python3
"""Run the repo-backed macOS wrapper server on one reserved loopback socket."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import uvicorn


HOST = "127.0.0.1"
GRACEFUL_SHUTDOWN_SECONDS = 5
STARTUP_TIMEOUT_SECONDS = 10
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _become_process_group_leader() -> None:
    """Give the native wrapper a stable process group to signal on shutdown."""
    try:
        os.setpgid(0, 0)
    except OSError:
        if os.getpgrp() != os.getpid():
            raise


def _reserved_socket() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind((HOST, 0))
        listener.listen(2048)
        listener.set_inheritable(False)
    except BaseException:
        listener.close()
        raise
    return listener


def _probe_readiness(port: int) -> tuple[bool, str, bool]:
    """Return (ready, reason, terminal).

    Once Uvicorn is listening, an HTTP error from the dedicated readiness route
    represents configuration/storage state that cannot heal inside this process.
    Connection-level failures remain retryable until the startup deadline.
    """
    url = f"http://{HOST}:{port}/api/ready"
    try:
        # The URL is built from the fixed loopback HOST constant.
        with urlopen(  # nosec B310
            url,
            timeout=0.25,
        ) as response:
            payload = response.read().decode("utf-8", errors="replace")
            if response.status == 200:
                return True, "ready", False
            return False, f"HTTP {response.status}: {payload[:500]}", True
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
            detail = payload.get("detail") if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            detail = None
        reason = detail if detail is not None else body
        return False, f"HTTP {exc.code}: {str(reason)[:500]}", True
    except (TimeoutError, URLError, OSError) as exc:
        return False, str(exc), False


async def _stop_before_ready(
    server: uvicorn.Server,
    serve_task: asyncio.Task[None],
) -> None:
    server.should_exit = True
    try:
        await asyncio.wait_for(
            serve_task,
            timeout=GRACEFUL_SHUTDOWN_SECONDS,
        )
    except TimeoutError:
        serve_task.cancel()
        await asyncio.gather(serve_task, return_exceptions=True)


async def _serve(listener: socket.socket) -> int:
    port = int(listener.getsockname()[1])
    config = uvicorn.Config(
        "backend.app.main:app",
        host=HOST,
        log_level="info",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve(sockets=[listener]))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STARTUP_TIMEOUT_SECONDS
    last_readiness_error = "server has not started"

    while True:
        if serve_task.done():
            await serve_task
            raise RuntimeError("server exited before becoming ready")
        if server.started:
            is_ready, reason, terminal = await asyncio.to_thread(
                _probe_readiness, port
            )
            if is_ready:
                break
            last_readiness_error = reason
            if terminal:
                await _stop_before_ready(server, serve_task)
                raise RuntimeError(f"server readiness check failed ({reason})")
        if loop.time() >= deadline:
            await _stop_before_ready(server, serve_task)
            raise TimeoutError(
                "server did not become ready within 10 seconds "
                f"({last_readiness_error})"
            )
        await asyncio.sleep(0.1 if server.started else 0.025)

    print(
        f"Cochl.Sense Cloud Live Demo is running at http://{HOST}:{port}",
        flush=True,
    )
    await serve_task
    return 0


def main() -> int:
    _become_process_group_leader()
    with _reserved_socket() as listener:
        return asyncio.run(_serve(listener))


if __name__ == "__main__":
    try:
        exit_code = main()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:
        print(f"Cochl.Sense Cloud Live Demo error: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
    raise SystemExit(exit_code)
