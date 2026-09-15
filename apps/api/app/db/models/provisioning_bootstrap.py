import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# The one well-known row id this table is ever expected to hold -- mirrors
# app/db/models/evaluation_poller_checkpoint.py's identical
# fixed-string-primary-key idiom for a single-row invariant. A fixed PK
# (rather than a random uuid4) is what makes a *successful* bootstrap
# atomic under concurrency: `app/services/provisioning.py` inserts this
# exact id with `ON CONFLICT (id) DO NOTHING`, so at most one concurrent
# transaction's insert can ever win it. It is deliberately NOT the sole
# "has this system been bootstrapped" gate, though -- see the class
# docstring below for why, and app/services/provisioning.py's module
# docstring for the full two-layer argument.
BOOTSTRAP_ROW_ID = "bootstrap"


class ProvisioningBootstrap(Base, TimestampMixin):
    """Records that `POST /v1/provisioning/bootstrap` (Phase 4D, F3) has
    run, and which organization it created -- an audit/diagnostic pointer,
    and (via its fixed `id=BOOTSTRAP_ROW_ID`) the mechanism that makes a
    successful bootstrap atomic under concurrent requests.

    **This row's presence is NOT the sole "has this system been
    bootstrapped" answer -- `organizations` being non-empty is.**
    `app/services/provisioning.py` checks `organizations` directly,
    first, before touching this table at all. An earlier draft of this
    endpoint treated this row's existence as the only gate, and documented
    `DELETE FROM provisioning_bootstrap;` as a way to re-enable bootstrap;
    that was wrong -- deleting only this row leaves the organization/
    project/API key it pointed to fully intact, so a second bootstrap
    would create a *second*, independent tenant/key alongside the first,
    not a clean reset. There is now no way to re-enable bootstrap by
    deleting only this row. See apps/api/README.md's "Provisioning"
    section for the actual (destructive, full-teardown) reset procedure
    for disposable development/staging environments.

    `organization_id`'s `ondelete="RESTRICT"` matches every other tenant
    foreign key in this schema (`projects.organization_id`,
    `api_keys.project_id`) -- deleting the bootstrapped organization while
    this row still references it is refused, never silently cascaded, so
    a full teardown must delete this row as one of its explicit steps
    (never accidentally skippable via cascade).
    """

    __tablename__ = "provisioning_bootstrap"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=BOOTSTRAP_ROW_ID)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
