"""Seed a demo user + a small fixed demo dataset (devices, alerts, an
incident, change requests) for DEMO_MODE deployments.

Run from backend/, with DEMO_MODE=true already set (see .env /
app.core.config.settings.DEMO_MODE):

    python seed_demo.py

Idempotent: safe to re-run -- every row is looked up by a fixed
identifying field (hostname, email, title) before insert, so re-running
this after the demo dataset is deleted/reset just recreates what's
missing rather than duplicating everything.

This intentionally does NOT touch real device connectivity: demo device
IP addresses are documentation-range (RFC 5737, 192.0.2.0/24) that will
never resolve to anything real, so SNMP polling for these devices should
be disabled (SNMP_INPROCESS_POLLING_ENABLED=False) in a demo deployment
-- otherwise the poll loop just logs failures every cycle for devices
that were never meant to be reachable. The Terminal page's demo behavior
(app.api.terminal._run_demo_terminal_session) already sidesteps this
entirely once DEMO_MODE=true, regardless of the poll setting.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from app.core.database import SessionLocal  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models.alert import Alert, AlertSeverity, AlertSource  # noqa: E402
from app.models.change_request import (  # noqa: E402
    ChangePriority,
    ChangeRequest,
    ChangeStatus,
)
from app.models.device import Device, DeviceStatus, DeviceVendor  # noqa: E402
from app.models.incident import Incident, IncidentSeverity, IncidentStatus  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402

DEMO_EMAIL = "demo@netguard.io"
DEMO_PASSWORD = "DemoNetGuard1234!"

# RFC 5737 TEST-NET-1 -- guaranteed never to route anywhere real.
DEMO_DEVICES = [
    dict(hostname="demo-core-sw01", ip_address="192.0.2.10", vendor=DeviceVendor.CISCO,
         site="HQ - Demo", device_type="switch", device_role="core", status=DeviceStatus.ONLINE),
    dict(hostname="demo-dist-sw01", ip_address="192.0.2.11", vendor=DeviceVendor.CISCO,
         site="HQ - Demo", device_type="switch", device_role="distribution", status=DeviceStatus.ONLINE),
    dict(hostname="demo-edge-fw01", ip_address="192.0.2.20", vendor=DeviceVendor.JUNIPER,
         site="HQ - Demo", device_type="firewall", device_role="edge-firewall", status=DeviceStatus.DEGRADED),
    dict(hostname="demo-wan-rtr01", ip_address="192.0.2.30", vendor=DeviceVendor.ARISTA,
         site="Branch - Demo", device_type="router", device_role="wan-edge", status=DeviceStatus.ONLINE),
    dict(hostname="demo-access-sw01", ip_address="192.0.2.40", vendor=DeviceVendor.CISCO,
         site="Branch - Demo", device_type="switch", device_role="access", status=DeviceStatus.OFFLINE),
]


def seed_demo_user(db) -> User:
    user = db.query(User).filter(User.email == DEMO_EMAIL).first()
    if user:
        print(f"User '{DEMO_EMAIL}' already exists — reusing.")
        return user
    user = User(
        email=DEMO_EMAIL,
        full_name="Demo Viewer",
        hashed_password=hash_password(DEMO_PASSWORD),
        role=UserRole.NETWORK_ADMIN,
        mfa_enabled="false",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    print(f"Created demo user: {DEMO_EMAIL} / {DEMO_PASSWORD}")
    return user


def seed_devices(db) -> dict[str, Device]:
    by_hostname: dict[str, Device] = {}
    for spec in DEMO_DEVICES:
        existing = db.query(Device).filter(Device.hostname == spec["hostname"]).first()
        if existing:
            by_hostname[spec["hostname"]] = existing
            continue
        device = Device(**spec)
        db.add(device)
        db.flush()
        by_hostname[spec["hostname"]] = device
    db.commit()
    print(f"Devices: {len(by_hostname)} present ({sum(1 for d in by_hostname.values() if d.id)} total).")
    return by_hostname


def seed_alerts(db, devices: dict[str, Device]) -> None:
    fw = devices["demo-edge-fw01"]
    access = devices["demo-access-sw01"]

    existing_categories = {
        (a.device_id, a.category)
        for a in db.query(Alert).filter(Alert.device_id.in_([fw.id, access.id])).all()
    }

    to_create = [
        dict(device_id=fw.id, severity=AlertSeverity.WARNING, source=AlertSource.HEALTH_POLL,
             category="High CPU", message="CPU utilization at 87% for over 10 minutes."),
        dict(device_id=access.id, severity=AlertSeverity.CRITICAL, source=AlertSource.HEALTH_POLL,
             category="Device Unreachable", message="demo-access-sw01 has not responded to health checks."),
    ]
    created = 0
    for spec in to_create:
        if (spec["device_id"], spec["category"]) in existing_categories:
            continue
        db.add(Alert(**spec))
        created += 1
    db.commit()
    print(f"Alerts: {created} created.")


def seed_incident(db, devices: dict[str, Device]) -> None:
    title = "Branch access switch offline"
    if db.query(Incident).filter(Incident.title == title).first():
        print("Incident already present.")
        return
    incident = Incident(
        title=title,
        summary="demo-access-sw01 dropped off the network during a simulated demo scenario.",
        severity=IncidentSeverity.MINOR,
        status=IncidentStatus.MITIGATED,
        detected_at=datetime.now(timezone.utc) - timedelta(hours=3),
        mitigated_at=datetime.now(timezone.utc) - timedelta(hours=2),
        created_by="system:demo-seed",
    )
    db.add(incident)
    db.commit()
    print("Incident created.")


def seed_change_request(db, devices: dict[str, Device], submitter: User) -> None:
    device = devices["demo-dist-sw01"]
    description = "Demo: tighten inbound ACL on distribution uplink"
    if db.query(ChangeRequest).filter(ChangeRequest.description == description).first():
        print("Change request already present.")
        return
    cr = ChangeRequest(
        device_id=device.id,
        submitted_by=submitter.id,
        priority=ChangePriority.MEDIUM,
        description=description,
        business_justification="Demonstrates the approval workflow in the public demo.",
        current_config="interface GigabitEthernet0/1\n description UPLINK-TO-CORE\n",
        proposed_config=(
            "interface GigabitEthernet0/1\n description UPLINK-TO-CORE\n"
            " ip access-group DEMO-INBOUND in\n"
        ),
        status=ChangeStatus.PENDING_APPROVAL,
        risk_score=25,
        risk_findings=json.dumps(["Adds an inbound ACL to a core uplink -- verify existing flows are permitted."]),
        risk_classification="Low Risk",
    )
    db.add(cr)
    db.commit()
    print("Change request created.")


def main() -> None:
    db = SessionLocal()
    try:
        user = seed_demo_user(db)
        devices = seed_devices(db)
        seed_alerts(db, devices)
        seed_incident(db, devices)
        seed_change_request(db, devices, user)
        print("\nDemo dataset ready.")
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
