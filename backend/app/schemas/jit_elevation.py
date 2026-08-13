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
    # Blast radius: this app's RBAC is role-based with no per-device
    # scoping (see app.api.rbac.PERMISSION_MATRIX), so any capability a
    # role gains applies fleet-wide. blast_radius_devices is the current
    # total device count when the grant confers at least one new
    # capability the requester doesn't already have, else 0.
    capabilities_gained: list[str] = []
    blast_radius_devices: int = 0
