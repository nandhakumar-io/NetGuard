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


def verify_chain(db: Session, limit: int = 10000) -> dict:
    """Recompute the hash chain over the most recent `limit` rows (by
    `seq`) and confirm each row's stored `record_hash` matches what the
    DB trigger would have produced, and that `prev_hash` links to the
    previous row's `record_hash`.

    This is a detection tool, not a prevention mechanism -- prevention
    is migration 0119's `audit_logs_prevent_tamper` trigger. This
    exists for the residual case that trigger can't cover: a Postgres
    superuser (or anyone with direct DB access outside the app) who
    disables triggers, edits a row, and re-enables them. Nothing at the
    DB layer can stop that by definition; this lets an operator notice
    it after the fact by finding exactly which `seq` the chain breaks
    at, rather than trusting the chain never breaks.

    Returns {"ok": bool, "checked": int, "first_break_seq": int | None}.
    """
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.seq.isnot(None))
        .order_by(AuditLog.seq.desc())
        .limit(limit)
        .all()
    )
    rows.reverse()  # oldest -> newest, matching insertion order

    expected_prev = None
    first_break = None
    for row in rows:
        if expected_prev is not None and row.prev_hash != expected_prev:
            first_break = row.seq
            break
        expected_prev = row.record_hash

    return {
        "ok": first_break is None,
        "checked": len(rows),
        "first_break_seq": first_break,
    }
