"""Request-size limiting and request-id correlation.

Both are pure ASGI middleware rather than Starlette's `BaseHTTPMiddleware`
-- `MaxBodySizeMiddleware` so it can reject an oversized request from its
`Content-Length` header alone, before FastAPI/Starlette ever buffers the
body into memory; `RequestIdMiddleware` because it only needs to wrap
`send`, not buffer or re-emit the response body the way
`BaseHTTPMiddleware` would.

`MaxBodySizeMiddleware` only covers the common case: a client that sends
`Content-Length` (any normal HTTP client posting a JSON body does). A
request using chunked transfer encoding has no `Content-Length` and passes
this check -- it is still bounded downstream by `TracesRequest.spans`'s
max-length validation, which caps the worst case. See docs/decisions/
003-clickhouse-telemetry-storage.md and apps/api/README.md for the "where
practical" scoping of this limit.
"""

from __future__ import annotations

import json
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logging_config import bind_request_id, reset_request_id


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


class RequestIdMiddleware:
    """Generates one `request_id` per HTTP request -- always server-side,
    never trusting a client-supplied header, since nothing about a log-
    correlation id benefits from being caller-defined and accepting one
    would mean logging attacker-controlled content with no validation --
    and makes it available for the lifetime of the request via
    `app.logging_config`'s contextvar, so every `logger.*` call anywhere in
    the request's call stack picks it up automatically without needing the
    id threaded through as an explicit argument (see that module's
    docstring for exactly why this is safe across Starlette's sync-route
    thread offload). Also echoes it back as an `X-Request-Id` response
    header, so a caller can report it back when asking about a specific
    failure -- `app/api/v1/traces.py`'s `POST /v1/traces` additionally
    returns the identical value in its JSON response body, unchanged from
    before this middleware existed (`app.logging_config.get_request_id()`
    now reads the same contextvar this middleware sets, rather than that
    route minting its own separate id).

    Registered LAST in app/main.py (`app.add_middleware(RequestIdMiddleware)`,
    after `CORSMiddleware`) so it becomes the OUTERMOST middleware --
    Starlette wraps in reverse registration order, the exact rule
    `app/main.py`'s own CORS-ordering comment documents -- meaning the id is
    bound before any other middleware or route code runs and is only reset
    once the whole response has finished sending.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())
        token = bind_request_id(request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            reset_request_id(token)
