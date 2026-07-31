"""Configuration Drift Detection.

Compares a device's *live* running-config against the last ConfigSnapshot
NetGuard took of it (the most recent one on file, regardless of whether
that snapshot came from a deployment or a previous drift check). Any
difference means the device was changed outside NetGuard's approved
change-request workflow -- by someone logging in directly, a script, a
vendor tool, etc. -- since that's the only other way its config could move.

Two entry points:
  - `check_device_drift`: the unit of work for ONE device; used both by
    the periodic Celery beat sweep and by an on-demand API call.
  - Called defensively from pipeline_service right before a new deployment
    takes its own snapshot (SRS: catch drift *before* stacking a new
    approved change on top of an already-out-of-band-modified config).
"""
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.config_drift import ConfigDrift, DriftSeverity
from app.models.device import Device
from app.models.snapshot import ConfigSnapshot
from app.services import diff_engine, snapshot_service

NAPALM_DRIVER_MAP = {
    "cisco_ios": "ios",
    "juniper_junos": "junos",
    "arista_eos": "eos",
}

# Line-count thresholds for severity classification. Deliberately simple
# (line count, not semantic parsing) -- good enough to triage "someone
# tweaked the banner" from "someone rewrote the ACLs".
SEVERITY_LOW_MAX_LINES = 3
SEVERITY_MEDIUM_MAX_LINES = 15

# Config lines that touch security-relevant constructs bump severity up
# regardless of line count, since a one-line ACL/AAA change matters more
# than ten lines of banner text.
HIGH_SEVERITY_KEYWORDS = (
    "access-list", "ip access-group", "aaa ", "enable secret", "enable password",
    "username ", "crypto ", "ip route", "router bgp", "router ospf", "snmp-server",
)


@dataclass
class DriftCheckResult:
    drifted: bool
    severity: DriftSeverity
    lines_changed: int
    diff: str
    detail: str


def _fetch_live_config(device: Device, username: str, password: str) -> str:
    """Pulls the device's current running-config. Uses NAPALM where a
    driver exists for the device's vendor; raises for unsupported
    platforms so the caller can record that as a check failure rather
    than silently reporting "no drift".
    """
    netmiko_type_map = {"cisco": "cisco_ios", "juniper": "juniper_junos", "arista": "arista_eos", "linux": "linux"}
    netmiko_type = netmiko_type_map.get(
        device.vendor.value if hasattr(device.vendor, "value") else device.vendor, "cisco_ios"
    )
    driver_name = NAPALM_DRIVER_MAP.get(netmiko_type)
    if driver_name is None:
        raise ValueError(f"Drift detection isn't supported for platform '{netmiko_type}' (no NAPALM driver)")

    import napalm

    driver = napalm.get_network_driver(driver_name)
    conn = driver(hostname=device.ip_address, username=username, password=password, timeout=15)
    conn.open()
    try:
        return conn.get_config()["running"]
    finally:
        conn.close()


def _classify_severity(diff_text: str, lines_changed: int) -> DriftSeverity:
    if lines_changed == 0:
        return DriftSeverity.NONE

    lowered = diff_text.lower()
    if any(keyword in lowered for keyword in HIGH_SEVERITY_KEYWORDS):
        return DriftSeverity.HIGH
    if lines_changed <= SEVERITY_LOW_MAX_LINES:
        return DriftSeverity.LOW
    if lines_changed <= SEVERITY_MEDIUM_MAX_LINES:
        return DriftSeverity.MEDIUM
    return DriftSeverity.HIGH


def compare_configs(baseline_config: str, live_config: str) -> DriftCheckResult:
    diff_text = diff_engine.generate_diff(baseline_config, live_config)
    changed_lines = [
        line for line in diff_text.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    lines_changed = len(changed_lines)
    drifted = lines_changed > 0
    severity = _classify_severity(diff_text, lines_changed)

    detail = (
        "No drift detected -- live config matches last known-good snapshot."
        if not drifted
        else f"{lines_changed} line(s) differ from the last known-good snapshot."
    )
    return DriftCheckResult(drifted=drifted, severity=severity, lines_changed=lines_changed, diff=diff_text, detail=detail)


def check_device_drift(
    db: Session,
    device: Device,
    username: str,
    password: str,
    triggered_by: str = "scheduled",
) -> ConfigDrift:
    """Runs one drift check for `device` and persists the result as a
    ConfigDrift row (recorded whether or not drift was found, so the
    absence of drift is auditable too -- "last checked" matters as much
    as "last found drifted").
    """
    baseline: ConfigSnapshot | None = (
        db.query(ConfigSnapshot)
        .filter(ConfigSnapshot.device_id == device.id)
        .order_by(ConfigSnapshot.created_at.desc())
        .first()
    )

    if baseline is None:
        record = ConfigDrift(
            device_id=device.id,
            baseline_snapshot_id=None,
            drifted="false",
            severity=DriftSeverity.NONE,
            lines_changed=0,
            diff=None,
            detail="No snapshot on file yet -- nothing to compare against (device has never been deployed to).",
            triggered_by=triggered_by,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record

    try:
        live_config = _fetch_live_config(device, username, password)
        baseline_config = snapshot_service.decrypt_config(baseline.running_config_encrypted)
        result = compare_configs(baseline_config, live_config)

        record = ConfigDrift(
            device_id=device.id,
            baseline_snapshot_id=baseline.id,
            drifted="true" if result.drifted else "false",
            severity=result.severity,
            lines_changed=result.lines_changed,
            diff=result.diff if result.drifted else None,
            detail=result.detail,
            triggered_by=triggered_by,
        )
    except Exception as exc:  # noqa: BLE001 - a failed check is its own outcome, not a crash
        record = ConfigDrift(
            device_id=device.id,
            baseline_snapshot_id=baseline.id,
            drifted="false",
            severity=DriftSeverity.NONE,
            lines_changed=0,
            diff=None,
            detail=f"Drift check failed: {exc}",
            triggered_by=triggered_by,
        )

    db.add(record)
    db.commit()
    db.refresh(record)
    return record
