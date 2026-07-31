from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog


def record_event(
    db: Session,
    actor: str,
    action: str,
    result: str,
    device_hostname: str | None = None,
    change_request_id=None,
    detail: str | None = None,
) -> AuditLog:
    """Append an immutable audit log entry. Never update or delete rows from
    this table at the application layer.
    """
    entry = AuditLog(
        actor=actor,
        action=action,
        result=result,
        device_hostname=device_hostname,
        change_request_id=change_request_id,
        detail=detail,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry
