from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit naming convention so constraint/index names are stable and predictable
# across Alembic autogenerate runs, instead of relying on database-assigned defaults.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """created_at/updated_at columns shared by every table.

    updated_at is maintained via SQLAlchemy's `onupdate`, not a PostgreSQL
    trigger: the ORM includes `now()` in the UPDATE statement it issues, so the
    timestamp is still computed by the database (avoiding client clock skew)
    while the *decision* to update it stays at the application layer. The
    tradeoff is that rows changed outside the ORM (raw SQL) won't refresh
    updated_at automatically; that's acceptable while all writes go through
    SQLAlchemy.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
