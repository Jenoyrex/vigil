"""ClickHouse client construction.

A previous version of this function cached one client **per thread** (via
`threading.local`), to reuse the handshake the official `clickhouse-connect`
client performs at construction time. That caching assumed "the thread that
fetches the client" and "the thread that uses it" are always the same
thread -- true for a plain Python thread pool, but not for FastAPI: a sync
`Depends()` callable (this function's only caller, via
`get_..._repository()`) and the sync route handler that ultimately runs a
query are each dispatched to Starlette's thread pool *independently*
(`fastapi.dependencies.utils.solve_dependencies` and
`fastapi.routing.run_endpoint_function` each make their own
`run_in_threadpool` call), so nothing guarantees they land on the same OS
thread. A thread-cached client can end up handed to two different
concurrently-executing requests at once, and a `clickhouse_connect` `Client`
keeps per-session query state that isn't safe for that: "Attempt to execute
concurrent queries within the same session. Please use a separate client
instance per thread/process." -- reproduced under real concurrent dashboard
load (multiple parallel GETs against the same endpoint intermittently
returning this as a 500).

The fix is a fresh client per call, with no session at all
(`autogenerate_session_id=False`): every repository built from this client
(`analytics_repository.py`, `query_repository.py`,
`evaluations_query_repository.py`, and `repository.py`'s insert path) only
ever issues single, independent, parameterized statements -- no temporary
tables, no session-scoped `SET`, nothing that needs ClickHouse's session
concept at all. Trading the handshake-reuse optimization for correctness
here is deliberate: a wrong thread-affinity assumption that only fails under
load is worse than paying a per-call handshake, and nothing about the
request volume this API sees makes that cost material.
"""

from __future__ import annotations

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from app.config import settings


def get_clickhouse_client() -> Client:
    return clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        database=settings.clickhouse_database,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        connect_timeout=settings.clickhouse_timeout_seconds,
        send_receive_timeout=settings.clickhouse_timeout_seconds,
        autogenerate_session_id=False,
    )
