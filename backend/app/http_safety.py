from __future__ import annotations

import ipaddress
import secrets
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MEBIBYTE = 1024 * 1024
MULTIPART_OVERHEAD_BYTES = 256 * 1024
LIVE_REQUEST_MAX_BYTES = 2 * MEBIBYTE
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "base-uri 'none'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "script-src 'self' blob:",
        "style-src 'self' 'unsafe-inline'",
        "connect-src 'self'",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        "worker-src 'self' blob:",
        "font-src 'self' data:",
    )
)
SECURITY_RESPONSE_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Reject upload bodies while they are being received, before multipart spooling."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings_resolver: Callable[[Scope], Any],
    ) -> None:
        self.app = app
        self.settings_resolver = settings_resolver

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self._limit_for(scope)
        if limit is None:
            await self.app(scope, receive, send)
            return

        content_length = _content_length(scope)
        if content_length is not None and content_length > limit:
            await _send_too_large(scope, receive, send, limit)
            return

        received = 0
        response_started = False
        limit_exceeded = False

        async def limited_receive() -> Message:
            nonlocal received, limit_exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    limit_exceeded = True
                    raise RequestBodyTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if limit_exceeded:
                # FastAPI/Starlette may translate receive errors into a generic
                # 400 response. Replace that response at this outer ASGI layer
                # so chunked requests receive the same accurate 413 as requests
                # with a Content-Length header.
                if not response_started:
                    response_started = True
                    await _send_too_large(scope, receive, send, limit)
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestBodyTooLarge:
            if response_started:
                return
            await _send_too_large(scope, receive, send, limit)

    def _limit_for(self, scope: Scope) -> int | None:
        path = scope.get("path", "")
        if path not in {"/api/analyze-live-chunk", "/api/analyze-recording"}:
            return None
        settings = self.settings_resolver(scope)
        configured = int(settings.max_upload_mb) * MEBIBYTE + MULTIPART_OVERHEAD_BYTES
        if path == "/api/analyze-live-chunk":
            return min(configured, LIVE_REQUEST_MAX_BYTES)
        return configured


class LocalAccessMiddleware:
    """Block cross-site browser writes to the loopback-only application API."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = _headers(scope)
        origin = headers.get("origin")
        fetch_site = headers.get("sec-fetch-site", "").lower()
        if origin and not _is_loopback_origin(origin):
            await _send_forbidden(scope, receive, send, "Cross-origin request rejected.")
            return
        if fetch_site == "cross-site":
            await _send_forbidden(scope, receive, send, "Cross-site request rejected.")
            return

        method = scope.get("method", "GET").upper()
        is_browser_request = origin is not None or bool(fetch_site)
        if method in UNSAFE_METHODS and is_browser_request:
            supplied = headers.get("x-cochl-local-token", "")
            expected = getattr(scope["app"].state, "api_token", "")
            if not supplied or not expected or not secrets.compare_digest(supplied, expected):
                await _send_forbidden(
                    scope,
                    receive,
                    send,
                    "Missing or invalid local API token.",
                )
                return

        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    """Attach browser hardening headers to API, static, and error responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in SECURITY_RESPONSE_HEADERS.items():
                    headers[key] = value
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


def _headers(scope: Scope) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def _content_length(scope: Scope) -> int | None:
    value = _headers(scope).get("content-length")
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return max(0, parsed)


def _is_loopback_origin(origin: str) -> bool:
    try:
        hostname = urlsplit(origin).hostname
    except ValueError:
        return False
    if not hostname:
        return False
    hostname = hostname.lower()
    if hostname in {"localhost", "testserver"}:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


async def _send_too_large(
    scope: Scope,
    receive: Receive,
    send: Send,
    limit: int,
) -> None:
    response = JSONResponse(
        {"detail": f"Request body exceeds the {limit} byte limit."},
        status_code=413,
    )
    await response(scope, receive, send)


async def _send_forbidden(
    scope: Scope,
    receive: Receive,
    send: Send,
    detail: str,
) -> None:
    response = JSONResponse({"detail": detail}, status_code=403)
    await response(scope, receive, send)
