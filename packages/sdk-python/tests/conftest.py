"""Shared test fixtures.

All HTTP is faked via `httpx.MockTransport` -- no real network access or
running Vigil API is required for this suite (see test_integration.py for
the one deliberate exception, which skips itself when no local API is up).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from vigil import Vigil


@dataclass
class RecordingTransport:
    """A fake httpx transport that records every request it sees and, by
    default, replies 200 OK. Set `handler` to script specific responses
    (or raise `httpx` exceptions) for retry/error-path tests."""

    handler: Callable[[httpx.Request], httpx.Response] | None = None
    requests: list[httpx.Request] = field(default_factory=list)
    bodies: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(json.loads(request.content))
        if self.handler is not None:
            return self.handler(request)
        accepted = len(self.bodies[-1]["spans"])
        return httpx.Response(200, json={"accepted": accepted, "request_id": "test"})


@pytest.fixture
def recording_transport() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture
def vigil_factory(recording_transport: RecordingTransport):
    """Builds `Vigil` clients wired to `recording_transport` instead of real
    network I/O, and closes every client it built at test teardown."""
    clients: list[Vigil] = []

    def _factory(**kwargs: Any) -> Vigil:
        kwargs.setdefault("api_key", "vgl_test1234.testsecret")
        kwargs.setdefault("base_url", "http://vigil.test")
        # Fast, near-zero backoff so retry tests don't sleep for real.
        kwargs.setdefault("retry_backoff_base", 0.001)
        kwargs.setdefault("retry_backoff_max", 0.01)
        client = Vigil(_transport=httpx.MockTransport(recording_transport), **kwargs)
        clients.append(client)
        return client

    yield _factory

    for client in clients:
        client.close()
