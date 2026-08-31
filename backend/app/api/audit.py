import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, get_tenant_scope
from app.models.audit_log import AuditLog
from app.models.user import User
from app.services import audit_service

router = APIRouter(prefix="/audit-logs", tags=["audit"])


@router.get("")
def list_audit_logs(
    limit: int = Query(100, ge=1, le=500),
    tenant_id: uuid.UUID | None = Query(
        None, description="MSP staff only: filter to a single tenant's audit history."
    ),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    scope=Depends(get_tenant_scope),
):
    # Was reachable with no auth at all and an uncapped `limit` -- every
    # user's email (actor), every action taken, and every device hostname
    # touched, for the whole audit history, to anyone who could reach the
    # API. Matches the RBAC matrix's "all other reads" bucket
    # (app/api/rbac.py): any authenticated role, not just admin/auditor --
    # the matrix already treats audit visibility as a normal read, not a
    # privileged one, this just makes the endpoint actually enforce that
    # instead of enforcing nothing.
    #
    # Tenant scoping (0097_audit_log_tenant_and_rule_inheritance): before
    # this, AuditLog had no tenant_id at all, so a non-MSP user of any
    # tenant could read every other tenant's audit history through this
    # same endpoint -- same class of gap the auth check above closed, just
    # for tenant boundaries instead of anonymous access. A regular user is
    # always pinned to their own tenant (scope is not None); the optional
    # `tenant_id` query param lets MSP staff (scope is None) drill into one
    # tenant's history from the cross-tenant NOC board without exposing
    # that filter to anyone it wouldn't apply to.
    q = db.query(AuditLog)
    if scope is not None:
        q = q.filter(AuditLog.tenant_id == scope)
    elif tenant_id is not None:
        q = q.filter(AuditLog.tenant_id == tenant_id)

    logs = q.order_by(AuditLog.created_at.desc()).limit(limit).all()
    return [
        {
            "id": str(log.id),
            "time": log.created_at,
            "user": log.actor,
            "action": log.action,
            "device": log.device_hostname,
            "result": log.result,
        }
        for log in logs
    ]


@router.get("/integrity")
def check_audit_integrity(
    limit: int = Query(10000, ge=1, le=100000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Recomputes the audit-log hash chain (migration 0119) and reports
    whether it's intact. Restricted to Security/Auditor/Network Admin --
    unlike GET /audit-logs itself (a normal read per the RBAC matrix),
    an integrity check reveals whether tampering has occurred, which is
    a more sensitive signal than the log contents themselves.
    """
    from app.models.user import UserRole

    if current_user.role not in (UserRole.SECURITY, UserRole.AUDITOR, UserRole.NETWORK_ADMIN):
        raise HTTPException(status_code=403, detail="Requires Security, Auditor, or Network Admin role")

    return audit_service.verify_chain(db, limit=limit)
