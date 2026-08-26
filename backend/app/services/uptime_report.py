"""Uptime & Incident Report generator.

Built for the recurring "MSPs get asked for this constantly" request: a
per-tenant (or per-device-group, or fleet-wide for MSP staff) rollup of
device availability and incident activity over a trailing window,
downloadable on demand and deliverable on a weekly/monthly schedule --
same shape as app.services.compliance_report, which this deliberately
mirrors rather than introduces a second reporting pattern:

  - Availability is computed from DeviceStatusHistory using the exact
    same time-weighted online/degraded-vs-offline/unknown algorithm as
    app.services.metrics_service.fleet_availability_summary, just
    per-report-scope instead of always fleet-wide, and additionally
    tracking each device's outage_count and total downtime for the
    window (fleet_availability_summary only ever needed the percentage).
  - Incident stats come from app.models.incident.Incident, scoped down
    to incidents whose root-cause alert belongs to a device in scope.
    MTTA/MTTR are computed from the timestamps already on Incident
    (detected_at -> mitigated_at for MTTA, detected_at -> resolved_at
    for MTTR) rather than adding new tracking.

Called by GET /reports/uptime-incident?format=csv|pdf (see
app.api.reports) and the weekly/monthly Celery beat tasks (see
app.tasks.run_weekly_uptime_report_task /
run_monthly_uptime_report_task).
"""
from __future__ import annotations

import csv
import datetime
import io
import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.device import Device, DeviceStatus
from app.models.device_status_history import DeviceStatusHistory
from app.models.incident import Incident
from app.models.tenant import Tenant
from app.services.metrics_service import _as_aware_utc, _nines

_AVAILABLE_STATUSES = (DeviceStatus.ONLINE, DeviceStatus.DEGRADED)


@dataclass
class DeviceUptimeRow:
    hostname: str
    site: str | None
    availability_pct: float
    downtime_hours: float
    outage_count: int  # transitions INTO an unavailable state within the window


@dataclass
class IncidentRow:
    title: str
    severity: str
    status: str
    detected_at: datetime.datetime | None
    resolved_at: datetime.datetime | None
    duration_hours: float | None  # None if not yet resolved


@dataclass
class UptimeIncidentReport:
    generated_at: datetime.datetime
    window_days: int
    scope_label: str  # tenant name, device group name, or "All Tenants (fleet-wide)"

    device_rows: list[DeviceUptimeRow] = field(default_factory=list)
    incident_rows: list[IncidentRow] = field(default_factory=list)

    @property
    def fleet_availability_pct(self) -> float | None:
        if not self.device_rows:
            return None
        return round(sum(r.availability_pct for r in self.device_rows) / len(self.device_rows), 3)

    @property
    def total_downtime_hours(self) -> float:
        return round(sum(r.downtime_hours for r in self.device_rows), 2)

    @property
    def total_outages(self) -> int:
        return sum(r.outage_count for r in self.device_rows)

    @property
    def incidents_by_severity(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.incident_rows:
            counts[row.severity] = counts.get(row.severity, 0) + 1
        return counts

    @property
    def open_incident_count(self) -> int:
        return sum(1 for r in self.incident_rows if r.status not in ("resolved", "closed"))

    @property
    def mttr_hours(self) -> float | None:
        durations = [r.duration_hours for r in self.incident_rows if r.duration_hours is not None]
        return round(sum(durations) / len(durations), 2) if durations else None


def _scoped_devices(db: Session, tenant_id: uuid.UUID | None, device_group_id: uuid.UUID | None) -> list[Device]:
    q = db.query(Device)
    if tenant_id is not None:
        q = q.filter(Device.tenant_id == tenant_id)
    if device_group_id is not None:
        q = q.filter(Device.group_id == device_group_id)
    return q.order_by(Device.hostname).all()


def _device_uptime_row(db: Session, device: Device, since: datetime.datetime, now: datetime.datetime) -> DeviceUptimeRow | None:
    """Same time-weighted walk as metrics_service.fleet_availability_summary,
    but additionally counts outages (transitions landing on an
    unavailable status) instead of only the resulting percentage --
    a report reader wants "flapped 6 times for 40 minutes total", not
    just "99.3% available", to tell a single long outage apart from a
    flapping link.
    """
    history = (
        db.query(DeviceStatusHistory)
        .filter(DeviceStatusHistory.device_id == device.id, DeviceStatusHistory.changed_at >= since)
        .order_by(DeviceStatusHistory.changed_at.asc())
        .all()
    )
    prior = (
        db.query(DeviceStatusHistory)
        .filter(DeviceStatusHistory.device_id == device.id, DeviceStatusHistory.changed_at < since)
        .order_by(DeviceStatusHistory.changed_at.desc())
        .first()
    )
    if not history and prior is None:
        # Never observed changing status in this device's lifetime --
        # nothing to roll up (e.g. added but not yet polled).
        return None

    status_at_window_start = prior.status if prior is not None else (history[0].previous_status or device.status)
    window_seconds = (now - since).total_seconds()
    available_seconds = 0.0
    outage_count = 0
    cursor = since
    current_status = status_at_window_start
    for row in history:
        changed_at = _as_aware_utc(row.changed_at)
        segment = (changed_at - cursor).total_seconds()
        if current_status in _AVAILABLE_STATUSES:
            available_seconds += max(segment, 0.0)
        if current_status in _AVAILABLE_STATUSES and row.status not in _AVAILABLE_STATUSES:
            outage_count += 1
        cursor = changed_at
        current_status = row.status
    tail_segment = (now - cursor).total_seconds()
    if current_status in _AVAILABLE_STATUSES:
        available_seconds += max(tail_segment, 0.0)

    pct = (available_seconds / window_seconds * 100) if window_seconds > 0 else 100.0
    downtime_hours = max(window_seconds - available_seconds, 0.0) / 3600

    return DeviceUptimeRow(
        hostname=device.hostname,
        site=device.site,
        availability_pct=round(pct, 3),
        downtime_hours=round(downtime_hours, 2),
        outage_count=outage_count,
    )


def build_report(
    db: Session,
    window_days: int = 30,
    tenant_id: uuid.UUID | None = None,
    device_group_id: uuid.UUID | None = None,
) -> UptimeIncidentReport:
    now = datetime.datetime.now(datetime.timezone.utc)
    since = now - datetime.timedelta(days=window_days)

    devices = _scoped_devices(db, tenant_id, device_group_id)
    device_ids = {d.id for d in devices}

    device_rows: list[DeviceUptimeRow] = []
    for device in devices:
        row = _device_uptime_row(db, device, since, now)
        if row is not None:
            device_rows.append(row)

    incident_rows: list[IncidentRow] = []
    if device_ids:
        # An Incident doesn't carry device_id directly (see the model's
        # own docstring) -- it's derived from its root-cause Alert, which
        # does. Joining through that alert is enough to scope incidents
        # to this report's device set without adding a denormalized
        # column that could drift from the real source of truth.
        from app.models.alert import Alert

        incidents = (
            db.query(Incident)
            .join(Alert, Alert.id == Incident.root_cause_alert_id)
            .filter(Alert.device_id.in_(device_ids), Incident.created_at >= since)
            .order_by(Incident.created_at.desc())
            .all()
        )
        for incident in incidents:
            duration_hours = None
            if incident.detected_at and incident.resolved_at:
                delta = _as_aware_utc(incident.resolved_at) - _as_aware_utc(incident.detected_at)
                duration_hours = round(delta.total_seconds() / 3600, 2)
            incident_rows.append(
                IncidentRow(
                    title=incident.title,
                    severity=incident.severity.value if hasattr(incident.severity, "value") else str(incident.severity),
                    status=incident.status.value if hasattr(incident.status, "value") else str(incident.status),
                    detected_at=incident.detected_at,
                    resolved_at=incident.resolved_at,
                    duration_hours=duration_hours,
                )
            )

    if tenant_id is not None:
        tenant = db.get(Tenant, tenant_id)
        scope_label = tenant.name if tenant else "Unknown Tenant"
    elif device_group_id is not None:
        from app.models.device_group import DeviceGroup

        group = db.get(DeviceGroup, device_group_id)
        scope_label = f"Device Group: {group.name}" if group else "Unknown Device Group"
    else:
        scope_label = "All Tenants (fleet-wide)"

    return UptimeIncidentReport(
        generated_at=now, window_days=window_days, scope_label=scope_label,
        device_rows=device_rows, incident_rows=incident_rows,
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

_CSV_UPTIME_HEADER = ["hostname", "site", "availability_pct", "downtime_hours", "outage_count"]
_CSV_INCIDENT_HEADER = ["title", "severity", "status", "detected_at", "resolved_at", "duration_hours"]


def render_csv(report: UptimeIncidentReport) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([f"# NetGuard Uptime & Incident Report -- {report.scope_label}"])
    writer.writerow([f"# Generated {report.generated_at.isoformat()} -- window: last {report.window_days} day(s)"])
    fleet_pct = report.fleet_availability_pct
    writer.writerow([
        f"# Fleet availability: {_nines(fleet_pct) if fleet_pct is not None else 'n/a'}",
        f"total downtime (hrs): {report.total_downtime_hours}",
        f"total outages: {report.total_outages}",
        f"open incidents: {report.open_incident_count}",
        f"MTTR (hrs): {report.mttr_hours if report.mttr_hours is not None else 'n/a'}",
    ])
    writer.writerow([])

    writer.writerow(["## Device Availability"])
    writer.writerow(_CSV_UPTIME_HEADER)
    for row in report.device_rows:
        writer.writerow([row.hostname, row.site or "", row.availability_pct, row.downtime_hours, row.outage_count])

    writer.writerow([])
    writer.writerow(["## Incidents"])
    writer.writerow(_CSV_INCIDENT_HEADER)
    for row in report.incident_rows:
        writer.writerow([
            row.title, row.severity, row.status,
            row.detected_at.isoformat() if row.detected_at else "",
            row.resolved_at.isoformat() if row.resolved_at else "",
            row.duration_hours if row.duration_hours is not None else "",
        ])

    return buf.getvalue().encode("utf-8")


def render_pdf(report: UptimeIncidentReport) -> bytes:
    # Imported lazily -- see compliance_report.render_pdf for why.
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
    fleet_pct = report.fleet_availability_pct
    story = [
        Paragraph(f"NetGuard Uptime &amp; Incident Report &mdash; {report.scope_label}", styles["Title"]),
        Paragraph(
            f"Generated {report.generated_at.strftime('%Y-%m-%d %H:%M UTC')} &mdash; "
            f"window: last {report.window_days} day(s)",
            styles["Normal"],
        ),
        Paragraph(
            f"Fleet availability: <b>{_nines(fleet_pct) if fleet_pct is not None else 'n/a'}</b> &nbsp;|&nbsp; "
            f"Total downtime: <b>{report.total_downtime_hours} hrs</b> &nbsp;|&nbsp; "
            f"Total outages: <b>{report.total_outages}</b> &nbsp;|&nbsp; "
            f"Open incidents: <b>{report.open_incident_count}</b> &nbsp;|&nbsp; "
            f"MTTR: <b>{report.mttr_hours if report.mttr_hours is not None else 'n/a'} hrs</b>",
            styles["Normal"],
        ),
        Spacer(1, 0.2 * inch),
        Paragraph("Device Availability", styles["Heading2"]),
    ]

    uptime_header = ["Hostname", "Site", "Availability", "Downtime (hrs)", "Outages"]
    uptime_data = [uptime_header]
    for row in report.device_rows:
        uptime_data.append([row.hostname, row.site or "-", f"{row.availability_pct}%", str(row.downtime_hours), str(row.outage_count)])
    uptime_table = Table(uptime_data, repeatRows=1)
    uptime_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    for i, row in enumerate(report.device_rows, start=1):
        if row.availability_pct < 99.0:
            uptime_table.setStyle(TableStyle([("TEXTCOLOR", (2, i), (2, i), colors.HexColor("#b91c1c"))]))
    story.append(uptime_table)

    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph("Incidents", styles["Heading2"]))
    incident_header = ["Title", "Severity", "Status", "Detected", "Resolved", "Duration (hrs)"]
    incident_data = [incident_header]
    for row in report.incident_rows:
        incident_data.append([
            row.title, row.severity.upper(), row.status.upper(),
            row.detected_at.strftime("%Y-%m-%d %H:%M") if row.detected_at else "-",
            row.resolved_at.strftime("%Y-%m-%d %H:%M") if row.resolved_at else "-",
            str(row.duration_hours) if row.duration_hours is not None else "open",
        ])
    if len(incident_data) == 1:
        incident_data.append(["No incidents in this window", "", "", "", "", ""])
    incident_table = Table(incident_data, repeatRows=1)
    incident_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(incident_table)

    doc.build(story)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Scheduled delivery
# ---------------------------------------------------------------------------


def deliver_scheduled_report(
    db: Session,
    window_days: int,
    period_label: str,
    tenant_id: uuid.UUID | None = None,
) -> bool:
    """Same delivery shape as compliance_report.deliver_scheduled_report:
    renders (PDF, falling back to CSV if reportlab isn't installed),
    emails via notification_service using the existing SMTP config, and
    always leaves an audit trail entry regardless of whether the send
    actually went out.
    """
    from app.services import audit_service, notification_service

    report = build_report(db, window_days=window_days, tenant_id=tenant_id)
    timestamp = report.generated_at.strftime("%Y%m%d")
    period_slug = period_label.lower()
    scope_slug = report.scope_label.lower().replace(" ", "-").replace("(", "").replace(")", "")

    try:
        report_bytes = render_pdf(report)
        filename = f"netguard-uptime-report-{scope_slug}-{period_slug}-{timestamp}.pdf"
        subtype = "pdf"
    except ImportError:
        report_bytes = render_csv(report)
        filename = f"netguard-uptime-report-{scope_slug}-{period_slug}-{timestamp}.csv"
        subtype = "csv"

    fleet_pct = report.fleet_availability_pct
    subject = f"[NetGuard] {period_label} Uptime & Incident Report -- {report.scope_label} -- {report.generated_at.strftime('%Y-%m-%d')}"
    body = (
        f"Attached is the {period_slug} NetGuard uptime & incident report for {report.scope_label}, "
        f"covering the last {window_days} day(s).\n\n"
        f"Fleet availability: {_nines(fleet_pct) if fleet_pct is not None else 'n/a'}\n"
        f"Total downtime: {report.total_downtime_hours} hrs across {report.total_outages} outage(s)\n"
        f"Open incidents: {report.open_incident_count}\n"
        f"MTTR: {report.mttr_hours if report.mttr_hours is not None else 'n/a'} hrs\n\n"
        "Full detail (per-device breakdown, incident list) is in the attached report."
    )

    sent = notification_service.send_email_attachment(subject, body, attachments=[(filename, report_bytes, subtype)])

    audit_service.record_event(
        db,
        actor=f"system:{period_slug}-uptime-report",
        action="Uptime & Incident Report Delivery",
        result="Sent" if sent else "Skipped (SMTP not configured)",
        detail=(
            f"scope={report.scope_label!r} window_days={window_days} "
            f"fleet_availability_pct={fleet_pct} open_incidents={report.open_incident_count}"
        ),
    )
    return sent
