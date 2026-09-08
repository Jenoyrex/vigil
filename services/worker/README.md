# vigil-worker

Evaluation job worker for Vigil, per
[`docs/decisions/005-evaluation-job-storage-worker.md`](../../docs/decisions/005-evaluation-job-storage-worker.md)
("ADR 005"). This package is the `services/worker` half of ADR 005's design.

**Phase 2** built the ClickHouse `evaluation_results` storage layer.
`app/clickhouse/repository.py`'s `EvaluationResultsRepository` batch-inserts and point-looks-up
rows in the `evaluation_results` table (schema:
`infrastructure/clickhouse/init/002_create_evaluation_results_table.sql`), mirroring
`apps/api/app/clickhouse/repository.py`/`query_repository.py`'s conventions exactly. It is a
storage adapter only -- no dependency on `services/evaluator`, and no import from or by `apps/api`.

**Phase 3B** added `app/postgres/repository.py`'s `EvaluationJobsRepository`: atomic job claiming
(`SKIP LOCKED`) and the two terminal completion transitions (`mark_succeeded`, `mark_failed`,
`mark_dead_letter`), each enforcing the attempt_count fencing invariant documented on the class and
in ADR 005's Phase 3 amendment. Raw `psycopg`, never SQLAlchemy or any import from `apps/api`'s ORM
(`app/postgres/client.py`; ADR 001 decision 6, ADR 005 section 6).

## What this package is not (yet)

- **Not a runnable worker.** No poller, no dispatch loop wiring the pieces below together, no retry/
  backoff calculation, and no stuck-job reaper exist yet -- see ADR 005 sections 6-8 for what those
  will look like when built. `EvaluationJobsRepository` provides the claim/complete mechanics; nothing
  yet calls it in a loop.
- **Not connected to `services/evaluator`.** Nothing here imports `EvaluationResult` or any
  `Evaluator` implementation. `EvaluationResultsRepository.insert_results` takes plain row dicts
  (the same shape `SpansRepository.insert_spans` expects), by design -- turning one
  `EvaluationResult` plus its job identity into such a row is the future dispatch/persistence
  adapter's job, not this repository's.
- **No span fetching, no evaluator dispatch, no result-persistence orchestration.** These tie
  `EvaluationJobsRepository` and `EvaluationResultsRepository` together into an actual claim →
  evaluate → persist flow; not yet built.
- **No `evaluation_poller_checkpoint` access, no poller, no internal-API calls.** `apps/api`'s
  job-creation endpoint this worker's future poller would call also does not exist yet.

## `evaluation_id` identity

`EvaluationResultsRepository` never generates a `evaluation_id` -- the caller must always pass the
corresponding `evaluation_jobs.id` (PostgreSQL) through verbatim. This is what gives a ClickHouse
result row and its owning PostgreSQL job row a stable, application-level shared identity across the
two stores, per ADR 005's Phase 2 decision.

## Tests

```bash
pytest
```

`tests/test_evaluation_results_repository.py` uses a fake ClickHouse client (no server required).
`tests/test_evaluation_results_clickhouse_integration.py` runs against a real local ClickHouse
(`infrastructure/docker-compose.yml`) and is skipped automatically if one isn't reachable.

`tests/test_evaluation_jobs_repository.py` uses a fake PostgreSQL connection to assert exact SQL/
parameters (no server required). `tests/test_evaluation_jobs_postgres_integration.py` runs against
the same real `vigil_test` PostgreSQL database `apps/api/tests/conftest.py` uses, and is skipped
automatically if unreachable -- this is the file that actually proves `SKIP LOCKED` concurrency and
the attempt_count fencing invariant, since neither is something a fake connection can demonstrate.
