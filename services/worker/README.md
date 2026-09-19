# vigil-worker

Evaluation job worker for Vigil, per
[`docs/decisions/005-evaluation-job-storage-worker.md`](../../docs/decisions/005-evaluation-job-storage-worker.md)
("ADR 005"). This package is the `services/worker` half of ADR 005's design.

**Phase 2** built the ClickHouse `evaluation_results` storage layer.
`worker/clickhouse/repository.py`'s `EvaluationResultsRepository` batch-inserts and point-looks-up
rows in the `evaluation_results` table (schema:
`infrastructure/clickhouse/init/002_create_evaluation_results_table.sql`), mirroring
`apps/api/app/clickhouse/repository.py`/`query_repository.py`'s conventions exactly. It is a
storage adapter only -- no dependency on `services/evaluator`, and no import from or by `apps/api`.

**Phase 3B** added `worker/postgres/repository.py`'s `EvaluationJobsRepository`: atomic job claiming
(`SKIP LOCKED`) and the two terminal completion transitions (`mark_succeeded`, `mark_failed`,
`mark_dead_letter`), each enforcing the attempt_count fencing invariant documented on the class and
in ADR 005's Phase 3 amendment. Raw `psycopg`, never SQLAlchemy or any import from `apps/api`'s ORM
(`worker/postgres/client.py`; ADR 001 decision 6, ADR 005 section 6).

**Note on package naming:** this package's own top-level importable name is `worker` (not `app`,
unlike `apps/api` and `services/evaluator`) -- `services/evaluator`'s package is `app`, and since
Phase 3C makes `services/worker` import `services/evaluator` directly into the same process (ADR 004
section 4, ADR 005 section 6), two distinct top-level packages both named `app` cannot coexist in one
Python environment. `services/worker` was renamed to resolve that collision; `services/evaluator`
(already reviewed/shipped, and referenced by literal `app/...` paths throughout ADR 004) was not.

**Phase 3C** wired claim → evaluate → persist end to end for one already-claimed job:
`worker/clickhouse/span_repository.py`'s `SourceSpanRepository` fetches a job's source span
(`FINAL`, four columns only, never `SELECT *`); `worker/adapters.py` converts it to a
`RelevanceEvaluatorInput`; `worker/registry.py`'s `EvaluatorRegistry` constructs both production
relevance evaluators once and reuses them, keyed by `(evaluator_name, evaluator_version)`;
`worker/postgres/evaluator_config_repository.py`'s `EvaluatorConfigRepository` resolves a project's
configured threshold (read-only: `enabled`, `sampling_rate`, `threshold`); `worker/result_mapping.py`
converts an `EvaluationResult` + `ClaimedJob` into an `evaluation_results` row
(`evaluation_id = job.id`, never generated); and `worker/execution.py`'s `execute_job` ties all of the
above together, enforcing that the ClickHouse result write completes before the PostgreSQL
`succeeded` transition is even attempted (never the reverse -- see `execution.py`'s module docstring
and ADR 005's Phase 3 plan section 9 for why the two orders are not symmetric).

**Phase 3D** added `worker/dispatcher.py`'s `Dispatcher`: bounded-concurrency batch dispatch over
`execute_job`. `Dispatcher.dispatch(jobs: list[ClaimedJob])` runs each job through `execute_job` inside
a `concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent_evaluations)` (the limit is a
required constructor argument, never unbounded -- `worker/config.py`'s `max_concurrent_evaluations`
setting, default `4`), returning one `DispatchOutcome` per job (in the same order `jobs` was given)
with either a populated `evaluation_outcome` or a captured `error` -- never both, never neither. One
job raising inside `execute_job` is caught and recorded; it never cancels or affects any other
concurrently-dispatched job, and `Dispatcher` never calls `mark_failed`/`mark_dead_letter` or computes
retry/backoff itself (that remains a later phase's decision). `Dispatcher` also never claims jobs --
its input is always an already-claimed `list[ClaimedJob]`.

**Resource ownership** (also Phase 3D, added when the first real-store dispatch test surfaced a real
bug): `Dispatcher` does not hold fixed ClickHouse/PostgreSQL repository instances built outside the
thread pool -- a real `clickhouse_connect.Client`/`psycopg.Connection` is not safe for concurrent use
from multiple threads, and sharing one across concurrently-dispatched tasks reproduces "Attempt to
execute concurrent queries within the same session." Instead `Dispatcher` holds a
`worker/resources.py::ResourceProvider` -- a zero-argument context-manager factory it calls *inside*
each task, right before `execute_job`. `worker/resources.py`'s `real_execution_resources` is the
production provider: it calls the already-thread-local-cached `get_clickhouse_client()` (one client
per pool thread, reused across that thread's jobs) and opens a fresh `psycopg` connection per task via
`get_connection()` (already returned a new connection per call; this just closes it in a `finally`, so
nothing leaks) -- not a connection pool. `EvaluatorRegistry` is unaffected by any of this: it is
genuinely shared and reused across every task, unlike the per-task ClickHouse/PostgreSQL resources.
Tests inject a fixed fake bundle via `contextlib.nullcontext` -- the same provider mechanism, no
special-casing.

**Phase 3E** added `worker/failure_handling.py`: `is_retryable` classifies an exception `execute_job`
raised (permanent -- dead-letter immediately -- only for `SourceSpanNotFoundError` and
`InvalidEvaluatorInputError`; everything else, including unclassified exception types, is retryable by
default); `compute_next_attempt_at` implements the exponential-backoff-with-jitter formula (attempt 1
≈ 5s, attempt 2 ≈ 10s, attempt 3 ≈ 20s, ... capped at 300s, `worker/config.py`'s `retry_base_seconds`/
`retry_max_delay_seconds`/`retry_jitter_seconds`); `handle_execution_failure` applies the resulting
`mark_failed` (retry budget remains) or `mark_dead_letter` (budget exhausted, or a permanent failure at
any attempt count) transition, using the exact `job.attempt_count` fencing token already established in
Phase 3B -- never re-read or recomputed. `max_retries` is a **total-attempt cap**
(`evaluation_job.py`'s own docstring already established this): with `max_retries=3`, three total
attempts occur before dead-lettering, not four. `Dispatcher` now calls `handle_execution_failure` from
`_run_one`'s exception handler (using the same task-local `jobs_repository` `execute_job` was already
given), and `DispatchOutcome` gained a `failure_handling` field reporting the result -- `None` if
resource acquisition itself failed, or if recording the failure raised in turn (a rare compound
failure that degrades to leaving the job's row untouched, never a crash of `dispatch()`). See ADR 005's
Phase 3E amendment for the full classification table and formula derivation.

## What this package is not (yet)

- **Not a runnable worker.** No poller, no main/daemon loop that claims a batch and calls
  `Dispatcher.dispatch` repeatedly, and no stuck-job reaper exist yet -- see ADR 005 sections 6-8 for
  what those will look like when built. A genuine process crash (not a catchable Python exception)
  still leaves a job stuck `running` forever until the reaper exists; Phase 3E only closes that gap for
  the catchable-exception path.
- **No `evaluation_poller_checkpoint` access, no poller, no internal-API calls, no `apps/api`
  job-creation endpoint.** None of these exist yet anywhere in this repository.
- **No new evaluator algorithms, no sampling dispatch, no config-mutation API.**
  `EvaluatorConfigRepository` is read-only by design (`get_config` only); `enabled`/`sampling_rate`
  gating remains `apps/api`'s job-creation-time business logic (ADR 005 section 10), unchanged.

## `evaluation_id` identity

`EvaluationResultsRepository` never generates a `evaluation_id` -- the caller must always pass the
corresponding `evaluation_jobs.id` (PostgreSQL) through verbatim. This is what gives a ClickHouse
result row and its owning PostgreSQL job row a stable, application-level shared identity across the
two stores, per ADR 005's Phase 2 decision.

## Logging

Structured (JSON Lines) logging on stdout -- one JSON object per line, every field a genuine
top-level key (`timestamp`, `level`, `service`, `logger`, `message`, plus fields like `worker_id`/
`job_id`/`project_id`/`evaluator_name` where relevant) rather than text embedded in a message
string. See `worker/logging_config.py`'s module docstring for the full design.

`VIGIL_WORKER_LOG_LEVEL` (default `INFO`, shared by both `worker` and `poller`) controls the root
logger level; one of `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` -- anything else fails at startup.
`service` distinguishes the two processes in their JSON output (`"worker"` vs. `"poller"`) even
though they share one image/package (`services/worker/Dockerfile`).

`worker_id` (`worker.runtime.generate_worker_id`, `hostname:pid:short-uuid`, one per process) is
attached via `extra={...}` at each relevant `worker/runtime.py` call site -- deliberately not an
auto-injecting contextvar the way `apps/api` does for `request_id`, since job execution runs on a
plain `ThreadPoolExecutor` (`worker/dispatcher.py`), which does not propagate a context variable
set on the submitting thread into pool worker threads.

**Local development sees the identical JSON output production does** -- deliberately not a second,
prettier console formatter. Pipe through `jq` for a readable view:

```bash
uv run python -m worker | jq .
```

**Never logged**: the internal service token, database URLs, or raw span/evaluation input/output
content. Every call site's `extra={...}` is limited to identifiers (worker/job/project/trace/span
ids, evaluator name/version, counts, error type names) -- see `worker/logging_config.py`'s security
note.

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

`tests/test_span_repository.py`, `test_adapters.py`, `test_registry.py`,
`test_evaluator_config_repository.py`, `test_result_mapping.py`, and `test_execution.py` cover
Phase 3C's new pieces with fakes (`test_registry.py` uses one module-scoped real registry, so
`EmbeddingRelevanceEvaluator`'s model load is paid once per test run, not once per test).
`tests/test_execution_integration.py` runs `execute_job` against both real stores at once (real
PostgreSQL claim, real ClickHouse span + result), skipped automatically if either is unreachable.

`tests/test_dispatcher.py` covers `Dispatcher` with hand-rolled, thread-safe fakes keyed by job
identity (not the FIFO-queue fakes above -- concurrent jobs call repository methods in a genuinely
non-deterministic interleaved order, so a shared FIFO queue could hand one job's response to a
different job), including a deterministic bounded-concurrency proof (`threading.Barrier` + a locked
peak-counter, not sleep/timing) and an explicit proof that `Dispatcher` calls the injected
`resource_provider` itself, once per task. `tests/test_dispatcher_integration.py` runs
`Dispatcher.dispatch` on six real claimed jobs against both real stores at
`max_concurrent_evaluations=3`, using `worker.resources.real_execution_resources` as the provider --
proving the "Attempt to execute concurrent queries" failure does not recur -- plus two more surgical
tests: each resource-provider call gets its own PostgreSQL connection (closed on exit), and several
threads can resolve resources and query ClickHouse concurrently via a `threading.Barrier` with no
error. All skipped automatically if either store is unreachable.

`tests/test_failure_handling.py` covers classification, backoff (deterministic component via a
patched `random.uniform`), and `handle_execution_failure` against the real `EvaluationJobsRepository`
wrapped around a fake connection (exact SQL/parameters, not just "some method was called").
`tests/test_failure_handling_postgres_integration.py` proves the same transitions against a real
claimed row -- retryable-with-budget, budget-exhausted, permanent-at-attempt-1, a two-cycle
reclaim-then-dead-letter sequence, and the stale-fencing race -- skipped automatically if PostgreSQL is
unreachable.
