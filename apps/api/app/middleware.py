"""Request-size limiting.

A pure ASGI middleware rather than Starlette's `BaseHTTPMiddleware`, so it
can reject an oversized request from its `Content-Length` header alone,
before FastAPI/Starlette ever buffers the body into memory.

This only covers the common case: a client that sends `Content-Length` (any
normal HTTP client posting a JSON body does). A request using chunked
transfer encoding has no `Content-Length` and passes this check -- it is
still bounded downstream by `TracesRequest.spans`'s max-length validation,
which caps the worst case. See docs/decisions/003-clickhouse-telemetry-storage.md
and apps/api/README.md for the "where practical" scoping of this limit.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Receive, Scope, Send


class MaxBodySizeMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = next(
            (value for key, value in scope.get("headers", []) if key == b"content-length"),
            None,
        )
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = None
            if declared_size is not None and declared_size > self.max_body_bytes:
                await _send_413(send, self.max_body_bytes)
                return

        await self.app(scope, receive, send)


async def _send_413(send: Send, max_body_bytes: int) -> None:
    body = json.dumps(
        {"detail": f"Request body exceeds the maximum allowed size of {max_body_bytes} bytes."}
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})
