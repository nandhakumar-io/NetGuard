"""Plain-CSV bulk import/export for devices, alongside the existing
NetBox pull-sync (app.services.netbox_service) -- for orgs that don't run
NetBox, or want a one-off bulk load/edit via spreadsheet.

Import matches existing devices by hostname (same match-key convention
netbox_service uses, minus the netbox_id path -- there's no external
system id here) and updates them in place; a hostname not already in the
DB creates a new device. This mirrors netbox_service.sync_devices'
create-or-update behavior so the two import paths feel consistent.
"""
from __future__ import annotations

import csv
import io
import json

from sqlalchemy.orm import Session

from app.models.device import Device, DeviceLifecycleState, DeviceVendor

# Columns written on export / accepted on import. Credentials and
# derived/computed fields (EOL info, snmp_credentials_configured, ...)
# are deliberately excluded -- this is an inventory data interchange
# format, not a full device dump.
CSV_FIELDS = [
    "hostname",
    "ip_address",
    "vendor",
    "site",
    "device_type",
    "device_role",
    "lifecycle_state",
    "data_center",
    "rack",
    "rack_position",
    "platform",
    "model",
    "serial_number",
    "os_version",
    "tags",
    "custom_fields",
]


def export_devices_csv(devices: list[Device]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    writer.writeheader()
    for d in devices:
        tags = []
        if d.tags:
            try:
                parsed = json.loads(d.tags)
                if isinstance(parsed, list):
                    tags = parsed
            except (ValueError, TypeError):
                pass
        custom_fields = {}
        if d.custom_fields:
            try:
                parsed = json.loads(d.custom_fields)
                if isinstance(parsed, dict):
                    custom_fields = parsed
            except (ValueError, TypeError):
                pass
        writer.writerow(
            {
                "hostname": d.hostname,
                "ip_address": d.ip_address,
                "vendor": d.vendor.value if d.vendor else "",
                "site": d.site or "",
                "device_type": d.device_type or "",
                "device_role": d.device_role or "",
                "lifecycle_state": d.lifecycle_state.value if d.lifecycle_state else "",
                "data_center": d.data_center or "",
                "rack": d.rack or "",
                "rack_position": d.rack_position if d.rack_position is not None else "",
                "platform": d.platform or "",
                "model": d.model or "",
                "serial_number": d.serial_number or "",
                "os_version": d.os_version or "",
                "tags": ";".join(tags),
                "custom_fields": json.dumps(custom_fields) if custom_fields else "",
            }
        )
    return buf.getvalue()


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def import_devices_csv(db: Session, content: str) -> dict:
    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None or "hostname" not in reader.fieldnames:
        raise ValueError("CSV must include a 'hostname' column")

    created: list[str] = []
    updated: list[str] = []
    errors: list[dict] = []
    total_rows = 0

    existing_by_hostname = {d.hostname: d for d in db.query(Device).all()}

    for i, row in enumerate(reader, start=2):  # row 1 is the header
        total_rows += 1
        hostname = _clean(row.get("hostname"))
        if not hostname:
            errors.append({"row": i, "hostname": None, "error": "hostname is required"})
            continue

        try:
            vendor_raw = _clean(row.get("vendor"))
            vendor = DeviceVendor(vendor_raw) if vendor_raw else None

            lifecycle_raw = _clean(row.get("lifecycle_state"))
            lifecycle = DeviceLifecycleState(lifecycle_raw) if lifecycle_raw else None

            rack_position_raw = _clean(row.get("rack_position"))
            rack_position = int(rack_position_raw) if rack_position_raw else None

            tags_raw = _clean(row.get("tags"))
            tags_list = [t.strip() for t in tags_raw.split(";") if t.strip()] if tags_raw else []

            custom_fields_raw = _clean(row.get("custom_fields"))
            custom_fields = {}
            if custom_fields_raw:
                parsed = json.loads(custom_fields_raw)
                if isinstance(parsed, dict):
                    custom_fields = {str(k): str(v) for k, v in parsed.items()}
        except ValueError as exc:
            errors.append({"row": i, "hostname": hostname, "error": f"Invalid value: {exc}"})
            continue

        device = existing_by_hostname.get(hostname)
        is_new = device is None
        if is_new:
            ip_address = _clean(row.get("ip_address"))
            if not ip_address:
                errors.append({"row": i, "hostname": hostname, "error": "ip_address is required for new devices"})
                continue
            device = Device(hostname=hostname, ip_address=ip_address, vendor=vendor or DeviceVendor.CISCO)
            db.add(device)
            existing_by_hostname[hostname] = device
        else:
            if _clean(row.get("ip_address")):
                device.ip_address = _clean(row.get("ip_address"))
            if vendor:
                device.vendor = vendor

        if lifecycle:
            device.lifecycle_state = lifecycle
        device.site = _clean(row.get("site")) or device.site if not is_new else _clean(row.get("site"))
        device.device_type = _clean(row.get("device_type")) or (device.device_type if not is_new else None)
        device.device_role = _clean(row.get("device_role")) or (device.device_role if not is_new else None)
        device.data_center = _clean(row.get("data_center")) or (device.data_center if not is_new else None)
        device.rack = _clean(row.get("rack")) or (device.rack if not is_new else None)
        if rack_position is not None:
            device.rack_position = rack_position
        device.platform = _clean(row.get("platform")) or (device.platform if not is_new else None)
        device.model = _clean(row.get("model")) or (device.model if not is_new else None)
        device.serial_number = _clean(row.get("serial_number")) or (device.serial_number if not is_new else None)
        device.os_version = _clean(row.get("os_version")) or (device.os_version if not is_new else None)
        if tags_raw is not None:
            device.tags = json.dumps(tags_list) if tags_list else None
        if custom_fields_raw is not None:
            device.custom_fields = json.dumps(custom_fields) if custom_fields else None

        (created if is_new else updated).append(hostname)

    db.commit()
    return {"created": created, "updated": updated, "errors": errors, "total_rows": total_rows}
