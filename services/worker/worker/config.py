"""services/worker settings, same `pydantic-settings`/env-var pattern as
`apps/api/app/config.py` (see docs/decisions/005-evaluation-job-storage-worker.md
section 12), scoped for now to what has actually been built: ClickHouse
`evaluation_results` storage, PostgreSQL `evaluation_jobs` lifecycle/
claiming, `worker.dispatcher.Dispatcher`'s bounded concurrency,
`worker.failure_handling`'s retry/backoff, `worker.reaper`'s stuck-job
reclaim, `worker.runtime.WorkerRuntime`'s claim/dispatch/reap loop, and
(Phase 4A) `worker.timeouts`'s per-call evaluator timeout enforcement plus
the bounded-orphan-count self-restart it feeds into `worker/runtime.py`.
`evaluator_call_timeout_seconds` and `evaluator_init_timeout_seconds` are
two deliberately separate settings -- see `worker/registry.py`'s module
docstring for why steady-state `evaluate()` latency and first-use
model-construction latency must never share one timeout.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VIGIL_WORKER_", env_file=".env")

    # ClickHouse connection. Defaults match infrastructure/.env.example /
    # infrastructure/docker-compose.yml local development credentials --
    # same defaults apps/api/app/config.py uses for the same database.
    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_database: str = "vigil"
    clickhouse_user: str = "vigil"
    clickhouse_password: str = "vigil"
    clickhouse_timeout_seconds: float = 10.0

    # PostgreSQL connection -- raw psycopg, never SQLAlchemy/apps/api's ORM
    # (ADR 001 decision 6, ADR 005 section 6). A plain `postgresql://` DSN
    # (psycopg's own conninfo format), not apps/api's SQLAlchemy-dialect-
    # prefixed `postgresql+psycopg://` one, though it points at the same
    # local database by default.
    database_url: str = "postgresql://vigil:vigil@localhost:5434/vigil"

    # Bounded-concurrency dispatch (worker/dispatcher.py). Deliberately
    # small by default, not unbounded -- ADR 005 section 12's "a project
    # operator... should not silently get full-volume evaluation" posture,
    # applied here to worker-process resource usage rather than per-project
    # sampling. Must be >= 1; Dispatcher itself also enforces this.
    max_concurrent_evaluations: int = 4

    # Per-call evaluator timeout (Phase 4A, worker/timeouts.py,
    # worker/execution.py). Bounds one `evaluate()` call -- both registered
    # evaluators are documented sub-second once loaded, so this is generous
    # relative to any genuine call, while still catching a hang far sooner
    # than stuck_job_threshold_seconds below ever could. This is a
    # best-effort timeout, not forced cancellation: Python cannot forcibly
    # stop a running thread, so a call that exceeds this is abandoned, not
    # killed -- see worker/timeouts.py's module docstring for exactly what
    # that does and does not guarantee, and max_orphaned_evaluator_threads
    # below for how the resulting resource usage is bounded rather than
    # left unbounded.
    evaluator_call_timeout_seconds: float = 30.0

    # First-use evaluator construction timeout (Phase 4A, worker/registry.py).
    # Deliberately separate from, and more generous than,
    # evaluator_call_timeout_seconds above: constructing
    # EmbeddingRelevanceEvaluator can mean a one-time, cold-cache network
    # download of its ONNX model (~67MB from Hugging Face) on whichever
    # worker process/thread first calls `.get()` for it, a categorically
    # different, one-time cost that must never be judged against the tight
    # per-call inference timeout.
    evaluator_init_timeout_seconds: float = 90.0

    # Bounded-orphan self-restart (Phase 4A, worker/timeouts.py,
    # worker/runtime.py). A timed-out call (either kind above) is
    # abandoned, not stopped -- its thread may keep running, unsupervised,
    # for however long it takes to finish on its own or the process exits.
    # This is NOT true forced cancellation and does NOT eliminate the
    # resulting resource usage; it bounds the *worst case* instead: once
    # this many such abandoned calls are simultaneously still outstanding
    # in this process, worker.runtime.WorkerRuntime requests its own
    # graceful shutdown (reusing the exact same request_stop() a SIGTERM
    # already triggers) rather than letting the count grow without limit.
    # Recovery after that depends entirely on an external process
    # supervisor (systemd/Docker/Kubernetes restart policy) bringing up a
    # fresh, zero-orphan replacement -- this setting only ever decides when
    # to retire this process, never how it comes back. Defaults to the same
    # value as max_concurrent_evaluations: losing that many concurrency
    # slots to permanently-stuck calls is, in effect, having lost this
    # process's entire intended throughput budget to leaks.
    max_orphaned_evaluator_threads: int = 4

    # Retry/backoff (worker/failure_handling.py), per
    # docs/decisions/005-evaluation-job-storage-worker.md's Phase 3E
    # amendment: delay_seconds = min(retry_max_delay_seconds,
    # retry_base_seconds * (2 ** (attempt_count - 1))) +
    # random.uniform(0, retry_jitter_seconds). Attempt 1 -> ~5s, attempt 2
    # -> ~10s, attempt 3 -> ~20s, ... capped at 300s before jitter.
    retry_base_seconds: float = 5.0
    retry_max_delay_seconds: float = 300.0
    retry_jitter_seconds: float = 2.0

    # Stuck-job reaper (worker/reaper.py), per docs/decisions/005-evaluation-
    # job-storage-worker.md's Phase 3F amendment. stuck_job_threshold_seconds
    # must be meaningfully larger than however long a genuine, alive
    # evaluation can take, so the reaper only catches real process death
    # (a crashed/killed worker), never a call that's merely slow. This
    # remains deliberately conservative even now that a per-call evaluator
    # timeout exists (evaluator_call_timeout_seconds below): the two are
    # complementary, not redundant -- that timeout bounds one thread's own
    # wait for one call and lets the worker process keep making progress on
    # other jobs; this threshold is the backstop for the case that timeout
    # cannot address at all, a worker process that has died outright and can
    # never itself decide anything. reaper_batch_size mirrors claim_jobs'
    # own batch_size argument, given a config default here since, unlike
    # claim_jobs' worker-loop caller, the reaper has no other natural source
    # for it.
    stuck_job_threshold_seconds: float = 900.0
    reaper_batch_size: int = 100

    # Worker runtime loop (worker/runtime.py), per docs/decisions/005-
    # evaluation-job-storage-worker.md's Phase 3G amendment. claim_batch_size
    # is deliberately independent of max_concurrent_evaluations -- both
    # default to 4 today, but they are separate knobs (how many jobs one
    # claim tick pulls vs. how many `Dispatcher` runs concurrently) and are
    # never coupled in code, so either can be retuned without touching the
    # other. poll_interval_seconds is how long an idle claim tick waits
    # before trying again; reaper_interval_seconds is the runtime loop's own,
    # separate cadence for calling reap_stuck_jobs, tracked via
    # time.monotonic() rather than wall-clock time.
    claim_batch_size: int = 4
    poll_interval_seconds: float = 2.0
    reaper_interval_seconds: float = 60.0

    # Evaluation job poller (worker/poller.py), per docs/decisions/005-
    # evaluation-job-storage-worker.md's Phase 3H amendment. poller_batch_size
    # is independent of claim_batch_size -- unrelated queries against
    # unrelated stores (a ClickHouse span scan vs. a Postgres job claim).
    # poller_overlap_seconds deliberately re-scans a trailing window of
    # already-advanced checkpoint history on every tick, to stay correct
    # against same-millisecond ingested_at collisions (the common case for a
    # batched span insert -- ClickHouse's now64() default is evaluated once
    # per INSERT statement, not once per row) and ClickHouse's own
    # insert-visibility lag, relying on idempotent job creation to make the
    # redundant re-scan free rather than on a precise cursor. See
    # worker/poller.py's module docstring for the full rationale.
    # poller_start_time_lookback_days is a partition-pruning OPTIMIZATION
    # ONLY (worker/clickhouse/eligible_span_repository.py's module
    # docstring) -- never a correctness boundary, and never applied at all
    # on the very first poll (no checkpoint yet), matching
    # evaluation_poller_checkpoint's own documented "NULL means start from
    # the beginning of the retention window" semantics.
    poller_batch_size: int = 500
    poller_overlap_seconds: float = 60.0
    poller_start_time_lookback_days: int = 3
    poller_interval_seconds: float = 30.0
    poller_job_creation_timeout_seconds: float = 10.0

    # Internal worker-fleet authentication (Phase 3H, ADR 005 section 9) --
    # the worker side of the SAME shared secret apps/api's
    # VIGIL_API_INTERNAL_SERVICE_TOKEN setting holds; both must be set to
    # the identical value for the poller to authenticate. No default here
    # either -- must be supplied via environment/.env in every environment,
    # matching that setting's own no-baked-in-default requirement.
    internal_service_token: str
    api_base_url: str = "http://localhost:8000"


settings = Settings()
