"""Liveness heartbeat for `WorkerRuntime.run()` and `Poller.run()`
(Phase 4D, F6) -- a file whose mtime is refreshed once per completed loop
iteration, checked by `services/worker/Dockerfile`'s `HEALTHCHECK`
instruction.

Deliberately not an HTTP endpoint: neither process runs a server (see
`services/worker/Dockerfile`'s own comment), and adding one solely to
satisfy a healthcheck would be new exposed network surface for zero other
benefit. A heartbeat file needs no listener and no port, and -- because it
is written from inside the exact loop it certifies, once per iteration --
a genuinely stuck loop (one that never completes another iteration) simply
stops updating it. That is the entire point: this must distinguish "the
process is running" (all Docker's default liveness, and everything this
image had before this, could ever say) from "the process is actually
making progress."

Phase 4D's F5 fix (worker/postgres/client.py's `connect_timeout`/
`statement_timeout`) is what makes this heartbeat meaningful rather than
cosmetic: every I/O call inside one loop iteration -- PostgreSQL (F5),
ClickHouse (`clickhouse_timeout_seconds`, pre-existing), the evaluator call
itself (`evaluator_call_timeout_seconds`/`evaluator_init_timeout_seconds`,
Phase 4A), and the poller's HTTP call to apps/api
(`poller_job_creation_timeout_seconds`, pre-existing) -- now has *some*
upper bound, so one iteration completing (and this heartbeat refreshing)
within a generous, bounded window is a real signal, not a guess.

`HEARTBEAT_PATH` is under `/tmp` deliberately: `/tmp` is world-writable
(mode 1777) in the `python:3.12-slim-bookworm` base image by design, so the
non-root `vigil` user (`services/worker/Dockerfile`) can write here with no
additional `chown`/directory-creation step -- unlike the FastEmbed cache
directory that same Dockerfile's own Phase 4C review had to fix explicitly
for exactly the opposite reason. This exact literal path is duplicated
(not imported -- a `HEALTHCHECK CMD` runs as its own, separate process) into
that Dockerfile's `HEALTHCHECK` instruction; if this constant ever changes,
that instruction must change with it.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

HEARTBEAT_PATH = Path("/tmp/vigil-worker-heartbeat")


def touch_heartbeat() -> None:
    """Refresh the heartbeat file's mtime (creating it on first call).
    Never raises: a failure to write a liveness signal must never itself be
    treated as a reason to crash the loop it's meant to describe. If `/tmp`
    is ever unwritable, the correct outcome is that the healthcheck
    eventually reports unhealthy -- not a `WorkerRuntime`/`Poller` crash.
    """
    with contextlib.suppress(OSError):
        HEARTBEAT_PATH.touch()
