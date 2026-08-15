"""Just-In-Time (JIT) Access: request, approve, reject, revoke, and list
temporary role elevations. See app.models.jit_elevation and
app.services.jit_service for the underlying model/lifecycle, and
app.core.deps.require_roles for where an approved grant actually takes
effect.

Any authenticated user may request an elevation for themselves. Only
NETWORK_ADMIN may approve/reject/revoke -- checked against the user's
*base* User.role (not require_roles' JIT-aware variant), so a JIT grant
can never be used to approve or extend more JIT access.
"""
import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.rbac import PERMISSION_MATRIX
from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.device import Device
from app.models.jit_elevation import JitElevation, JitElevationStatus
from app.models.user import User, UserRole
from app.schemas.jit_elevation import (
    JitDecisionRequest,
    JitElevationRead,
    JitElevationRequest,
)
from app.services import jit_service

router = APIRouter(prefix="/jit-access", tags=["jit-access"])

_admin_only = require_roles(UserRole.NETWORK_ADMIN)


def _capabilities_gained(current_role: str | None, elevated_role: str) -> list[str]:
    """Resources the elevated role can reach that the requester's own
    base role cannot -- i.e. what this specific grant actually adds, not
    just everything the elevated role can do."""
    gained = []
    for entry in PERMISSION_MATRIX:
        roles = entry["roles"]
        if elevated_role in roles and current_role not in roles:
            gained.append(entry["resource"])
    return gained


def _hydrate(db: Session, rows: list[JitElevation]) -> list[JitElevationRead]:
    if not rows:
        return []
    user_ids = {r.user_id for r in rows} | {r.requested_by for r in rows}
    users_by_id = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()}
    now = datetime.datetime.now(datetime.timezone.utc)
    # Grants are role-based and unscoped to specific devices (see
    # PERMISSION_MATRIX), so any new capability applies to the whole
    # fleet -- one count covers every row being hydrated.
    total_devices = db.query(Device).count()

    out = []
    for r in rows:
        active_now = jit_service.is_active_now(r, now)
        seconds_remaining = None
        if active_now and r.expires_at:
            expires_at = r.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
            seconds_remaining = max(0, int((expires_at - now).total_seconds()))

        requester = users_by_id.get(r.user_id)
        current_role = requester.role.value if requester and hasattr(requester.role, "value") else (requester.role if requester else None)
        elevated_role_value = r.elevated_role.value if hasattr(r.elevated_role, "value") else r.elevated_role
        capabilities_gained = _capabilities_gained(current_role, elevated_role_value)
        blast_radius_devices = total_devices if capabilities_gained else 0

        out.append(
            JitElevationRead(
                id=str(r.id),
                user_id=str(r.user_id),
                user_email=users_by_id[r.user_id].email if r.user_id in users_by_id else None,
                elevated_role=r.elevated_role,
                reason=r.reason,
                change_request_id=str(r.change_request_id) if r.change_request_id else None,
                requested_by=str(r.requested_by),
                requested_at=r.requested_at.isoformat() if r.requested_at else None,
                requested_duration_minutes=r.requested_duration_minutes,
                status=r.status.value if hasattr(r.status, "value") else r.status,
                decided_by=str(r.decided_by) if r.decided_by else None,
                decided_at=r.decided_at.isoformat() if r.decided_at else None,
                decision_note=r.decision_note,
                activated_at=r.activated_at.isoformat() if r.activated_at else None,
                expires_at=r.expires_at.isoformat() if r.expires_at else None,
                revoked_by=str(r.revoked_by) if r.revoked_by else None,
                revoked_at=r.revoked_at.isoformat() if r.revoked_at else None,
                is_active_now=active_now,
                seconds_remaining=seconds_remaining,
                is_stale=jit_service.is_stale(r, now),
                time_to_approve_seconds=jit_service.time_to_approve_seconds(r),
                capabilities_gained=capabilities_gained,
                blast_radius_devices=blast_radius_devices,
                requires_dual_approval=r.requires_dual_approval,
                dual_approval_reason=r.dual_approval_reason,
                first_approved_by=str(r.first_approved_by) if r.first_approved_by else None,
                first_approved_at=r.first_approved_at.isoformat() if r.first_approved_at else None,
                is_first_approval_needed=r.requires_dual_approval and r.first_approved_by is None,
            )
        )
    return out


def _get_or_404(db: Session, elevation_id: str) -> JitElevation:
    try:
        elevation_uuid = uuid.UUID(elevation_id)
    except ValueError:
        raise HTTPException(404, "Elevation not found")
    elevation = db.get(JitElevation, elevation_uuid)
    if elevation is None:
        raise HTTPException(404, "Elevation not found")
    return elevation


@router.post("/request", response_model=JitElevationRead)
def request_jit_access(
    payload: JitElevationRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Request a temporary elevation to `elevated_role` for yourself.
    Requesting your own current role (or "downward") is allowed but
    pointless -- left unvalidated since it's harmless and approving it is
    a no-op once granted.
    """
    change_request_uuid = None
    if payload.change_request_id:
        try:
            change_request_uuid = uuid.UUID(payload.change_request_id)
        except ValueError:
            raise HTTPException(400, "Invalid change_request_id")

    elevation = jit_service.request_elevation(
        db,
        user_id=user.id,
        elevated_role=payload.elevated_role.value,
        reason=payload.reason,
        duration_minutes=payload.duration_minutes,
        change_request_id=change_request_uuid,
        requested_by_email=user.email,
    )
    return _hydrate(db, [elevation])[0]


@router.get("/mine", response_model=list[JitElevationRead])
def list_my_elevations(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    jit_service.mark_expired_elevations(db)
    rows = (
        db.query(JitElevation)
        .filter(JitElevation.user_id == user.id)
        .order_by(JitElevation.requested_at.desc())
        .all()
    )
    return _hydrate(db, rows)


@router.get("/metrics")
def get_approval_metrics(days: int = 30, db: Session = Depends(get_db), _: User = Depends(_admin_only)):
    """Time-to-approve (mean/median/p90) over the last `days`, plus a
    live count of stale-active grants -- backs a small card on the
    RBAC/JIT audit page. Sweeps expired rows first so the stale count
    reflects grants the sweep genuinely hasn't caught yet, not ones that
    just haven't been viewed since lapsing.
    """
    jit_service.mark_expired_elevations(db)
    return jit_service.approval_metrics(db, days=days)


@router.get("/pending", response_model=list[JitElevationRead])
def list_pending_elevations(db: Session = Depends(get_db), _: User = Depends(_admin_only)):
    """Approval queue -- every elevation awaiting a decision."""
    rows = (
        db.query(JitElevation)
        .filter(JitElevation.status == JitElevationStatus.PENDING)
        .order_by(JitElevation.requested_at.asc())
        .all()
    )
    return _hydrate(db, rows)


@router.get("", response_model=list[JitElevationRead])
def list_all_elevations(db: Session = Depends(get_db), _: User = Depends(_admin_only)):
    """Full history across all users -- for the RBAC/audit surface."""
    jit_service.mark_expired_elevations(db)
    rows = db.query(JitElevation).order_by(JitElevation.requested_at.desc()).limit(500).all()
    return _hydrate(db, rows)


@router.post("/{elevation_id}/approve", response_model=JitElevationRead)
def approve_jit_access(
    elevation_id: str,
    payload: JitDecisionRequest,
    db: Session = Depends(get_db),
    approver: User = Depends(_admin_only),
):
    elevation = _get_or_404(db, elevation_id)
    if elevation.status != JitElevationStatus.PENDING:
        raise HTTPException(409, f"Elevation is '{elevation.status.value}', not pending -- nothing to approve")
    try:
        elevation = jit_service.approve_elevation(
            db, elevation, approver_id=approver.id, approver_email=approver.email, note=payload.note
        )
    except jit_service.JitAlreadyApprovedByYouError as exc:
        raise HTTPException(400, str(exc))
    return _hydrate(db, [elevation])[0]


@router.post("/{elevation_id}/reject", response_model=JitElevationRead)
def reject_jit_access(
    elevation_id: str,
    payload: JitDecisionRequest,
    db: Session = Depends(get_db),
    approver: User = Depends(_admin_only),
):
    elevation = _get_or_404(db, elevation_id)
    if elevation.status != JitElevationStatus.PENDING:
        raise HTTPException(409, f"Elevation is '{elevation.status.value}', not pending -- nothing to reject")
    elevation = jit_service.reject_elevation(
        db, elevation, approver_id=approver.id, approver_email=approver.email, note=payload.note
    )
    return _hydrate(db, [elevation])[0]


@router.post("/{elevation_id}/revoke", response_model=JitElevationRead)
def revoke_jit_access(
    elevation_id: str,
    payload: JitDecisionRequest,
    db: Session = Depends(get_db),
    revoker: User = Depends(_admin_only),
):
    """Ends an ACTIVE grant early -- e.g. the incident it was granted for
    is already resolved. A no-op safety check rejects anything not
    currently active rather than silently "revoking" a grant that was
    never live or has already lapsed/been rejected.
    """
    elevation = _get_or_404(db, elevation_id)
    if elevation.status != JitElevationStatus.ACTIVE or not jit_service.is_active_now(elevation):
        raise HTTPException(409, f"Elevation is '{elevation.status.value}', not active -- nothing to revoke")
    elevation = jit_service.revoke_elevation(
        db, elevation, revoker_id=revoker.id, revoker_email=revoker.email, note=payload.note
    )
    return _hydrate(db, [elevation])[0]
