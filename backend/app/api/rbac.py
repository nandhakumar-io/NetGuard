"""RBAC audit dashboard: who has access to what, tied to AuditLog.

Two views:
  GET /rbac/matrix  — the static permission matrix: which protected
      resources exist and which roles (app.core.deps.require_roles) can
      reach them. This is inherently a snapshot of the API's own
      `require_roles(...)` guards -- there's no dynamic policy store in
      this app, roles are hard-coded per-endpoint -- so the matrix is
      maintained by hand here rather than introspected at runtime.
  GET /rbac/users   — every user, their role (which the matrix above
      resolves into effective permissions), MFA status, and their most
      recent AuditLog activity, so an admin/auditor can see not just who
      *can* do something but who actually *has*.

Restricted to NETWORK_ADMIN and AUDITOR -- the two roles with a
legitimate need to see the fleet's full access list, mirroring how
`require_roles` is used to gate every other admin-surface endpoint.
"""
import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_roles
from app.models.audit_log import AuditLog
from app.models.user import User, UserRole

router = APIRouter(prefix="/rbac", tags=["rbac"])

_allowed = require_roles(UserRole.NETWORK_ADMIN, UserRole.AUDITOR)

# Mirrors the `Depends(require_roles(...))` guards actually present across
# app/api/*.py. Update this alongside any endpoint that adds/changes a
# require_roles(...) guard -- see the module docstring for why this can't
# just be introspected at runtime.
PERMISSION_MATRIX = [
    {"resource": "Devices — create/delete", "endpoint": "POST/DELETE /devices", "roles": ["network_admin"]},
    {"resource": "Device Groups — manage", "endpoint": "/device-groups (write)", "roles": ["network_admin"]},
    {"resource": "Config Management — golden configs, restore", "endpoint": "/config (write)", "roles": ["network_admin"]},
    {"resource": "Config Templates — manage", "endpoint": "/config-templates (write)", "roles": ["network_admin"]},
    {"resource": "Compliance Baselines — manage", "endpoint": "/compliance-baselines (write)", "roles": ["network_admin"]},
    {"resource": "Drift — manage baselines", "endpoint": "/drift (write)", "roles": ["network_admin"]},
    {"resource": "Firmware Upgrades — schedule/run", "endpoint": "/firmware-upgrades (write)", "roles": ["network_admin"]},
    {"resource": "Metrics — polling config", "endpoint": "/metrics (write)", "roles": ["network_admin"]},
    {"resource": "GNS3 Lab — provision", "endpoint": "/gns3 (write)", "roles": ["network_admin"]},
    {
        "resource": "JIT Access — approve/reject/revoke",
        "endpoint": "/jit-access/{id}/approve|reject|revoke, /jit-access/pending",
        "roles": ["network_admin"],
    },
    {
        "resource": "JIT Access — request/view own",
        "endpoint": "/jit-access/request, /jit-access/mine",
        "roles": ["network_admin", "network_engineer", "noc_engineer", "security", "auditor"],
    },
    {
        "resource": "Maintenance Windows — create/manage",
        "endpoint": "/maintenance-windows (write)",
        "roles": ["network_admin", "network_engineer", "noc_engineer"],
    },
    {
        "resource": "All other reads (dashboard, alerts, deployments, reports, ...)",
        "endpoint": "most GET endpoints",
        "roles": ["network_admin", "network_engineer", "noc_engineer", "security", "auditor"],
    },
]


@router.get("/matrix")
def get_permission_matrix(_: User = Depends(_allowed)):
    return {"roles": [r.value for r in UserRole], "matrix": PERMISSION_MATRIX}


@router.get("/users")
def rbac_user_dashboard(db: Session = Depends(get_db), _: User = Depends(_allowed)):
    """Every user with their role, MFA status, and most recent AuditLog
    activity (matched on AuditLog.actor == User.email -- the same field
    every audit_service.record_event() call and the /audit-logs feed
    already key on)."""
    users = db.query(User).order_by(User.email).all()

    # Most recent audit_log row per actor, in one query rather than N.
    latest_subq = (
        db.query(AuditLog.actor, func.max(AuditLog.created_at).label("latest_at"))
        .group_by(AuditLog.actor)
        .subquery()
    )
    latest_rows = (
        db.query(AuditLog)
        .join(
            latest_subq,
            (AuditLog.actor == latest_subq.c.actor) & (AuditLog.created_at == latest_subq.c.latest_at),
        )
        .all()
    )
    latest_by_actor = {row.actor: row for row in latest_rows}

    activity_counts = dict(
        db.query(AuditLog.actor, func.count(AuditLog.id)).group_by(AuditLog.actor).all()
    )

    result = []
    for user in users:
        role_value = user.role.value if hasattr(user.role, "value") else user.role
        last = latest_by_actor.get(user.email)
        result.append({
            "id": str(user.id),
            "email": user.email,
            "full_name": user.full_name,
            "role": role_value,
            "is_active": str(user.is_active).lower() in ("true", "1"),
            "mfa_enabled": str(user.mfa_enabled).lower() == "true",
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "total_audit_events": activity_counts.get(user.email, 0),
            "last_action": last.action if last else None,
            "last_action_result": last.result if last else None,
            "last_action_at": last.created_at.isoformat() if last and last.created_at else None,
        })

    never_active_count = sum(1 for u in result if u["total_audit_events"] == 0)
    stale_cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=90)
    stale_accounts = [
        u["email"] for u in result
        if u["last_action_at"] and datetime.datetime.fromisoformat(u["last_action_at"]) < stale_cutoff
    ]

    return {
        "users": result,
        "total_users": len(result),
        "never_active_count": never_active_count,
        "stale_accounts_90d": stale_accounts,
    }
