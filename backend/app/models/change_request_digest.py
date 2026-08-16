"""Weekly Change Request Digest.

Same shape as app.services.compliance_report (build -> render -> email),
but summarizes change-request *activity and throughput* for the window
rather than fleet-wide compliance/drift standing -- the two reports
answer different questions ("is the fleet drifting" vs "how much change
happened and how fast did it move through approval") and are delivered on
independent schedules so either can be toggled off without affecting the
other.

Reuses ChangeRequest rows only (no new queries against drift/audit) since
everything needed -- status, priority, risk classification, timestamps,
submitter/approver -- already lives on the row.
"""
from __future__ import annotations

import csv
import datetime
import io
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.change_request import ChangeRequest, ChangeStatus
from app.models.device import Device
from app.models.user import User


@dataclass
class ChangeRequestDigestRow:
    hostname: str
    priority: str
    status: str
    risk_classification: str | None
    requires_dual_approval: bool
    submitted_by: str
    created_at: datetime.datetime
    approved_at: datetime.datetime | None
    time_to_approve_hours: float | None


@dataclass
class ChangeRequestDigest:
    generated_at: datetime.datetime
    window_days: int
    rows: list[ChangeRequestDigestRow] = field(default_factory=list)
    # Still-open pending-approval CRs regardless of when they were
    # submitted -- a CR opened 3 weeks ago and still stuck is exactly the
    # kind of thing a digest should surface even if it falls outside the
    # window used for the "what happened this week" counts above.
    still_pending: list[ChangeRequestDigestRow] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        return counts

    @property
    def critical_risk_count(self) -> int:
        return sum(1 for r in self.rows if r.risk_classification == "Critical Risk")

    @property
    def dual_approval_count(self) -> int:
        return sum(1 for r in self.rows if r.requires_dual_approval)

    @property
    def rolled_back_count(self) -> int:
        return sum(1 for r in self.rows if r.status == ChangeStatus.ROLLED_BACK.value)

    @property
    def median_time_to_approve_hours(self) -> float | None:
        vals = sorted(r.time_to_approve_hours for r in self.rows if r.time_to_approve_hours is not None)
        if not vals:
            return None
        mid = len(vals) // 2
        return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2

    @property
    def top_submitters(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for row in self.rows:
            counts[row.submitted_by] = counts.get(row.submitted_by, 0) + 1
        return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:5]


def _row_from(cr: ChangeRequest, device_by_id: dict, user_by_id: dict) -> ChangeRequestDigestRow:
    device = device_by_id.get(cr.device_id)
    submitter = user_by_id.get(cr.submitted_by)
    ttap_hours = None
    if cr.approved_at is not None:
        created_at = cr.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=datetime.timezone.utc)
        approved_at = cr.approved_at
        if approved_at.tzinfo is None:
            approved_at = approved_at.replace(tzinfo=datetime.timezone.utc)
        ttap_hours = max((approved_at - created_at).total_seconds() / 3600, 0.0)

    return ChangeRequestDigestRow(
        hostname=device.hostname if device else str(cr.device_id),
        priority=cr.priority.value if hasattr(cr.priority, "value") else str(cr.priority),
        status=cr.status.value if hasattr(cr.status, "value") else str(cr.status),
        risk_classification=cr.risk_classification,
        requires_dual_approval=cr.requires_dual_approval,
        submitted_by=submitter.email if submitter else str(cr.submitted_by),
        created_at=cr.created_at,
        approved_at=cr.approved_at,
        time_to_approve_hours=ttap_hours,
    )


def build_digest(db: Session, window_days: int = 7) -> ChangeRequestDigest:
    now = datetime.datetime.now(datetime.timezone.utc)
    window_start = now - datetime.timedelta(days=window_days)

    window_crs = db.query(ChangeRequest).filter(ChangeRequest.created_at >= window_start).all()
    pending_crs = db.query(ChangeRequest).filter(ChangeRequest.status == ChangeStatus.PENDING_APPROVAL).all()

    device_by_id = {d.id: d for d in db.query(Device).all()}
    user_by_id = {u.id: u for u in db.query(User).all()}

    rows = [_row_from(cr, device_by_id, user_by_id) for cr in window_crs]
    still_pending = [_row_from(cr, device_by_id, user_by_id) for cr in pending_crs]
    still_pending.sort(key=lambda r: r.created_at)

    return ChangeRequestDigest(generated_at=now, window_days=window_days, rows=rows, still_pending=still_pending)


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

_CSV_HEADER = [
    "hostname", "priority", "status", "risk_classification", "dual_approval",
    "submitted_by", "created_at", "approved_at", "time_to_approve_hours",
]


def render_csv(digest: ChangeRequestDigest) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([f"# NetGuard Change Request Digest -- generated {digest.generated_at.isoformat()}"])
    writer.writerow([f"# Window: last {digest.window_days} day(s)"])
    writer.writerow([f"# Total: {digest.total}", f"Critical Risk: {digest.critical_risk_count}", f"Rolled back: {digest.rolled_back_count}"])
    writer.writerow([])
    writer.writerow(_CSV_HEADER)
    for row in digest.rows:
        writer.writerow(
            [
                row.hostname, row.priority, row.status, row.risk_classification or "",
                "yes" if row.requires_dual_approval else "no", row.submitted_by,
                row.created_at.isoformat(),
                row.approved_at.isoformat() if row.approved_at else "",
                f"{row.time_to_approve_hours:.1f}" if row.time_to_approve_hours is not None else "",
            ]
        )

    if digest.still_pending:
        writer.writerow([])
        writer.writerow([f"# Still pending approval ({len(digest.still_pending)}), oldest first"])
        writer.writerow(["hostname", "priority", "risk_classification", "submitted_by", "created_at"])
        for row in digest.still_pending:
            writer.writerow([row.hostname, row.priority, row.risk_classification or "", row.submitted_by, row.created_at.isoformat()])

    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# Scheduled delivery
# ---------------------------------------------------------------------------


def deliver_scheduled_digest(db: Session, window_days: int) -> bool:
    """Builds the digest and emails it to NOTIFY_EMAIL_RECIPIENTS via
    app.services.notification_service.send_email_attachment, same delivery
    mechanism as app.services.compliance_report.deliver_scheduled_report.
    Called by the weekly Celery beat task (see
    app.tasks.run_weekly_change_request_digest_task). CSV-only (unlike the
    compliance report there's no PDF variant here -- a tabular activity
    log doesn't need the fixed-layout treatment, and CSV keeps this
    dependency-free of reportlab).

    Returns True if the email was actually sent, False if it was skipped
    because SMTP isn't configured (still recorded in the audit trail).
    """
    from app.services import audit_service, notification_service

    digest = build_digest(db, window_days=window_days)
    timestamp = digest.generated_at.strftime("%Y%m%d")
    filename = f"netguard-change-request-digest-{timestamp}.csv"

    status_lines = "\n".join(f"  {status}: {count}" for status, count in sorted(digest.by_status.items()))
    top_submitters = "\n".join(f"  {email}: {count}" for email, count in digest.top_submitters) or "  (none)"
    median_ttap = (
        f"{digest.median_time_to_approve_hours:.1f}h" if digest.median_time_to_approve_hours is not None else "N/A"
    )

    subject = f"[NetGuard] Weekly Change Request Digest -- {digest.generated_at.strftime('%Y-%m-%d')}"
    body = (
        f"{digest.total} change request(s) submitted in the last {window_days} day(s).\n\n"
        f"By status:\n{status_lines}\n\n"
        f"Critical Risk: {digest.critical_risk_count}\n"
        f"Required dual approval: {digest.dual_approval_count}\n"
        f"Rolled back: {digest.rolled_back_count}\n"
        f"Median time to approve: {median_ttap}\n\n"
        f"Top submitters:\n{top_submitters}\n\n"
        f"Still pending approval (any age): {len(digest.still_pending)}\n\n"
        "Full detail is in the attached CSV."
    )

    sent = notification_service.send_email_attachment(subject, body, attachments=[(filename, render_csv(digest), "csv")])

    audit_service.record_event(
        db,
        actor="system:weekly-change-request-digest",
        action="Change Request Digest Delivery",
        result="Sent" if sent else "Skipped (SMTP not configured)",
        detail=(
            f"window_days={window_days} total={digest.total} critical_risk={digest.critical_risk_count} "
            f"still_pending={len(digest.still_pending)}"
        ),
    )
    return sent
