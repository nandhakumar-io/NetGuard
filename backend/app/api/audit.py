from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.audit_log import AuditLog
from app.models.user import User

router = APIRouter(prefix="/audit-logs", tags=["audit"])


@router.get("")
def list_audit_logs(
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    # Was reachable with no auth at all and an uncapped `limit` -- every
    # user's email (actor), every action taken, and every device hostname
    # touched, for the whole audit history, to anyone who could reach the
    # API. Matches the RBAC matrix's "all other reads" bucket
    # (app/api/rbac.py): any authenticated role, not just admin/auditor --
    # the matrix already treats audit visibility as a normal read, not a
    # privileged one, this just makes the endpoint actually enforce that
    # instead of enforcing nothing.
    logs = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit).all()
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
