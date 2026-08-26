"""Cross-tenant NOC board -- a single-pane view across every managed
tenant for MSP staff who watch many customers at once (WallBoard.tsx,
by contrast, is a single tenant's own view).

Gated on User.is_msp_staff via app.core.deps.require_msp_staff, not on
role -- see that dependency's docstring.
"""
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_msp_staff
from app.models.alert import Alert, AlertSeverity
from app.models.device import Device, DeviceStatus
from app.models.incident import Incident, IncidentStatus
from app.models.tenant import Tenant
from app.models.user import User

router = APIRouter(prefix="/tenant-board", tags=["tenant-board"])


class TenantBoardRow(BaseModel):
    tenant_id: uuid.UUID
    tenant_name: str
    tenant_slug: str

    device_count: int
    devices_offline: int
    devices_degraded: int

    open_alerts_critical: int
    open_alerts_warning: int
    open_alerts_info: int

    open_incidents: int
    latest_critical_alert_message: str | None
    latest_critical_alert_at: str | None

    # Simple 0-100 health rollup so the board can sort/color tenants at a
    # glance without the viewer doing the arithmetic themselves: starts
    # at 100 and is docked per open problem, floored at 0. Weighted
    # toward what actually pages someone (critical alerts, open
    # incidents, offline devices) over cosmetic warning noise.
    health_score: int


class TenantBoardResponse(BaseModel):
    tenants: list[TenantBoardRow]


def _health_score(*, offline: int, critical_alerts: int, warning_alerts: int, incidents: int) -> int:
    score = 100
    score -= min(50, offline * 10)
    score -= min(30, critical_alerts * 8)
    score -= min(15, warning_alerts * 2)
    score -= min(20, incidents * 15)
    return max(0, score)


@router.get("/summary", response_model=TenantBoardResponse)
def get_tenant_board(
    db: Session = Depends(get_db),
    _: User = Depends(require_msp_staff),
) -> TenantBoardResponse:
    tenants = db.query(Tenant).filter(Tenant.is_active.is_(True)).order_by(Tenant.name).all()

    device_counts = dict(
        db.query(Device.tenant_id, func.count(Device.id))
        .group_by(Device.tenant_id)
        .all()
    )
    offline_counts = dict(
        db.query(Device.tenant_id, func.count(Device.id))
        .filter(Device.status == DeviceStatus.OFFLINE)
        .group_by(Device.tenant_id)
        .all()
    )
    degraded_counts = dict(
        db.query(Device.tenant_id, func.count(Device.id))
        .filter(Device.status == DeviceStatus.DEGRADED)
        .group_by(Device.tenant_id)
        .all()
    )

    # Open alerts, grouped by tenant (via the alert's device) and severity.
    alert_rows = (
        db.query(Device.tenant_id, Alert.severity, func.count(Alert.id))
        .join(Alert, Alert.device_id == Device.id)
        .filter(Alert.resolved.is_(False), Alert.suppressed.is_(False))
        .group_by(Device.tenant_id, Alert.severity)
        .all()
    )
    alert_counts: dict[uuid.UUID, dict[AlertSeverity, int]] = {}
    for tenant_id, severity, count in alert_rows:
        alert_counts.setdefault(tenant_id, {})[severity] = count

    # Most recent still-open critical alert per tenant, for the "what's
    # actually on fire right now" column.
    latest_critical = (
        db.query(Device.tenant_id, Alert.message, Alert.last_seen_at)
        .join(Alert, Alert.device_id == Device.id)
        .filter(
            Alert.resolved.is_(False),
            Alert.suppressed.is_(False),
            Alert.severity == AlertSeverity.CRITICAL,
        )
        .order_by(Device.tenant_id, Alert.last_seen_at.desc())
        .all()
    )
    latest_critical_by_tenant: dict[uuid.UUID, tuple[str, str | None]] = {}
    for tenant_id, message, last_seen_at in latest_critical:
        if tenant_id not in latest_critical_by_tenant:
            latest_critical_by_tenant[tenant_id] = (
                message,
                last_seen_at.isoformat() if last_seen_at else None,
            )

    # Open incidents, resolved to a tenant via their root-cause alert's
    # device -- Incident has no direct tenant_id/device_id of its own
    # (see app.models.incident), it's reached through the alert it was
    # built from.
    incident_rows = (
        db.query(Device.tenant_id, func.count(Incident.id))
        .join(Alert, Alert.id == Incident.root_cause_alert_id)
        .join(Device, Device.id == Alert.device_id)
        .filter(Incident.status != IncidentStatus.CLOSED)
        .group_by(Device.tenant_id)
        .all()
    )
    incident_counts = dict(incident_rows)

    rows: list[TenantBoardRow] = []
    for tenant in tenants:
        severities = alert_counts.get(tenant.id, {})
        critical = severities.get(AlertSeverity.CRITICAL, 0)
        warning = severities.get(AlertSeverity.WARNING, 0)
        info = severities.get(AlertSeverity.INFO, 0)
        offline = offline_counts.get(tenant.id, 0)
        open_incidents = incident_counts.get(tenant.id, 0)
        latest_msg, latest_at = latest_critical_by_tenant.get(tenant.id, (None, None))

        rows.append(
            TenantBoardRow(
                tenant_id=tenant.id,
                tenant_name=tenant.name,
                tenant_slug=tenant.slug,
                device_count=device_counts.get(tenant.id, 0),
                devices_offline=offline,
                devices_degraded=degraded_counts.get(tenant.id, 0),
                open_alerts_critical=critical,
                open_alerts_warning=warning,
                open_alerts_info=info,
                open_incidents=open_incidents,
                latest_critical_alert_message=latest_msg,
                latest_critical_alert_at=latest_at,
                health_score=_health_score(
                    offline=offline,
                    critical_alerts=critical,
                    warning_alerts=warning,
                    incidents=open_incidents,
                ),
            )
        )

    # Worst-health tenants first -- that's the point of a single pane.
    rows.sort(key=lambda r: r.health_score)
    return TenantBoardResponse(tenants=rows)
