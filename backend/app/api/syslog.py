"""Syslog Collection & Correlation API.

Read endpoints are open to any authenticated user, same as every other
monitoring/read surface (Health Dashboard, Alerts, Topology). POST
/syslog/ingest is the HTTP-transport ingestion path -- see
app.services.syslog_service module docstring for how it and the UDP
listener both funnel through the same ingest_message().
"""
import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.device import Device
from app.models.syslog_destination import SyslogDestination
from app.models.syslog_message import SyslogMessage, SyslogSeverity
from app.models.user import UserRole
from app.schemas.syslog import SyslogIngestRequest, SyslogMessageRead, SyslogSummary
from app.schemas.syslog_destination import (
    SyslogDestinationCreate,
    SyslogDestinationRead,
    SyslogDestinationUpdate,
)
from app.services import syslog_forward_service, syslog_service

router = APIRouter(prefix="/syslog", tags=["syslog"])

_admin_only = require_roles(UserRole.NETWORK_ADMIN)


def _to_read(row: SyslogMessage, hostname: str | None) -> SyslogMessageRead:
    payload = SyslogMessageRead.model_validate(row)
    payload.device_hostname = hostname
    return payload


@router.get("", response_model=list[SyslogMessageRead])
def list_syslog_messages(
    device_id: uuid.UUID | None = Query(None),
    min_severity: int = Query(7, ge=0, le=7, description="Numeric syslog severity ceiling (7=debug..0=emergency); default 7 returns everything"),
    category: str | None = Query(None, description="Filter to a correlated_category, e.g. 'Auth Failure'"),
    search: str | None = Query(None, description="Case-insensitive substring match against message text"),
    hours: int = Query(24, ge=1, le=24 * 30),
    limit: int = Query(200, ge=1, le=2000),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Syslog Viewer feed -- newest first, same filter shape as the
    Alert Center (severity/category/search) so the two feel consistent.
    `min_severity` is a *ceiling*: severity numbers count down in
    urgency, so e.g. min_severity=4 returns WARNING(4) and everything
    more severe (0-4), matching how operators think about "at least
    this urgent".
    """
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    query = (
        db.query(SyslogMessage, Device.hostname)
        .outerjoin(Device, Device.id == SyslogMessage.device_id)
        .filter(SyslogMessage.received_at >= since)
    )
    if device_id:
        query = query.filter(SyslogMessage.device_id == device_id)
    query = query.filter(SyslogMessage.severity.in_([s for s in SyslogSeverity if s.value <= min_severity]))
    if category:
        query = query.filter(SyslogMessage.correlated_category == category)
    if search:
        query = query.filter(SyslogMessage.message.ilike(f"%{search}%"))

    rows = query.order_by(SyslogMessage.received_at.desc()).limit(limit).all()
    return [_to_read(row, hostname) for row, hostname in rows]


@router.get("/summary", response_model=SyslogSummary)
def get_syslog_summary(
    hours: int = Query(24, ge=1, le=24 * 30),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    return syslog_service.fleet_syslog_summary(db, since=since)


@router.get("/categories", response_model=list[str])
def list_correlation_categories(_=Depends(get_current_user)):
    """The fixed set of category names CORRELATION_RULES can produce --
    powers the category filter dropdown without a DISTINCT query."""
    return [category for category, _severity, _pattern in syslog_service.CORRELATION_RULES]


@router.get("/devices/{device_id}", response_model=list[SyslogMessageRead])
def get_device_syslog(
    device_id: uuid.UUID,
    hours: int = Query(24, ge=1, le=24 * 30),
    limit: int = Query(200, ge=1, le=2000),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Per-device syslog feed for the device detail view."""
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    device = db.get(Device, device_id)
    rows = (
        db.query(SyslogMessage)
        .filter(SyslogMessage.device_id == device_id, SyslogMessage.received_at >= since)
        .order_by(SyslogMessage.received_at.desc())
        .limit(limit)
        .all()
    )
    hostname = device.hostname if device else None
    return [_to_read(row, hostname) for row in rows]


@router.post("/ingest", response_model=SyslogMessageRead)
def ingest_syslog_message(
    body: SyslogIngestRequest,
    request: Request,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """HTTP-transport ingestion for anything that can't reach the raw UDP
    listener directly (a TCP syslog relay, a device behind NAT that can
    only do outbound HTTPS, or a quick manual test). Goes through the
    exact same syslog_service.ingest_message() as the UDP path, so
    parsing/correlation is identical regardless of transport.
    """
    source_ip = body.source_ip or (request.client.host if request.client else "unknown")
    msg = syslog_service.ingest_message(db, source_ip=source_ip, raw=body.raw)
    device = db.get(Device, msg.device_id) if msg.device_id else None
    return _to_read(msg, device.hostname if device else None)


# --- Remote syslog forwarding destinations --------------------------------
#
# NOC teams typically forward everything NetGuard alerts on into a central
# SIEM/log collector (Splunk, Graylog, rsyslog relay) on top of Slack/
# email/webhooks. Restricted to NETWORK_ADMIN for writes since a
# destination can be pointed at an arbitrary host:port on the network --
# same access bar as webhook endpoint management.


@router.get("/destinations", response_model=list[SyslogDestinationRead])
def list_syslog_destinations(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(SyslogDestination).order_by(SyslogDestination.created_at.desc()).all()


@router.post("/destinations", response_model=SyslogDestinationRead)
def create_syslog_destination(
    body: SyslogDestinationCreate,
    db: Session = Depends(get_db),
    user=Depends(_admin_only),
):
    dest = SyslogDestination(**body.model_dump(), created_by=getattr(user, "email", None))
    db.add(dest)
    db.commit()
    db.refresh(dest)
    return dest


@router.patch("/destinations/{destination_id}", response_model=SyslogDestinationRead)
def update_syslog_destination(
    destination_id: uuid.UUID,
    body: SyslogDestinationUpdate,
    db: Session = Depends(get_db),
    _=Depends(_admin_only),
):
    dest = db.get(SyslogDestination, destination_id)
    if not dest:
        raise HTTPException(status_code=404, detail="Syslog destination not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(dest, field, value)
    db.commit()
    db.refresh(dest)
    return dest


@router.delete("/destinations/{destination_id}")
def delete_syslog_destination(
    destination_id: uuid.UUID,
    db: Session = Depends(get_db),
    _=Depends(_admin_only),
):
    dest = db.get(SyslogDestination, destination_id)
    if not dest:
        raise HTTPException(status_code=404, detail="Syslog destination not found")
    db.delete(dest)
    db.commit()
    return {"ok": True}


@router.post("/destinations/{destination_id}/test")
def test_syslog_destination(
    destination_id: uuid.UUID,
    db: Session = Depends(get_db),
    _=Depends(_admin_only),
):
    dest = db.get(SyslogDestination, destination_id)
    if not dest:
        raise HTTPException(status_code=404, detail="Syslog destination not found")
    ok, err = syslog_forward_service.send_test_message(db, dest)
    if not ok:
        raise HTTPException(status_code=502, detail=f"Test message failed: {err}")
    return {"ok": True}
