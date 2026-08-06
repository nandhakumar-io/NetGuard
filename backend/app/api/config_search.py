"""Fleet-wide configuration search ("grep across all devices").

Answers questions like "which devices still have telnet enabled" or
"find every device with this ACL line" without opening each device's
Configuration tab one at a time. Built directly on the existing
ConfigSnapshot table -- each device's most recent snapshot is decrypted
in memory and scanned line-by-line; nothing new is persisted, so this
needed no migration.

Deliberately NOT a database LIKE/full-text-index query: running configs
are stored encrypted (ConfigSnapshot.running_config_encrypted), so any
SQL-level search would have to run against ciphertext, which can't match
anything meaningful. Decrypting per-device at search time is the only
correct option given the current storage model. For a fleet in the
hundreds of devices this is still sub-second; if that ever stops being
true, the fix is a background job that maintains a separate searchable
(plaintext or hashed-token) index, not changing this endpoint's contract.
"""
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.device import Device
from app.models.snapshot import ConfigSnapshot
from app.services import snapshot_service

router = APIRouter(prefix="/config-search", tags=["configuration-search"])

MAX_MATCHES_PER_DEVICE = 25
MAX_QUERY_LENGTH = 200
CONTEXT_CHARS = 0  # whole matched line is already the context; kept as a knob for later


class _DeviceMatch:
    __slots__ = ("device_id", "hostname", "ip_address", "matches", "total_match_count", "vendor")

    def __init__(self, device_id, hostname, ip_address, vendor):
        self.device_id = device_id
        self.hostname = hostname
        self.ip_address = ip_address
        self.vendor = vendor
        self.matches: list[dict] = []
        self.total_match_count = 0


@router.get("")
def search_configs(
    query: str = Query(..., min_length=1, max_length=MAX_QUERY_LENGTH, description="Text or regex to search for"),
    regex: bool = Query(False, description="Treat `query` as a regular expression instead of a plain substring"),
    case_sensitive: bool = Query(False),
    device_id: uuid.UUID | None = Query(None, description="Limit the search to one device"),
    limit_devices: int = Query(200, le=500, description="Stop after this many matching devices"),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Searches every device's most recent running-config snapshot for
    `query`, returning one entry per device that has at least one match,
    with the matching line(s) and their line numbers.

    Devices with no snapshot on file yet (never backed up / never
    deployed to) are silently skipped rather than reported as
    zero-match -- there's nothing to search, which is different from
    "searched and found nothing".
    """
    try:
        pattern = re.compile(query if regex else re.escape(query), 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        raise HTTPException(status_code=400, detail=f"Invalid regular expression: {exc}")

    devices_q = db.query(Device)
    if device_id is not None:
        devices_q = devices_q.filter(Device.id == device_id)
    devices = devices_q.order_by(Device.hostname.asc()).all()

    # One query per device for its latest snapshot -- SQLAlchemy has no
    # portable "greatest-n-per-group" without a window function subquery,
    # and this endpoint is already O(devices) on the decrypt step, so a
    # second O(devices) trip for snapshot lookup doesn't change the
    # overall complexity. Fine at MSP fleet scale (hundreds, not tens of
    # thousands, of devices).
    results: list[_DeviceMatch] = []
    devices_searched = 0
    devices_with_no_snapshot = 0

    for device in devices:
        if len(results) >= limit_devices:
            break
        snapshot = (
            db.query(ConfigSnapshot)
            .filter(ConfigSnapshot.device_id == device.id)
            .order_by(ConfigSnapshot.created_at.desc())
            .first()
        )
        if snapshot is None:
            devices_with_no_snapshot += 1
            continue

        devices_searched += 1
        try:
            config_text = snapshot_service.decrypt_config(snapshot.running_config_encrypted)
        except Exception:
            # A corrupt/unreadable snapshot shouldn't take down the whole
            # fleet search -- skip it, same "best-effort" posture as the
            # rest of the config-reading endpoints in this app.
            continue

        device_match = _DeviceMatch(
            device_id=device.id,
            hostname=device.hostname,
            ip_address=device.ip_address,
            vendor=device.vendor.value if hasattr(device.vendor, "value") else str(device.vendor),
        )
        for line_no, line in enumerate(config_text.splitlines(), start=1):
            if pattern.search(line):
                device_match.total_match_count += 1
                if len(device_match.matches) < MAX_MATCHES_PER_DEVICE:
                    device_match.matches.append({"line_number": line_no, "text": line.strip()})

        if device_match.matches:
            results.append(device_match)

    return {
        "query": query,
        "regex": regex,
        "devices_searched": devices_searched,
        "devices_with_no_snapshot": devices_with_no_snapshot,
        "devices_matched": len(results),
        "results": [
            {
                "device_id": str(r.device_id),
                "hostname": r.hostname,
                "ip_address": r.ip_address,
                "vendor": r.vendor,
                "total_match_count": r.total_match_count,
                "truncated": r.total_match_count > len(r.matches),
                "matches": r.matches,
            }
            for r in results
        ],
    }
