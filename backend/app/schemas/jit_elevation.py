from pydantic import BaseModel, Field

from app.models.user import UserRole


class JitElevationRequest(BaseModel):
    elevated_role: UserRole
    reason: str = Field(min_length=3, max_length=2000)
    duration_minutes: int = Field(gt=0, le=480, description="Requested window length, in minutes (max 8h).")
    change_request_id: str | None = None


class JitDecisionRequest(BaseModel):
    note: str | None = None


class JitElevationRead(BaseModel):
    id: str
    user_id: str
    user_email: str | None = None
    elevated_role: str
    reason: str
    change_request_id: str | None = None
    requested_by: str
    requested_at: str | None = None
    requested_duration_minutes: int
    status: str
    decided_by: str | None = None
    decided_at: str | None = None
    decision_note: str | None = None
    activated_at: str | None = None
    expires_at: str | None = None
    revoked_by: str | None = None
    revoked_at: str | None = None
    is_active_now: bool = False
    seconds_remaining: int | None = None
    is_stale: bool = False  # ACTIVE in the DB but expires_at has already lapsed (sweep hasn't caught it yet)
    time_to_approve_seconds: float | None = None  # requested_at -> decided_at, null while still pending
    # Blast radius: this app's RBAC is role-based with no per-device
    # scoping (see app.api.rbac.PERMISSION_MATRIX), so any capability a
    # role gains applies fleet-wide. blast_radius_devices is the current
    # total device count when the grant confers at least one new
    # capability the requester doesn't already have, else 0.
    capabilities_gained: list[str] = []
    blast_radius_devices: int = 0
    # Danger feedback from the linked change request (see
    # jit_service._danger_context) -- fleet-wide RBAC blast radius above
    # is a separate, always-on signal; these two reflect only what this
    # specific change request's own risk scoring flagged.
    requires_dual_approval: bool = False
    dual_approval_reason: str | None = None
    first_approved_by: str | None = None
    first_approved_at: str | None = None
    is_first_approval_needed: bool = False
