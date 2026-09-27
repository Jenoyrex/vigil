"""Request/response schemas for the session-authenticated workspace routes
(app/api/v1/workspace.py): the signed-in user, their organizations and
projects, and a project's API keys."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, StringConstraints


class ProjectOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    created_at: datetime


class OrganizationOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    role: str
    projects: list[ProjectOut]


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str | None


class MeResponse(BaseModel):
    user: UserOut
    organizations: list[OrganizationOut]


class NameRequest(BaseModel):
    # Stripped before the length check, so a whitespace-only name is a 422.
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class APIKeyOut(BaseModel):
    """Never includes `key_hash` -- only the non-secret prefix identifies a key."""

    id: uuid.UUID
    name: str
    key_prefix: str
    status: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class APIKeyListResponse(BaseModel):
    items: list[APIKeyOut]


class APIKeyCreateResponse(APIKeyOut):
    """`api_key` is the raw key -- returned exactly once, here. Only its
    SHA-256 hash is stored (app/security/api_keys.py)."""

    api_key: str
