"""Data-access layer for the poller's checkpoint
(`evaluation_poller_checkpoint`'s singleton row) -- Phase 3H,
docs/decisions/005-evaluation-job-storage-worker.md sections 4/7 and the
Phase 3H amendment.

Raw `psycopg`, one connection per call passed in by the caller, mirroring
`worker/postgres/repository.py`'s exact conventions (ADR 001 decision 6:
duplicate rather than centralize -- `apps/api/app/db/models/
evaluation_poller_checkpoint.py`'s own model docstring already documents
this table as worker-owned lifecycle state, read/written directly, never
through `apps/api`'s job-creation endpoint).

`advance_checkpoint`'s `UPDATE` is an optimistic compare-and-swap, the same
fencing shape `worker/postgres/repository.py`'s `mark_failed`/
`mark_dead_letter` already use for `evaluation_jobs` rows: it only takes
effect if `last_ingested_at` still equals whatever this caller last
observed. A `False` return means some other poller instance already
advanced the checkpoint past what this caller saw -- not an error, never
raised or retried as a statement; the caller simply proceeds to its next
tick and re-reads whatever the checkpoint now is. This is the entire
concurrency story for running multiple poller processes: no distributed
lock, no `SELECT ... FOR UPDATE` held across a tick -- just this one
single-statement CAS, exactly like every other fenced transition in this
codebase. See `worker/poller.py`.
"""

from __future__ import annotations

from datetime import datetime

# The one well-known row id evaluation_poller_checkpoint is ever expected to
# hold -- matches apps/api/app/db/models/evaluation_poller_checkpoint.py's
# SINGLETON_ROW_ID exactly (duplicated as a literal here rather than
# imported, per this module's own docstring: services/worker must not
# depend on apps/api).
_CHECKPOINT_ID = "global"

_ENSURE_CHECKPOINT_ROW_SQL = """
    INSERT INTO evaluation_poller_checkpoint (id, last_ingested_at)
    VALUES (%(id)s, NULL)
    ON CONFLICT (id) DO NOTHING
"""

_GET_CHECKPOINT_SQL = """
    SELECT last_ingested_at
    FROM evaluation_poller_checkpoint
    WHERE id = %(id)s
"""

_ADVANCE_CHECKPOINT_SQL = """
    UPDATE evaluation_poller_checkpoint
    SET last_ingested_at = %(new_watermark)s,
        updated_at = now()
    WHERE id = %(id)s
      AND (
          last_ingested_at = %(observed_watermark)s
          OR (
              %(observed_watermark)s IS NULL
              AND last_ingested_at IS NULL
          )
      )
"""


class PollerCheckpointRepository:
    def __init__(self, connection) -> None:
        self._connection = connection

    def get_or_create_checkpoint(self) -> datetime | None:
        """The current `last_ingested_at` watermark, or `None` if no span
        has ever been successfully scanned past yet -- treated as "start
        from the beginning of the retention window"
        (`evaluation_poller_checkpoint.py`'s own documented semantics for a
        fresh/absent row). Creates the singleton row on the very first
        call if it doesn't already exist -- idempotent (`ON CONFLICT DO
        NOTHING`) and safe under concurrent pollers racing to create it.
        """
        self._connection.execute(_ENSURE_CHECKPOINT_ROW_SQL, {"id": _CHECKPOINT_ID})
        cursor = self._connection.execute(_GET_CHECKPOINT_SQL, {"id": _CHECKPOINT_ID})
        row = cursor.fetchone()
        return row[0] if row is not None else None

    def advance_checkpoint(
        self, *, observed_watermark: datetime | None, new_watermark: datetime
    ) -> bool:
        """Optimistic CAS: advances the checkpoint to `new_watermark` only
        if it still equals `observed_watermark` -- the value this caller's
        tick started with. Returns `False` -- not an error -- if another
        poller instance already moved it since; the checkpoint is never
        moved backward by this method regardless of `new_watermark`'s
        value (the caller, `worker/poller.py`, is responsible for only ever
        computing a `new_watermark` at or after what it observed).
        """
        cursor = self._connection.execute(
            _ADVANCE_CHECKPOINT_SQL,
            {
                "id": _CHECKPOINT_ID,
                "new_watermark": new_watermark,
                "observed_watermark": observed_watermark,
            },
        )
        return cursor.rowcount == 1
