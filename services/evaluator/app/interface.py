"""The evaluator contract every Vigil evaluator implements.

Per docs/decisions/004-evaluation-engine.md section 4, an evaluator is a
plain scoring library: input in, `EvaluationResult` out. It has no
knowledge of a job queue, a database, or an HTTP request -- see
types.py's `EvaluationResult` docstring for what that deliberately
excludes and why. services/worker (not yet built) is the only intended
caller of `evaluate()` in production; every evaluator here must also be
directly callable from a plain Python script or test with no other Vigil
service running.
"""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

from app.types import EvaluationResult

TInput = TypeVar("TInput")


@runtime_checkable
class Evaluator(Protocol[TInput]):
    """Structural contract for one evaluator.

    `name` and `version` make evaluator identity explicit and are exactly
    what `EvaluationResult.evaluator_name`/`evaluator_version` should be
    populated from -- callers should never invent or duplicate these
    strings elsewhere. `evaluate` is the one operation every evaluator
    supports: given this evaluator's own input type, return a result.

    Deliberately a `Protocol`, not an abstract base class: an evaluator
    only needs to structurally match this shape, not inherit from a
    shared base class -- so a new evaluator (or a test double) can be
    written with zero coupling to this module beyond the shape itself.
    `runtime_checkable` lets callers do `isinstance(x, Evaluator)` as a
    cheap sanity check without that coupling either. Note that
    `runtime_checkable` only verifies member *presence* (`name`,
    `version`, `evaluate`), never a method's exact signature -- an
    `evaluate` that ignores `threshold` entirely still satisfies this
    Protocol; see test_interface.py's compatibility test.

    `threshold`, keyword-only and optional, is the sole per-call
    override an evaluator accepts. `None` (every caller before this
    parameter existed, and any caller that still omits it) means "use
    this instance's own configured/constructed default" -- unchanged
    behavior. A caller that does pass a value (services/worker,
    resolving one project's configured `evaluator_configs.threshold`)
    overrides the threshold for that one call only; the instance's own
    default, and any other in-flight or future call against the same
    long-lived instance, are unaffected. This is what lets one
    expensive-to-construct evaluator instance (see e.g.
    embedding_relevance.py's ONNX session) be shared across many
    projects with different configured thresholds without constructing
    a separate instance per threshold.
    """

    name: str
    version: str

    def evaluate(
        self, evaluator_input: TInput, *, threshold: float | None = None
    ) -> EvaluationResult: ...
