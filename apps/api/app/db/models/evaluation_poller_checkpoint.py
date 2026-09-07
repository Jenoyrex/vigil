from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# The one well-known row id this table is ever expected to hold -- see the
# class docstring. Using a fixed string primary key (rather than a random
# uuid4, unlike every other table in this schema) makes the single-row
# invariant self-documenting in the schema itself: the worker always reads
# and upserts id=SINGLETON_ROW_ID, never generates a new id.
SINGLETON_ROW_ID = "global"


class EvaluationPollerCheckpoint(Base, TimestampMixin):
    """A single-row watermark tracking how far the worker's poller has
    scanned ClickHouse `spans` (by `ingested_at`) for newly-eligible
    telemetry -- ADR 005 sections 4 and 7.

    Required for correctness, not "might be useful later": without a durable
    checkpoint, a worker restart must either silently skip any span ingested
    during its downtime, or re-scan the full 30-day retention window on every
    restart. This table is worker-owned lifecycle/execution state (ADR 005
    section 10) -- the worker reads and writes it directly, never through
    apps/api's job-creation endpoint.

    No seed row is created by this table's migration. The worker's poller
    (not yet built) is expected to read `id=SINGLETON_ROW_ID`, and if absent,
    create it on first run with `last_ingested_at=NULL` -- treated as "no
    checkpoint yet, start from the beginning of the retention window."
    """

    __tablename__ = "evaluation_poller_checkpoint"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=SINGLETON_ROW_ID)
    last_ingested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
