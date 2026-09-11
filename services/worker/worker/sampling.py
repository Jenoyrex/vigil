"""Deterministic sampling decision for evaluation-job eligibility -- ADR 005
section 10, Phase 3H amendment.

Owned, functionally, by `apps/api`'s job-creation service (`create_evaluation_job`
actually enforces the decision this module computes) -- `apps/api`
independently implements the identical algorithm rather than importing this
module, per ADR 001 decision 6's "duplicate rather than centralize"
cross-service boundary: `apps/api` must never depend on `services/worker`,
the same reason it must never depend on `services/evaluator`. This module
exists so the algorithm has exactly one authoritative, unit-tested
definition in `services/worker`'s own test suite to keep both copies honest
against, and so the guarantee this whole design depends on is directly
provable: the same `(project_id, trace_id, span_id, evaluator_name)`
produces the same decision, for a given `sampling_rate`, regardless of
which process, or how many times, it is evaluated.
"""

from __future__ import annotations

import hashlib
import uuid


def is_sampled_in(
    *,
    project_id: uuid.UUID,
    trace_id: str,
    span_id: str,
    evaluator_name: str,
    sampling_rate: float,
) -> bool:
    """`True` if `(project_id, trace_id, span_id, evaluator_name)` falls
    within `sampling_rate`'s admitted fraction, deterministically.

    Uses `hashlib.sha256`, never Python's built-in `hash()`: `hash()` is
    salted per-process (`PYTHONHASHSEED`, to resist hash-flooding), so the
    *same* string hashes to *different* values across process restarts and
    across concurrently-running `apps/api` instances -- silently breaking
    the exact guarantee this function exists to provide, in a way that
    would pass every single-process test and only surface once something
    restarts or scales out. `sha256` is stable across processes, machines,
    and Python versions by construction.

    Sampling key deliberately excludes `evaluator_version`: a later version
    bump (e.g. `relevance` `0.1.0` -> `0.2.0`) re-evaluates the identical
    population of spans already sampled in under the prior version, rather
    than re-randomizing the sample -- matching ADR 005 section 3's stated
    intent that a version bump should produce new jobs for already-
    evaluated spans, not a freshly-randomized subset of them.

    Boundary behavior: `sampling_rate <= 0.0` always returns `False` and
    `sampling_rate >= 1.0` always returns `True`, both special-cased to
    skip the hash entirely and to avoid relying on a floating-point edge
    case at either boundary of the `[0, 1)`-normalized bucket below.
    """
    if sampling_rate <= 0.0:
        return False
    if sampling_rate >= 1.0:
        return True

    key = f"{project_id}:{trace_id}:{span_id}:{evaluator_name}".encode()
    digest = hashlib.sha256(key).digest()
    # First 8 bytes of the digest, as an unsigned 64-bit integer, normalized
    # into [0, 1).
    bucket = int.from_bytes(digest[:8], "big") / (2**64)
    return bucket < sampling_rate
