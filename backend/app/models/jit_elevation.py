import enum
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class JitElevationStatus(str, enum.Enum):
    PENDING = "pending"  # requested, awaiting a NETWORK_ADMIN's approval
    ACTIVE = "active"  # approved and within its time window
    EXPIRED = "expired"  # window elapsed without being revoked (set lazily -- see jit_service.is_active)
    REVOKED = "revoked"  # ended early by an admin
    REJECTED = "rejected"  # approval denied


class JitElevation(Base):
    """A temporary, time-bound grant of an elevated role to a user --
    Just-In-Time access. Lets a NETWORK_ENGINEER/NOC_ENGINEER/SECURITY
    user get NETWORK_ADMIN-only capability (e.g. to push their own
    approved change request) for a short, explicit window instead of
    holding that role permanently, without an admin having to babysit
    the action in person.

    Deliberately modeled as its own approval workflow rather than folded
    into ChangeRequest approval: an elevation grants a *role* for a
    *duration*, which can outlive or be requested independent of any
    single change (e.g. "give me admin for the next hour to handle this
    incident"), even though change_request_id is the common case and is
    what change_requests.py's own approve action is expected to key off.

    require_roles() (app.core.deps) checks for an active row here in
    addition to the user's base User.role -- see jit_service.is_active
    for what "active" means (approved status AND within the time
    window; expiry is evaluated at check-time, not by a background job,
    so a grant simply stops working the instant it lapses rather than
    needing a sweep task to catch it).
    """

    __tablename__ = "jit_elevations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)

    # The role being temporarily granted, e.g. "network_admin". Stored as
    # plain text (not app.models.user.UserRole) so a future role can be
    # requested here without an enum/migration round-trip -- validated
    # against UserRole at the API layer instead (see schemas/jit_elevation.py).
    elevated_role = Column(String, nullable=False)

    reason = Column(Text, nullable=False)
    change_request_id = Column(UUID(as_uuid=True), ForeignKey("change_requests.id"), nullable=True, index=True)

    # Device/operation scoping (Section 10 follow-up from Phase 1: JIT
    # grants were fleet-wide -- any capability the elevated role has
    # applied to every device). When device_id is set, the grant only
    # authorizes actions against that one device; NULL preserves the old
    # fleet-wide behavior for grants that genuinely need it (e.g. "give
    # me admin for the next hour to handle this incident" with no single
    # device in mind). scoped_operation narrows further to one
    # DeviceOperation (app.schemas.device_job) when set; NULL means any
    # operation the elevated role's permissions allow. Device Gateway's
    # validator.py enforces both independently of the API -- see its
    # module docstring, which previously documented this as unenforceable.
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True, index=True)
    scoped_operation = Column(String, nullable=True)

    requested_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    requested_at = Column(DateTime(timezone=True), server_default=func.now())
    requested_duration_minutes = Column(Integer, nullable=False)  # requested window length, echoed back for the reviewer

    status = Column(Enum(JitElevationStatus), nullable=False, default=JitElevationStatus.PENDING, index=True)

    # Fed from the linked ChangeRequest's own risk scoring (blast radius +
    # reachability impact -- app.services.impact_simulation_service /
    # topology_service.compute_blast_radius, same computation
    # ChangeRequest.requires_dual_approval already keys off). Set once, at
    # request time, by jit_service.request_elevation -- see its
    # _danger_context helper. dual_approval_reason mirrors
    # ChangeRequest.dual_approval_reason's convention ("Critical Risk",
    # "Blast Radius", or both) so the two surfaces read consistently.
    requires_dual_approval = Column(Boolean, nullable=False, default=False, server_default="false")
    dual_approval_reason = Column(String, nullable=True)
    # Same two-step shape as ChangeRequest.first_approved_by/at: the first
    # NETWORK_ADMIN's approval is recorded here without activating the
    # grant; a second, *different* admin must approve again (see
    # jit_service.approve_elevation).
    first_approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    first_approved_at = Column(DateTime(timezone=True), nullable=True)

    decided_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(Text, nullable=True)

    # Only set once approved -- a pending/rejected row never has a window.
    activated_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)

    revoked_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    # Set the first (and only) time the "expiring soon" notification fires
    # for this row (see jit_service.sweep_expiry_notifications) -- keeps a
    # periodic sweep from re-sending the same warning every tick. Untouched
    # by revoke/reject/actual-expiry; those are separate one-shot events.
    expiry_warning_sent_at = Column(DateTime(timezone=True), nullable=True)
