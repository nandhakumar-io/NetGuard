"""Force-revoking *another* user's sessions -- the admin-facing complement
to the self-service DELETE /auth/sessions/{id} in app.api.auth.

Distinct from that self-service path in one important way: this is used
by someone other than the account owner (a NETWORK_ADMIN), typically when
disabling an account or responding to a compromised-credential report.
Uses the exact same enforcement mechanism as self-service revocation --
flipping RefreshToken.revoked -- there's no separate "admin revoke" flag
elsewhere in the codebase to fall out of sync with, and no in-memory
allowlist/denylist that would reset on a process restart or not be shared
across API replicas. Effective immediately for POST /auth/refresh (the
target's browser can no longer mint a new access token); any still-live
access token they're currently holding keeps working until it naturally
expires (ACCESS_TOKEN_EXPIRE_MINUTES), the same bounded window documented
on app.api.auth.revoke_session.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.services import audit_service, notification_service


def revoke_all_sessions(
    db: Session, target_user: User, *, actor_email: str, reason: str = "", keep_session_id: str | None = None
) -> int:
    """Revokes every currently-active (non-revoked) refresh token
    belonging to target_user. Returns the number of sessions actually
    revoked so the caller (API layer) can report something meaningful
    back ("Signed out 3 active sessions" vs. "No active sessions").

    `keep_session_id`, when given, excludes that one session row from
    the revocation -- used by the self-service password-change flow
    (app.api.auth.change_password) so changing your own password
    doesn't also sign you out of the tab you just used to change it,
    while every *other* session still gets invalidated.
    """
    query = db.query(RefreshToken).filter(RefreshToken.user_id == target_user.id, RefreshToken.revoked.is_(False))
    if keep_session_id:
        query = query.filter(RefreshToken.id != keep_session_id)
    records = query.all()
    count = len(records)
    for record in records:
        record.revoked = True
    if count:
        db.commit()

    detail = f"{target_user.email}: {count} active session(s) revoked"
    if reason:
        detail += f" ({reason})"
    audit_service.record_event(
        db, actor=actor_email, action="Sessions Force-Revoked", result="Success", detail=detail,
    )
    if count:
        notification_service.notify(
            event="Sessions Force-Revoked",
            message=f"{actor_email} force-revoked {count} active session(s) for {target_user.email}"
            + (f" ({reason})" if reason else ""),
            severity="warning",
        )
    return count
