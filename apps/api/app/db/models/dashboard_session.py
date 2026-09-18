import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class DashboardSession(Base, TimestampMixin):
    """A dashboard user's login session (Phase 4D, F1) -- entirely separate
    from `APIKey` (customer telemetry-API authentication). Mirrors
    `APIKey`'s own shape deliberately: `token_hash` is the only thing ever
    compared against a presented session token (see
    app/security/sessions.py), never the raw token, which is not
    persisted anywhere once issued.

    Stateful by design, not a self-contained/stateless token: `expires_at`
    bounds a session's natural lifetime, and `revoked_at` lets a logout (or
    a future admin action) invalidate a session immediately, before its
    natural expiry -- something a signed/stateless token cannot do without
    a separate revocation list, which this table already is.
    """

    __tablename__ = "dashboard_sessions"
    __table_args__ = (Index("ix_dashboard_sessions_user_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
