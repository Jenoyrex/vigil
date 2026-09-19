"""Evaluator registry: lazily constructs each registered `Evaluator`
implementation on its own first use, then reuses that exact instance across
every job dispatched to it -- docs/decisions/005-evaluation-job-storage-worker.md
section 6's "construct once per worker process, never once per job"
requirement, because `EmbeddingRelevanceEvaluator`'s ONNX session load is
expensive and its (thread-safe) session should never be reloaded per job,
per project, or per configured threshold.

**Lazy, not eager (Phase 4A).** A worker process that never receives a
`relevance_embedding` job never constructs `EmbeddingRelevanceEvaluator` at
all -- the ~67MB ONNX model download/load (`app.embedding_relevance`'s own
docstring) happens only on the first actual `.get()` call for that key, not
merely because the process started. This makes ADR 005 section 10's
"opt-in, off by default" posture hold at the infrastructure level too, not
only at the per-project `enabled` flag: a deployment that only ever uses
`relevance` pays zero embedding-model cost, ever, on any worker replica.
`RelevanceEvaluator` is lazily constructed by the identical mechanism for
uniformity (its own construction is effectively instant either way).

Registered for this milestone -- the two relevance evaluators ADR 005
section 12 names as production-selectable, explicitly:

- `("relevance", "0.1.0")` -- `RelevanceEvaluator`, the TF-IDF baseline.
- `("relevance_embedding", "0.1.0")` -- `EmbeddingRelevanceEvaluator`,
  `BAAI/bge-small-en-v1.5`, ADR 005 section 12's recommended first
  production evaluator.

No LLM-judge, groundedness, or faithfulness evaluator exists to register --
ADR 004 sections 1-2, unchanged by this phase.

Threshold is never baked into which instance gets registered or looked up:
every instance here is constructed with no threshold override (each
evaluator's own `DEFAULT_THRESHOLD` -- an unvalidated placeholder, not a
WikiQA-derived number, see each evaluator's own docstring), and the
per-project effective threshold is supplied at `evaluate()` call time
instead (see `app.interface.Evaluator.evaluate`'s `threshold` parameter and
this ADR's Phase 3 threshold-resolution amendment). Nothing in this module
mutates an evaluator instance's state, and evaluator instances are safe for
concurrent `evaluate()` calls (both registered evaluators document this
explicitly -- `RelevanceEvaluator` has no shared mutable state at all,
`EmbeddingRelevanceEvaluator`'s ONNX Runtime session is documented safe for
concurrent inference from multiple threads).

**First-use construction is itself timeout-bounded** (`evaluator_init_
timeout_seconds`, separate from `worker.execution`'s steady-state
`evaluator_call_timeout_seconds` -- see `worker/timeouts.py`'s module
docstring for why this project cannot forcibly stop either kind of hang,
only stop waiting for it). This is deliberately a *more generous*, distinct
setting: model construction (a cold-cache network download) is a
categorically different, one-time cost from a single inference call, and
must never be judged against the tight per-call timeout meant for
steady-state `evaluate()` latency.

**Concurrency**: one `threading.Lock` per registered key (never one global
lock, so constructing `relevance` never blocks a concurrent first use of
`relevance_embedding`) serializes concurrent `.get()` calls for the *same*
key under normal operation -- only one thread actually calls the factory at
a time. That lock is held only around the decision to attempt construction
and the (bounded, `run_with_timeout`-wrapped) attempt itself; it is
released the instant `run_with_timeout` returns *or raises*, per
`worker/timeouts.py`'s own "never hold a lock waiting on an orphaned
thread" discipline. The one residual case this cannot fully serialize: if
an attempt has already timed out and been abandoned (its underlying thread
still running, unsupervised, in the background) and a *separate*, later
`.get()` call for the same key arrives (e.g. a retried job after backoff),
that later call is free to make its own attempt rather than block
indefinitely on the abandoned one -- so, rarely, two constructions of the
same evaluator can end up running concurrently. Whichever finishes first
wins the cache slot (`dict.setdefault`, a single GIL-atomic operation,
needs no lock of its own and is safe to call from an abandoned thread that
may complete arbitrarily late); the other's result is simply discarded and
garbage-collected normally. An abandoned construction attempt only ever
touches this registry's own in-memory cache -- it has no access to
`ClaimedJob`, `jobs_repository`, or `results_repository`, so it can never
have any effect on job/result state no matter how late it completes.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from app.embedding_relevance import EmbeddingRelevanceEvaluator
from app.interface import Evaluator
from app.relevance import RelevanceEvaluator

from worker.timeouts import run_with_timeout

DEFAULT_EVALUATOR_INIT_TIMEOUT_SECONDS = 90.0

_DEFAULT_FACTORIES: dict[tuple[str, str], Callable[[], Evaluator]] = {
    (RelevanceEvaluator.name, RelevanceEvaluator.version): RelevanceEvaluator,
    (EmbeddingRelevanceEvaluator.name, EmbeddingRelevanceEvaluator.version): (
        EmbeddingRelevanceEvaluator
    ),
}


class UnknownEvaluatorError(LookupError):
    """No registered evaluator matches the requested `(evaluator_name,
    evaluator_version)` pair -- e.g. a worker fleet mid rolling-deploy that
    no longer ships (or never shipped) that exact version. Not this
    registry's decision what to do about it; the caller (`worker/execution.py`)
    surfaces this as a typed failure for the retry/dead-letter layer to
    decide on.
    """


class EvaluatorRegistry:
    def __init__(
        self,
        evaluators: list[Evaluator] | None = None,
        *,
        factories: dict[tuple[str, str], Callable[[], Evaluator]] | None = None,
        evaluator_init_timeout_seconds: float = DEFAULT_EVALUATOR_INIT_TIMEOUT_SECONDS,
    ) -> None:
        """`evaluators=None, factories=None` (the production default)
        registers this milestone's two evaluator keys for lazy construction
        -- see module docstring. Passing an explicit `evaluators` list
        (e.g. a lightweight test double, or a fixed set of already-
        constructed instances) bypasses lazy construction entirely: those
        instances are cached immediately, `evaluator_init_timeout_seconds`
        is unused for them, and `.get()`'s behavior for that path is
        unchanged from before this phase -- only the default path's
        construction *timing* changed. `factories` (mutually exclusive with
        `evaluators`) overrides which keys/callables lazy construction
        uses -- primarily for tests that need a fast, controllable fake
        factory to exercise timeout/race behavior without paying
        `EmbeddingRelevanceEvaluator`'s real load cost.
        """
        if evaluators is not None:
            self._instances: dict[tuple[str, str], Evaluator] = {
                (evaluator.name, evaluator.version): evaluator for evaluator in evaluators
            }
            self._factories: dict[tuple[str, str], Callable[[], Evaluator]] = {}
            self._locks: dict[tuple[str, str], threading.Lock] = {}
            self._evaluator_init_timeout_seconds = evaluator_init_timeout_seconds
            return

        self._instances = {}
        self._factories = dict(factories) if factories is not None else dict(_DEFAULT_FACTORIES)
        self._locks = {key: threading.Lock() for key in self._factories}
        self._evaluator_init_timeout_seconds = evaluator_init_timeout_seconds

    def get(self, evaluator_name: str, evaluator_version: str) -> Evaluator:
        key = (evaluator_name, evaluator_version)

        instance = self._instances.get(key)
        if instance is not None:
            return instance

        factory = self._factories.get(key)
        if factory is None:
            raise UnknownEvaluatorError(
                f"No registered evaluator for evaluator_name={evaluator_name!r}, "
                f"evaluator_version={evaluator_version!r}."
            )

        with self._locks[key]:
            # Re-check: another thread may have finished constructing this
            # exact evaluator (including a previously-abandoned attempt
            # that has since completed late and self-cached, see
            # `_construct_and_cache` below) while we were waiting for the
            # lock.
            instance = self._instances.get(key)
            if instance is not None:
                return instance

            def _construct_and_cache() -> Evaluator:
                # Runs on run_with_timeout's daemon thread -- never touches
                # `self._locks[key]` (the calling thread above may still be
                # holding it while this runs, and while `run_with_timeout`
                # is still waiting; acquiring the same lock here would
                # deadlock). `dict.setdefault` is a single, GIL-atomic
                # operation: first successful completion wins the cache
                # slot, and this is safe to call even from an orphaned
                # thread that completes arbitrarily late, with no lock of
                # its own needed.
                built = factory()
                return self._instances.setdefault(key, built)

            return run_with_timeout(
                _construct_and_cache, timeout_seconds=self._evaluator_init_timeout_seconds
            )

    def registered_keys(self) -> frozenset[tuple[str, str]]:
        """Every supported `(evaluator_name, evaluator_version)` pair --
        available immediately, before any evaluator has ever been
        constructed. `worker.poller.Poller` relies on exactly this: it
        calls only this method, never `.get()`, so the poller process never
        triggers evaluator construction at all."""
        return frozenset(self._instances) | frozenset(self._factories)
