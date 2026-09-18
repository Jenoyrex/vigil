"""Business logic for `POST /v1/provisioning/bootstrap` (Phase 4D, F3;
extended in Phase 4D F1 to also create the first dashboard user) --
production provisioning/onboarding. See docs/decisions/
006-deployment-architecture.md's "Known limitations" (the gap this closes)
and app/api/v1/provisioning.py's module docstring for the endpoint's full
security model. This module owns only the database work; authentication
(`app.api.deps.get_bootstrap_auth`) and rate limiting
(`app.api.rate_limit.require_bootstrap_rate_limit`) both happen before a
request ever reaches here.

**This is bootstrap provisioning, not a signup system.** It creates exactly
one organization, one project, one API key, and (as of Phase 4D F1) one
dashboard owner user + owner membership, and is designed to succeed at
most once ever for a given deployment -- see `bootstrap_provisioning`'s
own docstring for the exact concurrency/atomicity argument. There is no
way to provision a *second* tenant, or a second/additional user, through
this endpoint -- ADR 005/006's existing "single-operator architecture"
framing is unchanged, not extended, by this module; this remains a
one-time bootstrap action, not an ongoing signup/invite system.

**Why user/membership creation belongs here now, when it previously did
not.** This module's own prior version documented, correctly at the time,
that introducing a user here would mean "inventing who that user is" for
"zero functional benefit" -- true when this API had no user-facing
authentication at all. Phase 4D F1 adds real dashboard login
(`app.services.auth`, backed by `users.hashed_password`/
`organization_memberships`), which needs a first user to exist
*somewhere*; bootstrap is that place, for the same reason it is already
the place `organizations`/`projects`/`api_keys` are minted -- a single,
atomic, one-time, database-enforced-safe-under-concurrency action, not a
second, parallel "create a user" endpoint with its own security model to
maintain.

**The invariant is "no organization exists yet," not merely "the
`provisioning_bootstrap` marker row is absent."** These were briefly
conflated in an earlier draft of this endpoint, which documented
`DELETE FROM provisioning_bootstrap;` as a way to re-enable bootstrap --
wrong, because deleting only that row does not delete the organization/
project/API key it recorded, so a second bootstrap through the HTTP
endpoint would then create a *second*, independent tenant/key while the
first one silently kept existing. `bootstrap_provisioning` now checks
`organizations` directly, first, before attempting anything: if any
organization row exists -- for any reason, including one the marker no
longer references -- bootstrap refuses. The marker row still exists and
is still written on success (useful as an audit/diagnostic pointer to
which organization resulted, and still the mechanism that makes a
*successful* run atomic under concurrency -- see below), but it is no
longer the sole or primary gate. There is consequently no way to
re-enable bootstrap through the HTTP endpoint, or by deleting only that
one row, short of a full, destructive teardown of the organization itself
(see apps/api/README.md's "Provisioning" section).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import (
    APIKey,
    Organization,
    OrganizationMembership,
    Project,
    ProvisioningBootstrap,
    User,
)
from app.db.models.provisioning_bootstrap import BOOTSTRAP_ROW_ID
from app.security.api_keys import generate_api_key
from app.security.passwords import hash_password

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BootstrapResult:
    organization_id: uuid.UUID
    project_id: uuid.UUID
    api_key_id: uuid.UUID
    api_key: str
    user_id: uuid.UUID
    owner_email: str


def bootstrap_provisioning(
    db: Session,
    *,
    organization_name: str,
    organization_slug: str,
    project_name: str,
    project_slug: str,
    api_key_name: str,
    owner_email: str,
    owner_full_name: str | None,
    owner_password: str,
) -> BootstrapResult | None:
    """Creates one organization, one project, one API key, one dashboard
    owner user, and one owner-role membership, all in a single transaction,
    and returns the result -- including the plaintext API key, which
    exists only in this return value and the HTTP response built from it;
    `app.security.api_keys.generate_api_key` never persists it, and
    nothing in this function does either. `owner_password` is likewise
    never returned or logged -- only its `hash_password` output is ever
    persisted, on the new `User` row.

    Returns `None` if bootstrap has already run, or an organization already
    exists for any other reason (mapped to a 409 by the caller) -- never
    raises for this expected, routine outcome.

    **Two layered checks, not one.** (1) A cheap, up-front
    `organizations` existence check -- the authoritative "has a tenant
    already been provisioned" answer, checked before touching anything
    else, so a losing/repeat caller never even generates API-key material.
    This is what makes the invariant survive someone deleting only the
    `provisioning_bootstrap` marker row directly (see the module
    docstring): the organization itself is still there, so this check
    still refuses. (2) The `provisioning_bootstrap` `INSERT ... ON
    CONFLICT (id) DO NOTHING` claim below, which remains the mechanism
    that makes a *successful* run safe under concurrency -- see the next
    paragraph. Check (1) alone would be a classic check-then-insert race
    under concurrent requests (two transactions can both observe an empty
    `organizations` table before either commits); check (2) is what closes
    that race with a real database guarantee. Together: no repeat run can
    ever succeed (1), and no two concurrent first runs can both succeed
    (2).

    **Atomicity and the concurrency invariant, in one sentence**: nothing
    commits until the very last statement -- an `INSERT ... ON CONFLICT
    (id) DO NOTHING` against `provisioning_bootstrap`'s fixed
    `BOOTSTRAP_ROW_ID` row -- succeeds, so either every row this function
    creates becomes durable together, or (on conflict, or on any exception
    propagating out of this function) none of them do.

    **Why this is safe under concurrent requests, not just checked at the
    application level**: this is the exact "idempotent, atomic insert --
    never check-then-insert, which would race" idiom
    `app.services.evaluations.create_evaluation_job` already uses for
    `evaluation_jobs` in this codebase, applied to a dedicated singleton
    row instead of a natural composite key. PostgreSQL's `INSERT ... ON
    CONFLICT` uses "speculative insertion": when two concurrent
    transactions attempt to insert the same primary key value that neither
    has yet committed, the second blocks on the first's in-flight insert
    (not merely raises immediately) until the first transaction resolves.
    If the first commits, the second's own conflict becomes real and its
    `DO NOTHING` applies -- `RETURNING` yields no row, `claimed_id` below is
    `None`, and this function rolls back everything it built (the
    organization/project/API key it just created) and returns `None`. If
    the first instead rolls back (any failure, anywhere above this point,
    in that or another concurrent request), the second's insert proceeds
    normally. Exactly one of any number of concurrent callers can ever
    observe a non-`None` return for a given deployment's lifetime, and the
    database -- not a Python-level flag, lock, or counter -- is what
    enforces that.

    **Why organization_id is looked up from `organizations` fresh, not from
    the `ProvisioningBootstrap` row's own `organization_id` column at read
    time elsewhere**: that column exists purely as an audit/diagnostic
    pointer ("which organization resulted from bootstrap") -- no other code
    path in this system ever queries it to make an authorization or
    tenant-scoping decision; `app.api.deps.get_current_api_key` resolves
    `project_id` from the presented API key exactly as it always has,
    completely independent of this table's existence.

    **The new user/membership rows are ordinary participants in the same
    transaction, not a special case.** `app.api.deps.get_current_api_key`'s
    own authorization path (customer telemetry-API access) still never
    touches `users`/`organization_memberships` at all -- this function
    creating a `User`/`OrganizationMembership` row changes nothing about
    that; the two authentication paths (customer API key vs. dashboard
    login) remain fully independent, per this phase's investigation
    report. `owner_password` is hashed via `app.security.passwords.
    hash_password` (scrypt) before it ever reaches this function's `User`
    row -- the plaintext value is never persisted, logged, or returned.
    """
    if db.query(Organization.id).first() is not None:
        # Already bootstrapped (via this function, ever), or an operator
        # created an organization some other way (e.g.
        # scripts/seed_local_api_key.py) -- either way, this is not an
        # empty deployment, and this function's entire purpose is
        # provisioning the FIRST one. No row is touched below this point.
        return None

    organization = Organization(name=organization_name, slug=organization_slug)
    db.add(organization)
    db.flush()

    project = Project(organization_id=organization.id, name=project_name, slug=project_slug)
    db.add(project)
    db.flush()

    raw_key, key_prefix, key_hash = generate_api_key()
    api_key = APIKey(
        project_id=project.id, name=api_key_name, key_prefix=key_prefix, key_hash=key_hash
    )
    db.add(api_key)
    db.flush()

    user = User(
        email=owner_email,
        full_name=owner_full_name,
        hashed_password=hash_password(owner_password),
        is_active=True,
    )
    db.add(user)
    db.flush()

    membership = OrganizationMembership(
        user_id=user.id, organization_id=organization.id, role="owner"
    )
    db.add(membership)
    db.flush()

    claim_stmt = (
        pg_insert(ProvisioningBootstrap)
        .values(id=BOOTSTRAP_ROW_ID, organization_id=organization.id)
        .on_conflict_do_nothing(index_elements=["id"])
        .returning(ProvisioningBootstrap.id)
    )
    claimed_id = db.execute(claim_stmt).scalars().first()

    if claimed_id is None:
        # Lost the race (or bootstrap already ran, ever) -- discard
        # everything this call built above; none of it was ever committed.
        db.rollback()
        return None

    db.commit()

    # NEVER the raw key, NEVER the plaintext password, NEVER the bootstrap
    # secret -- only identifiers, the same discipline every other
    # structured log call site in this codebase follows (see
    # app/logging_config.py's security note). key_prefix is deliberately
    # safe to log (app/security/api_keys.py's own module docstring: it
    # exists specifically so a key can be identified in logs without
    # exposing the secret half); user.email is likewise an identifier, not
    # a secret, and safe to log for the same reason.
    logger.info(
        "provisioning bootstrap completed",
        extra={
            "organization_id": str(organization.id),
            "project_id": str(project.id),
            "api_key_id": str(api_key.id),
            "api_key_prefix": key_prefix,
            "user_id": str(user.id),
            "owner_email": user.email,
        },
    )

    return BootstrapResult(
        organization_id=organization.id,
        project_id=project.id,
        api_key_id=api_key.id,
        api_key=raw_key,
        user_id=user.id,
        owner_email=user.email,
    )
