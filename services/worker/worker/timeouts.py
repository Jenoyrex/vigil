"""Best-effort per-call timeout enforcement for one synchronous, zero-argument
callable -- shared by `worker.execution` (the steady-state `evaluate()` call
timeout) and `worker.registry` (the first-use construction/initialization
timeout). See `worker/runtime.py`'s module docstring for how the two differ
and how this module's orphan counter feeds this worker process's own
self-restart decision.

**Why a plain daemon `threading.Thread`, never `concurrent.futures.
ThreadPoolExecutor`.** `future.result(timeout=...)` looks like the natural
fit here, but `ThreadPoolExecutor` cannot be used for this: every worker
thread it ever creates is registered in a module-level dict
(`concurrent.futures.thread._threads_queues`) and unconditionally joined,
with no timeout, by an interpreter-shutdown hook the stdlib installs via
`threading._register_atexit(_python_exit)` -- confirmed directly against
this project's own installed CPython 3.12.4:

    def _python_exit():
        ...
        for t, q in items:
            t.join()

This runs "just before joining all non-daemon threads" as part of the
interpreter's own shutdown sequence, independent of whether `.shutdown()`
was ever called on the executor or how it's owned/sized. A single call
still stuck inside a `ThreadPoolExecutor` worker thread at that point
blocks the *whole process* from exiting on its own, no matter how the
timeout at the `dispatch()` level is implemented. A `threading.Thread`
constructed with `daemon=True` is exempt from both this join and from
CPython's own thread-join-on-exit behavior -- the only primitive available
(without process isolation, which Phase 4A deliberately excludes) that lets
this worker process actually exit cleanly while a hung call is still
physically running.

**What this does NOT do: stop the work.** Python cannot forcibly interrupt
a running thread, and nothing in `sklearn` (the TF-IDF evaluator) or
`fastembed`/`onnxruntime` (the embedding evaluator, as we actually call
them -- no `RunOptions`/session access is exposed through `fastembed`'s
public API) offers a cancellation hook this module can reach without
depending on private internals. A "timed-out" call keeps running,
unsupervised, until it finishes on its own or the process exits. This
module only ever stops *waiting* for it -- see the orphan counter below for
how the resulting resource usage is bounded rather than left unbounded.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

_orphan_count_lock = threading.Lock()
_orphan_count = 0


class EvaluatorTimeoutError(TimeoutError):
    """Raised by `run_with_timeout` when `fn` does not finish within
    `timeout_seconds`. The message carries only the configured timeout
    value -- never `fn`'s arguments, return value, or any evaluator
    input/payload content, since `run_with_timeout` itself has no idea
    what `fn` closes over and must never assume it's safe to log."""


def outstanding_orphaned_calls() -> int:
    """Current count of `run_with_timeout` calls that timed out and whose
    underlying daemon thread is still running. Process-local (never shared
    across worker processes), and only ever decreases when an orphaned
    call eventually finishes on its own -- there is no other way to lower
    it. `worker.runtime.WorkerRuntime` polls this to decide when to
    self-restart; see that module."""
    with _orphan_count_lock:
        return _orphan_count


def _increment_orphan_count() -> None:
    global _orphan_count
    with _orphan_count_lock:
        _orphan_count += 1


def _decrement_orphan_count() -> None:
    global _orphan_count
    with _orphan_count_lock:
        _orphan_count -= 1


def run_with_timeout[T](fn: Callable[[], T], *, timeout_seconds: float) -> T:
    """Run `fn` (a synchronous, zero-argument callable) on a daemon thread,
    waiting up to `timeout_seconds` for it to finish.

    - Returns `fn`'s result if it finishes in time.
    - Re-raises `fn`'s own exception, unchanged, if it finishes in time
      with an error.
    - Raises `EvaluatorTimeoutError` if `fn` has not finished within
      `timeout_seconds`. The underlying thread is not stopped (Python
      cannot do that) -- it is abandoned, `outstanding_orphaned_calls()`'s
      count is incremented for exactly as long as it remains alive, and a
      second, separate daemon thread is started solely to `join()` it
      (however long that takes, even indefinitely) and decrement the count
      exactly once when it eventually finishes. That watcher thread is
      itself daemon, so it can never block process exit either.
    """
    result: list[T] = []
    error: list[BaseException] = []

    def _target() -> None:
        try:
            result.append(fn())
        except BaseException as exc:  # noqa: BLE001 -- re-raised verbatim below,
            # on the calling thread, never swallowed here.
            error.append(exc)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout_seconds)

    if thread.is_alive():
        _increment_orphan_count()
        _watch_for_late_completion(thread)
        raise EvaluatorTimeoutError(f"Evaluator call did not complete within {timeout_seconds}s.")

    if error:
        raise error[0]
    return result[0]


def _watch_for_late_completion(thread: threading.Thread) -> None:
    """Spawn a daemon thread whose only job is to wait -- for however long
    it takes, even forever -- for an already-orphaned `thread` to finish,
    then decrement the orphan count exactly once. Never touches `thread`'s
    result/exception (already discarded by `run_with_timeout`'s caller,
    which has moved on); this exists solely to keep the orphan count
    accurate."""

    def _watch() -> None:
        thread.join()
        _decrement_orphan_count()

    threading.Thread(target=_watch, daemon=True).start()
