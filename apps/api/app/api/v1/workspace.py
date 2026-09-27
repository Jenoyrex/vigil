"""Session-authenticated workspace routes for the dashboard's self-serve
onboarding: who am I, create an organization, create a project, manage a
project's API keys.

Authenticated by a dashboard session (`X-Vigil-Session-Token`) only -- never
a customer API key, so a leaked `vgl_*` key can't mint more keys. Every
project/organization id in a path is authorized against the caller's
memberships in app.services.workspace; a non-member gets 404, never 403,
so other tenants' ids reveal nothing.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.rate_limit import require_session_rate_limit
from app.db.session import get_db
from app.schemas.workspace import (
    APIKeyCreateResponse,
    APIKeyListResponse,
    APIKeyOut,
    MeResponse,
    NameRequest,
    OrganizationOut,
    ProjectOut,
)
from app.services import workspace
from app.services.auth import AuthenticatedSession

router = APIRouter(tags=["workspace"])

_RESPONSES = {
    401: {"description": "Invalid or expired session."},
    404: {"description": "Not found, or not a member of its organization."},
    429: {"description": "Rate limit exceeded. See the Retry-After header."},
}


def _call(fn, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
    try:
        return fn(*args, **kwargs)
    except workspace.NotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.") from None
    except workspace.ForbiddenError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only organization owners and admins can do this.",
        ) from None


@router.get("/v1/me", response_model=MeResponse, responses=_RESPONSES)
def me(
    session: AuthenticatedSession = Depends(require_session_rate_limit),
    db: Session = Depends(get_db),
) -> MeResponse:
    return workspace.get_me(db, user_id=session.user_id)


@router.post(
    "/v1/organizations",
    response_model=OrganizationOut,
    status_code=status.HTTP_201_CREATED,
    responses=_RESPONSES,
)
def create_organization(
    payload: NameRequest,
    session: AuthenticatedSession = Depends(require_session_rate_limit),
    db: Session = Depends(get_db),
) -> OrganizationOut:
    return workspace.create_organization(db, user_id=session.user_id, name=payload.name)


@router.post(
    "/v1/organizations/{organization_id}/projects",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
    responses={**_RESPONSES, 403: {"description": "Requires owner or admin role."}},
)
def create_project(
    organization_id: uuid.UUID,
    payload: NameRequest,
    session: AuthenticatedSession = Depends(require_session_rate_limit),
    db: Session = Depends(get_db),
) -> ProjectOut:
    return _call(
        workspace.create_project,
        db,
        user_id=session.user_id,
        organization_id=organization_id,
        name=payload.name,
    )


@router.get(
    "/v1/projects/{project_id}/api-keys", response_model=APIKeyListResponse, responses=_RESPONSES
)
def list_api_keys(
    project_id: uuid.UUID,
    session: AuthenticatedSession = Depends(require_session_rate_limit),
    db: Session = Depends(get_db),
) -> APIKeyListResponse:
    return _call(workspace.list_api_keys, db, user_id=session.user_id, project_id=project_id)


@router.post(
    "/v1/projects/{project_id}/api-keys",
    response_model=APIKeyCreateResponse,
    status_code=status.HTTP_201_CREATED,
    responses={**_RESPONSES, 403: {"description": "Requires owner or admin role."}},
)
def create_api_key(
    project_id: uuid.UUID,
    payload: NameRequest,
    session: AuthenticatedSession = Depends(require_session_rate_limit),
    db: Session = Depends(get_db),
) -> APIKeyCreateResponse:
    return _call(
        workspace.create_api_key,
        db,
        user_id=session.user_id,
        project_id=project_id,
        name=payload.name,
    )


@router.post(
    "/v1/projects/{project_id}/api-keys/{key_id}/revoke",
    response_model=APIKeyOut,
    responses={**_RESPONSES, 403: {"description": "Requires owner or admin role."}},
)
def revoke_api_key(
    project_id: uuid.UUID,
    key_id: uuid.UUID,
    session: AuthenticatedSession = Depends(require_session_rate_limit),
    db: Session = Depends(get_db),
) -> APIKeyOut:
    return _call(
        workspace.revoke_api_key,
        db,
        user_id=session.user_id,
        project_id=project_id,
        key_id=key_id,
    )
