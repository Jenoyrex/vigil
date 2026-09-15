"""`POST /v1/provisioning/bootstrap` -- production provisioning/onboarding
(Phase 4D, F3). Closes the production-readiness gap docs/decisions/
006-deployment-architecture.md's "Known limitations" named: the only
existing way to mint an organization/project/API key was
`apps/api/scripts/seed_local_api_key.py`, a script requiring direct
database access, with no HTTP equivalent for a production deployment where
an operator may not have (or want) a database shell open.

**This is bootstrap provisioning for a single, trusted operator -- not a
public signup system.** There is no user-facing authentication anywhere in
this codebase (no passwords, no sessions, no OAuth), and this endpoint does
not add one. It is protected by a dedicated, environment-configured secret
(`app.api.deps.get_bootstrap_auth`, `X-Vigil-Bootstrap-Token` -- a wholly
separate trust boundary from a customer's `Authorization: Bearer vgl_*`
key, structurally identical to `app.api.deps.get_internal_service_auth`'s
existing internal-worker-token pattern) that is empty/disabled by default,
and it can succeed at most once for the lifetime of a deployment
(`app.services.provisioning.bootstrap_provisioning`'s database-enforced
concurrency gate -- see that function's docstring for the full argument).
A customer API key can never satisfy `get_bootstrap_auth`, and this
endpoint never accepts or looks at one.

Layering matches every other route module in this API: rate limiting (here,
`app.api.rate_limit.require_bootstrap_rate_limit`, IP-keyed -- a bootstrap
request has no authenticated identity to key on) -> authentication
(`get_bootstrap_auth`) -> validation (`app.schemas.provisioning`) ->
`app.services.provisioning` -> status-code mapping. Rate limiting is
listed BEFORE authentication in this route's `dependencies=[...]` (FastAPI
resolves them in that order) deliberately: a caller guessing at
`bootstrap_secret` must be throttled by IP regardless of whether any given
guess is right or wrong, or the rate limit would do nothing to slow brute
forcing an invalid secret.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_bootstrap_auth
from app.api.rate_limit import require_bootstrap_rate_limit
from app.db.session import get_db
from app.schemas.provisioning import BootstrapRequest, BootstrapResponse
from app.services.provisioning import bootstrap_provisioning

logger = logging.getLogger(__name__)

router = APIRouter(tags=["provisioning"])


@router.post(
    "/v1/provisioning/bootstrap",
    response_model=BootstrapResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_bootstrap_rate_limit), Depends(get_bootstrap_auth)],
    summary="One-time bootstrap: create the first organization, project, and API key",
    description=(
        "Requires `X-Vigil-Bootstrap-Token` to match the operator-configured "
        "VIGIL_API_BOOTSTRAP_SECRET -- disabled entirely (401 for every "
        "request) when that setting is unset, which is its default. "
        "Succeeds at most once per deployment: a second call, concurrent or "
        "later, always receives 409. The returned `api_key` is shown "
        "exactly once and cannot be retrieved again -- see the response "
        "schema and apps/api/README.md's Provisioning section."
    ),
    responses={
        401: {
            "description": (
                "Missing/invalid bootstrap token, or bootstrap is disabled "
                "(VIGIL_API_BOOTSTRAP_SECRET is not configured)."
            )
        },
        409: {"description": "Bootstrap has already been completed for this deployment."},
        422: {"description": "Invalid organization/project name or slug."},
        429: {
            "description": "Rate limit exceeded for bootstrap attempts. See the Retry-After header."
        },
    },
)
def bootstrap(payload: BootstrapRequest, db: Session = Depends(get_db)) -> BootstrapResponse:
    try:
        result = bootstrap_provisioning(
            db,
            organization_name=payload.organization_name,
            organization_slug=payload.organization_slug,
            project_name=payload.project_name,
            project_slug=payload.project_slug,
            api_key_name=payload.api_key_name,
        )
    except IntegrityError as exc:
        # Should be rare now that app.services.provisioning.
        # bootstrap_provisioning checks `organizations` up front: the only
        # way to still reach a unique-constraint collision here is two
        # concurrent bootstrap requests that both passed that check (both
        # saw an empty `organizations` table) and both chose the identical
        # organization_slug/project_slug -- never surfaced as a raw
        # 500/driver exception regardless. db.rollback() is required before
        # this Session can be used again: a failed flush leaves it unusable
        # until then.
        db.rollback()
        logger.warning(
            "provisioning bootstrap failed: slug already in use",
            extra={
                "organization_slug": payload.organization_slug,
                "project_slug": payload.project_slug,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Unable to provision: an organization or project with this slug may already exist."
            ),
        ) from exc

    if result is None:
        logger.info("provisioning bootstrap rejected: already completed")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Bootstrap has already been completed for this deployment.",
        )

    return BootstrapResponse(
        organization_id=result.organization_id,
        project_id=result.project_id,
        api_key_id=result.api_key_id,
        api_key=result.api_key,
    )
