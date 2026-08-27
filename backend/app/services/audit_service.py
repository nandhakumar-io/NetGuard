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
    tenant_id=None,
) -> AuditLog:
    """Append an immutable audit log entry. Never update or delete rows from
    this table at the application layer.

    tenant_id: the tenant this event belongs to, or None for a
    global/system event (login, MSP-staff action). Optional for now so
    existing call sites keep working, but every call site that has a
    device or current_user in scope should pass it -- see
    app.models.audit_log.AuditLog.tenant_id and app.api.audit, which
    filters reads by it. A row written with tenant_id=None is visible
    fleet-wide, same "NULL = global" convention as AlertRule/WebhookEndpoint.
    """
    entry = AuditLog(
        actor=actor,
        action=action,
        result=result,
        device_hostname=device_hostname,
        change_request_id=change_request_id,
        detail=detail,
        tenant_id=tenant_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry
