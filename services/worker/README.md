# vigil-worker

Evaluation job worker for Vigil, per
[`docs/decisions/005-evaluation-job-storage-worker.md`](../../docs/decisions/005-evaluation-job-storage-worker.md)
("ADR 005"). This package is the `services/worker` half of ADR 005's design.

**This milestone (Phase 2) builds the ClickHouse `evaluation_results` storage layer only.**
`app/clickhouse/repository.py`'s `EvaluationResultsRepository` batch-inserts and point-looks-up
rows in the `evaluation_results` table (schema:
`infrastructure/clickhouse/init/002_create_evaluation_results_table.sql`), mirroring
`apps/api/app/clickhouse/repository.py`/`query_repository.py`'s conventions exactly. It is a
storage adapter only -- it has no PostgreSQL access, no HTTP surface, no dependency on
`services/evaluator`, and does not import from or get imported by `apps/api`.

## What this package is not (yet)

- **Not a runnable worker.** No poller, no `SKIP LOCKED` claim loop, no evaluator dispatch, no
  retry/backoff/dead-letter logic, and no stuck-job reaper exist yet -- see ADR 005 sections 6-8 for
  what those will look like when built.
- **Not connected to `services/evaluator`.** Nothing here imports `EvaluationResult` or any
  `Evaluator` implementation. `EvaluationResultsRepository.insert_results` takes plain row dicts
  (the same shape `SpansRepository.insert_spans` expects), by design -- turning one
  `EvaluationResult` plus its job identity into such a row is the future dispatch/persistence
  adapter's job, not this repository's.
- **Not connected to PostgreSQL.** The `evaluation_jobs` claim loop and
  `evaluation_poller_checkpoint` access ADR 005 section 6 describes are a later phase.

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
