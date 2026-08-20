"""User Management: the admin-facing "who has an account, what can they
do, are they still active" page. Complements two things that already
existed but didn't add up to full CRUD:

  - GET /rbac/users (app.api.rbac) -- read-only, audit-activity-focused,
    for NETWORK_ADMIN and AUDITOR both.
  - PATCH /auth/users/{id}/role (app.api.auth) -- role changes only, no
    create/disable/delete.

This router adds the missing create/disable-enable/delete surface, and a
list endpoint shaped for the User Management page specifically (role
cards with live counts, not audit-activity columns -- see GET /users).
Restricted to NETWORK_ADMIN only (not AUDITOR) since every endpoint here
except the list is a write.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_roles
from app.core.security import hash_password
from app.models.user import User, UserRole
from app.schemas.user_management import (
    AdminUserCreate,
    AdminUserListResponse,
    AdminUserRead,
    UserPermissionsUpdate,
    UserRoleCounts,
    UserStatusUpdate,
)
from app.services import audit_service, session_revocation_service

router = APIRouter(prefix="/users", tags=["users"])

_admin_only = require_roles(UserRole.NETWORK_ADMIN)


def _is_active(user: User) -> bool:
    return str(user.is_active).lower() in ("true", "1")


def _parse_extra_roles(user: User) -> list[UserRole]:
    if not user.extra_roles:
        return []
    out = []
    for raw in user.extra_roles.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(UserRole(raw))
        except ValueError:
            continue  # tolerate a stale/unknown value rather than 500ing the whole list
    return out


def _serialize(user: User) -> AdminUserRead:
    return AdminUserRead(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        extra_roles=_parse_extra_roles(user),
        is_active=_is_active(user),
        mfa_enabled=str(user.mfa_enabled).lower() == "true",
        sso_provider=user.sso_provider,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


def _active_admin_count(db: Session) -> int:
    return sum(1 for u in db.query(User).filter(User.role == UserRole.NETWORK_ADMIN).all() if _is_active(u))


@router.get("", response_model=AdminUserListResponse)
def list_users(db: Session = Depends(get_db), _: User = Depends(_admin_only)):
    users = db.query(User).order_by(User.email).all()
    counts = UserRoleCounts(
        total=len(users),
        network_admin=sum(1 for u in users if u.role == UserRole.NETWORK_ADMIN),
        network_engineer=sum(1 for u in users if u.role == UserRole.NETWORK_ENGINEER),
        noc_engineer=sum(1 for u in users if u.role == UserRole.NOC_ENGINEER),
        security=sum(1 for u in users if u.role == UserRole.SECURITY),
        auditor=sum(1 for u in users if u.role == UserRole.AUDITOR),
        disabled=sum(1 for u in users if not _is_active(u)),
    )
    return AdminUserListResponse(users=[_serialize(u) for u in users], counts=counts)


@router.post("", response_model=AdminUserRead, status_code=201)
def create_user(payload: AdminUserCreate, db: Session = Depends(get_db), current_user: User = Depends(_admin_only)):
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=409, detail="A user with that email already exists")

    user = User(
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=payload.role,
        extra_roles=",".join(r.value for r in payload.extra_roles) or None,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    audit_service.record_event(
        db, actor=current_user.email, action="User Created", result="Success",
        detail=f"{user.email} created with role {user.role.value}"
        + (f" (+{', '.join(r.value for r in payload.extra_roles)})" if payload.extra_roles else ""),
    )
    return _serialize(user)


@router.patch("/{user_id}/permissions", response_model=AdminUserRead)
def update_user_permissions(
    user_id: str, payload: UserPermissionsUpdate, db: Session = Depends(get_db), current_user: User = Depends(_admin_only)
):
    """Sets a user's fine-grained custom permissions: which *other* roles'
    endpoints they can reach in addition to their base `role`, without a
    full role change. Replaces the whole extra_roles set each call (not a
    per-item add/remove) -- the User Management UI always sends the
    complete desired set from a checkbox list, same pattern as
    update_user_status for is_active.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    extra = [r for r in payload.extra_roles if r != user.role]  # granting a user's own base role is a no-op
    user.extra_roles = ",".join(r.value for r in extra) or None
    db.commit()
    db.refresh(user)

    audit_service.record_event(
        db, actor=current_user.email, action="User Permissions Updated", result="Success",
        detail=f"{user.email}: extra_roles={[r.value for r in extra] or 'none'}",
    )
    return _serialize(user)


@router.patch("/{user_id}/status", response_model=AdminUserRead)
def update_user_status(
    user_id: str, payload: UserStatusUpdate, db: Session = Depends(get_db), current_user: User = Depends(_admin_only)
):
    """Enable/disable an account -- a disabled user can no longer log in
    (see app.api.auth.login's password check / app.api.sso's SSO
    provisioning, both of which should treat is_active the same way
    app.api.sso.py already does at line ~86). Kept separate from the role
    field entirely: disabling is reversible and doesn't need to touch or
    remember what role the account had.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.id == current_user.id and not payload.is_active:
        raise HTTPException(status_code=400, detail="You can't disable your own account")

    if (
        not payload.is_active
        and user.role == UserRole.NETWORK_ADMIN
        and _is_active(user)
        and _active_admin_count(db) <= 1
    ):
        raise HTTPException(status_code=400, detail="Can't disable the last active Network Admin")

    user.is_active = payload.is_active
    db.commit()
    db.refresh(user)

    audit_service.record_event(
        db, actor=current_user.email,
        action="User Enabled" if payload.is_active else "User Disabled",
        result="Success", detail=user.email,
    )

    # Disabling an account only blocks *future* logins (see app.api.auth's
    # password-grant / SSO checks against is_active) -- a browser that's
    # already signed in would otherwise keep refreshing its access token
    # indefinitely. Force-revoke every active session on disable so
    # "Disable" actually ends any session in progress, not just new ones.
    if not payload.is_active:
        session_revocation_service.revoke_all_sessions(
            db, user, actor_email=current_user.email, reason="account disabled",
        )

    return _serialize(user)


@router.post("/{user_id}/revoke-sessions")
def revoke_user_sessions(
    user_id: str, db: Session = Depends(get_db), current_user: User = Depends(_admin_only)
):
    """Force sign-out: immediately revokes every active session (refresh
    token) for the target user, independent of their enabled/disabled
    status. Use this for a suspected-compromised account, a lost device,
    or an offboarding step that shouldn't wait for a full disable. Actual
    enforcement is identical to the self-service DELETE
    /auth/sessions/{id} a user can do to their own sessions -- see
    app.services.session_revocation_service for why there's no separate,
    weaker "admin revoke" mechanism.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    revoked_count = session_revocation_service.revoke_all_sessions(
        db, user, actor_email=current_user.email, reason="manual force sign-out",
    )
    return {"revoked_count": revoked_count}


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: str, db: Session = Depends(get_db), current_user: User = Depends(_admin_only)):
    """Hard delete. Only permitted for an already-disabled account, so
    there's always an explicit "turn this off" step first -- one fewer way
    to fat-finger removing someone who's mid-shift. change_request_id
    foreign keys elsewhere (audit_log, change_requests.requested_by, ...)
    stay intact since none of them cascade from users.id -- this account
    simply stops resolving to a name for future lookups on old rows, same
    as any deleted-user handling already has to account for.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="You can't delete your own account")
    if _is_active(user):
        raise HTTPException(status_code=400, detail="Disable the account before deleting it")

    audit_service.record_event(
        db, actor=current_user.email, action="User Deleted", result="Success", detail=user.email,
    )
    db.delete(user)
    db.commit()
