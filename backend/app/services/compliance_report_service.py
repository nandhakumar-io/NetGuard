"""Compliance Report generator.

Builds a fleet-wide compliance snapshot -- one row per device -- and
renders it as CSV or PDF. Reuses data already produced by other services
rather than recomputing anything:

  - app.services.drift_service / models.ConfigDrift for the latest
    compliance_score + severity per device (drift detection already scores
    this nightly + on-demand, see app.tasks.drift_detection_task)
  - ChangeRequest rows for change volume + Critical Risk counts in the
    reporting window (AI Configuration Analyzer output, SRS 6.2 / FR-6)
  - AuditLog for a plain-text activity trail appended after the table

Called by GET /reports/compliance?format=csv|pdf (see app.api.reports).
"""
from __future__ import annotations

import csv
import datetime
import io
from dataclasses import dataclass, field

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.change_request import ChangeRequest
from app.models.config_drift import ConfigDrift, DriftStatus
from app.models.device import Device


@dataclass
class DeviceComplianceRow:
    hostname: str
    site: str | None
    vendor: str
    compliance_score: int | None  # None = no drift scan on record yet
    drift_severity: str | None
    open_drift_count: int
    change_requests_in_window: int
    critical_risk_changes_in_window: int
    last_change_at: datetime.datetime | None


@dataclass
class ComplianceReport:
    generated_at: datetime.datetime
    window_days: int
    rows: list[DeviceComplianceRow] = field(default_factory=list)

    @property
    def fleet_average_compliance(self) -> int:
        scored = [r.compliance_score for r in self.rows if r.compliance_score is not None]
        return round(sum(scored) / len(scored)) if scored else 100

    @property
    def total_open_drifts(self) -> int:
        return sum(r.open_drift_count for r in self.rows)

    @property
    def total_critical_risk_changes(self) -> int:
        return sum(r.critical_risk_changes_in_window for r in self.rows)


def build_report(db: Session, window_days: int = 30) -> ComplianceReport:
    now = datetime.datetime.now(datetime.timezone.utc)
    window_start = now - datetime.timedelta(days=window_days)

    devices = db.query(Device).order_by(Device.hostname).all()
    rows: list[DeviceComplianceRow] = []

    for device in devices:
        latest_drift = (
            db.query(ConfigDrift)
            .filter(ConfigDrift.device_id == device.id)
            .order_by(ConfigDrift.detected_at.desc())
            .first()
        )
        open_drift_count = (
            db.query(func.count(ConfigDrift.id))
            .filter(ConfigDrift.device_id == device.id, ConfigDrift.status == DriftStatus.OPEN)
            .scalar()
            or 0
        )

        window_crs = (
            db.query(ChangeRequest)
            .filter(ChangeRequest.device_id == device.id, ChangeRequest.created_at >= window_start)
            .all()
        )
        critical_count = sum(1 for cr in window_crs if cr.risk_classification == "Critical Risk")
        last_change_at = max((cr.created_at for cr in window_crs), default=None)

        rows.append(
            DeviceComplianceRow(
                hostname=device.hostname,
                site=device.site,
                vendor=device.vendor.value if hasattr(device.vendor, "value") else str(device.vendor),
                compliance_score=latest_drift.compliance_score if latest_drift else None,
                drift_severity=latest_drift.severity.value if latest_drift else None,
                open_drift_count=open_drift_count,
                change_requests_in_window=len(window_crs),
                critical_risk_changes_in_window=critical_count,
                last_change_at=last_change_at,
            )
        )

    return ComplianceReport(generated_at=now, window_days=window_days, rows=rows)


def recent_audit_entries(db: Session, window_days: int, limit: int = 50) -> list[AuditLog]:
    window_start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=window_days)
    return (
        db.query(AuditLog)
        .filter(AuditLog.created_at >= window_start)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
        .all()
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

_CSV_HEADER = [
    "hostname",
    "site",
    "vendor",
    "compliance_score",
    "drift_severity",
    "open_drift_count",
    "change_requests_in_window",
    "critical_risk_changes_in_window",
    "last_change_at",
]


def render_csv(report: ComplianceReport) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([f"# NetGuard Compliance Report -- generated {report.generated_at.isoformat()}"])
    writer.writerow([f"# Window: last {report.window_days} day(s)"])
    writer.writerow(
        [f"# Fleet average compliance: {report.fleet_average_compliance}/100", f"open drifts: {report.total_open_drifts}"]
    )
    writer.writerow([])
    writer.writerow(_CSV_HEADER)
    for row in report.rows:
        writer.writerow(
            [
                row.hostname,
                row.site or "",
                row.vendor,
                row.compliance_score if row.compliance_score is not None else "N/A",
                row.drift_severity or "N/A",
                row.open_drift_count,
                row.change_requests_in_window,
                row.critical_risk_changes_in_window,
                row.last_change_at.isoformat() if row.last_change_at else "",
            ]
        )
    return buf.getvalue().encode("utf-8")


def render_pdf(report: ComplianceReport) -> bytes:
    # Imported lazily so environments that never request a PDF report don't
    # need reportlab installed to import this module at all.
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(letter),
        leftMargin=0.5 * inch, rightMargin=0.5 * inch, topMargin=0.5 * inch, bottomMargin=0.5 * inch,
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph("NetGuard Compliance Report", styles["Title"]),
        Paragraph(
            f"Generated {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')} &mdash; "
            f"window: last {report.window_days} day(s)",
            styles["Normal"],
        ),
        Paragraph(
            f"Fleet average compliance: <b>{report.fleet_average_compliance}/100</b> &nbsp;|&nbsp; "
            f"Open drifts: <b>{report.total_open_drifts}</b> &nbsp;|&nbsp; "
            f"Critical Risk changes in window: <b>{report.total_critical_risk_changes}</b>",
            styles["Normal"],
        ),
        Spacer(1, 0.25 * inch),
    ]

    header = ["Hostname", "Site", "Vendor", "Compliance", "Drift Sev.", "Open Drifts", "CRs", "Critical CRs", "Last Change"]
    data = [header]
    for row in report.rows:
        data.append(
            [
                row.hostname,
                row.site or "-",
                row.vendor,
                str(row.compliance_score) if row.compliance_score is not None else "N/A",
                (row.drift_severity or "N/A").upper(),
                str(row.open_drift_count),
                str(row.change_requests_in_window),
                str(row.critical_risk_changes_in_window),
                row.last_change_at.strftime("%Y-%m-%d") if row.last_change_at else "-",
            ]
        )

    table = Table(data, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )

    # Flag low-compliance / critical rows in red for at-a-glance scanning.
    for i, row in enumerate(report.rows, start=1):
        if (row.compliance_score is not None and row.compliance_score < 60) or row.drift_severity == "critical":
            table.setStyle(TableStyle([("TEXTCOLOR", (3, i), (4, i), colors.HexColor("#b91c1c"))]))

    story.append(table)
    doc.build(story)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Scheduled delivery
# ---------------------------------------------------------------------------


def deliver_scheduled_report(db: Session, window_days: int, period_label: str) -> bool:
    """Builds the compliance report and emails it to NOTIFY_EMAIL_RECIPIENTS
    via app.services.notification_service.send_email_attachment, using the
    same SMTP config as every other notification. Renders as PDF; falls
    back to CSV if reportlab isn't installed on this server rather than
    failing the scheduled run outright.

    Called by the weekly/monthly Celery beat tasks (see
    app.tasks.run_weekly_compliance_report_task /
    run_monthly_compliance_report_task) so the report lands in inboxes on
    its own schedule instead of only being available on-demand via
    GET /reports/compliance.

    Returns True if the email was actually sent, False if it was skipped
    because SMTP isn't configured (recorded in the audit trail either way,
    so a silently-unconfigured SMTP setup is still visible there).
    """
    from app.services import audit_service, notification_service

    report = build_report(db, window_days=window_days)
    timestamp = report.generated_at.strftime("%Y%m%d")
    period_slug = period_label.lower()

    try:
        report_bytes = render_pdf(report)
        filename = f"netguard-compliance-report-{period_slug}-{timestamp}.pdf"
        subtype = "pdf"
    except ImportError:
        report_bytes = render_csv(report)
        filename = f"netguard-compliance-report-{period_slug}-{timestamp}.csv"
        subtype = "csv"

    subject = f"[NetGuard] {period_label} Compliance Report -- {report.generated_at.strftime('%Y-%m-%d')}"
    body = (
        f"Attached is the {period_slug} NetGuard compliance report covering the "
        f"last {window_days} day(s).\n\n"
        f"Fleet average compliance: {report.fleet_average_compliance}/100\n"
        f"Open drifts: {report.total_open_drifts}\n"
        f"Critical Risk changes in window: {report.total_critical_risk_changes}\n\n"
        "Full detail (per-device breakdown, audit trail) is in the attached report."
    )

    sent = notification_service.send_email_attachment(subject, body, attachments=[(filename, report_bytes, subtype)])

    audit_service.record_event(
        db,
        actor=f"system:{period_slug}-compliance-report",
        action="Compliance Report Delivery",
        result="Sent" if sent else "Skipped (SMTP not configured)",
        detail=(
            f"window_days={window_days} fleet_avg_compliance={report.fleet_average_compliance} "
            f"open_drifts={report.total_open_drifts}"
        ),
    )
    return sent
