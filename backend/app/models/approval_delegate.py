"""Approval Delegates: "while I'm out, N acts in my place".

Segregation-of-duties roles (Manager for MANAGER_SIGNOFF, and to a
lesser extent Peer Review) are often held by very few people. If the
one Manager eligible for a stage is on PTO, out sick, or simply
unreachable, a chain that *requires* their sign-off has no path forward
-- which either stalls a legitimate, time-sensitive change indefinitely,
or (worse) pressures someone into a workaround that defeats the actual
compliance intent (two distinct people, one technical + one business).

An ApprovalDelegate is an explicit, auditable "B may act for A" mapping
that a delegator sets up themselves (never something one user grants
another user). It's scoped to a stage_type (so a Manager can delegate
manager_signoff without also handing away peer_review authority they
may not even hold) and has an optional time window (start/end), so a
delegation can be set up ahead of a known absence and expire on its own
rather than needing to be remembered and revoked later.

Delegation never bypasses segregation of duties -- it only widens *who*
counts as eligible for a stage; approval_chain_service.act_on_current_stage
still enforces that the actor isn't the change's submitter and hasn't
already acted on an earlier stage of the same chain, and every action
taken via a delegation is recorded against the delegate's own user id
plus which delegator's authority it was exercised under, so the audit
trail never has to guess.
"""
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base
from app.models.approval_chain import ApprovalStageType


class ApprovalDelegate(Base):
    __tablename__ = "approval_delegates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Whose approval authority is being delegated. The delegator is the
    # only one who may create/revoke this row (see api/approval_delegates.py)
    # -- nobody can grant themselves someone else's sign-off authority.
    delegator_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    # Who may act in the delegator's place while this delegation is active.
    delegate_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)

    # Which stage type this covers. Not nullable -- a delegation is always
    # scoped to one stage type, so a Manager delegating manager_signoff
    # can't accidentally also hand away peer_review eligibility (or vice
    # versa for a Peer Review-eligible engineer).
    stage_type = Column(Enum(ApprovalStageType), nullable=False)

    # Optional time window. Both null = active indefinitely, until revoked.
    starts_at = Column(DateTime(timezone=True), nullable=True)
    ends_at = Column(DateTime(timezone=True), nullable=True)

    # Soft-revoke flag, kept alongside the time window rather than a hard
    # delete, so a resolved chain's audit trail can still show exactly
    # whose delegated authority a past approval was exercised under even
    # after the delegation itself is no longer active.
    active = Column(Boolean, nullable=False, default=True, server_default="true")

    reason = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # A delegator may have at most one *active* delegate per stage
        # type at a time -- prevents an ambiguous "who's actually
        # covering for me" state. (Enforced in application code, since a
        # DB-level partial unique index on active=true varies by dialect
        # support; see approval_delegate_service.create_delegate.)
        UniqueConstraint("delegator_id", "delegate_id", "stage_type", "id", name="uq_approval_delegate_natural"),
    )
