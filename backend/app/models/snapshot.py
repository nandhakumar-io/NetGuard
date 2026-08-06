import uuid

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class ConfigSnapshot(Base):
    """Immutable, encrypted backup of a device configuration taken before deployment."""

    __tablename__ = "config_snapshots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False)
    change_request_id = Column(UUID(as_uuid=True), ForeignKey("change_requests.id"), nullable=True)

    running_config_encrypted = Column(Text, nullable=False)
    startup_config_encrypted = Column(Text, nullable=True)
    checksum = Column(String, nullable=False)
    version = Column(String, nullable=False)  # e.g. incrementing version or git-style hash

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Monotonic, DB-assigned tiebreaker for "newest first" ordering.
    # created_at (func.now()) can tie when two snapshots are written in
    # rapid succession -- e.g. deployment_engine snapshotting immediately
    # before and after a rollback -- which made list_snapshots'
    # `order_by(created_at.desc())` return an arbitrary (and sometimes
    # wrong) order for same-tick rows. seq is a DB-generated identity value
    # (not just autoincrement=True, which SQLAlchemy only honors for a
    # single-column integer primary key -- on any other column, including
    # this one, it silently does nothing and leaves seq as a bare NOT NULL
    # column with no default), so ordering by it is always correct
    # regardless of timestamp resolution, and callers never need to set it.
    # Monotonic tiebreaker for "newest first" ordering. created_at
    # (func.now()) can tie when two snapshots are written in rapid
    # succession -- e.g. deployment_engine snapshotting immediately before
    # and after a rollback -- which made list_snapshots'
    # `order_by(created_at.desc())` return an arbitrary (and sometimes
    # wrong) order for same-tick rows.
    #
    # NOTE: this is deliberately *not* relying on `autoincrement=True` --
    # SQLAlchemy only honors that for a single-column integer primary key
    # (the PK here is the UUID `id` column), so on this column it's a
    # silent no-op that leaves seq as a bare NOT NULL column with no way to
    # get a value. It's also deliberately not a Postgres IDENTITY column:
    # those can't be declared nullable, which breaks portability to the
    # SQLite engine the test suite uses. Instead callers must set it
    # explicitly -- see snapshot_service.next_seq().
    seq = Column(BigInteger, nullable=False)
