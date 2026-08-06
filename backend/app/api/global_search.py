"""Global search (Cmd+K command palette).

Unifies the fleet's existing, separately-built search surfaces into one
endpoint instead of introducing a fourth: device inventory lookup
(hostname/IP/site, already how Devices.tsx filters client-side), Alert
Center (category/message), Change Requests (description), and
Configuration Search (app.api.config_search's line-grep across every
device's decrypted running config). Each category is capped and returned
separately so the frontend palette can group results under headers rather
than a single flat, unordered list.

Deliberately thin: this delegates the actual config-grepping to
config_search.search_configs's helper rather than re-implementing decrypt
+ scan here, and everything else is a straightforward ILIKE against
already-plaintext columns -- no new index or storage needed.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.alert import Alert
from app.models.change_request import ChangeRequest
from app.models.device import Device
from app.models.device_group import DeviceGroup

router = APIRouter(prefix="/search", tags=["global-search"])

MAX_QUERY_LENGTH = 200
PER_CATEGORY_LIMIT = 8
# Config search decrypts + scans every device's config -- meaningfully
# more expensive than the ILIKE categories below, so the palette gets a
# smaller slice of it and only runs it once the query is long enough to
# be a plausible config token (avoids grepping the whole fleet on every
# single keystroke of a 1-2 char query).
CONFIG_MIN_QUERY_LENGTH = 3
CONFIG_MATCH_LIMIT = 5


@router.get("")
def global_search(
    query: str = Query(..., min_length=1, max_length=MAX_QUERY_LENGTH),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """One query, fanned out across devices / groups / alerts / change
    requests / configs. Returns partial results per category rather than
    failing the whole search if one category errors -- a broken config
    decrypt on one device shouldn't hide a matching device hostname.
    """
    q = query.strip()
    like = f"%{q}%"

    devices = (
        db.query(Device)
        .filter(or_(Device.hostname.ilike(like), Device.ip_address.ilike(like), Device.site.ilike(like)))
        .order_by(Device.hostname)
        .limit(PER_CATEGORY_LIMIT)
        .all()
    )
    device_results = [
        {
            "id": str(d.id),
            "title": d.hostname,
            "subtitle": f"{d.ip_address}" + (f" · {d.site}" if d.site else ""),
            "url": f"/devices?q={d.hostname}",
        }
        for d in devices
    ]

    groups = (
        db.query(DeviceGroup)
        .filter(or_(DeviceGroup.name.ilike(like), DeviceGroup.description.ilike(like)))
        .order_by(DeviceGroup.name)
        .limit(PER_CATEGORY_LIMIT)
        .all()
    )
    group_results = [
        {
            "id": str(g.id),
            "title": g.name,
            "subtitle": g.description or g.group_type,
            "url": f"/groups?group={g.id}",
        }
        for g in groups
    ]

    alerts = (
        db.query(Alert)
        .filter(or_(Alert.category.ilike(like), Alert.message.ilike(like)))
        .order_by(Alert.created_at.desc())
        .limit(PER_CATEGORY_LIMIT)
        .all()
    )
    alert_results = [
        {
            "id": str(a.id),
            "title": a.category,
            "subtitle": (a.message[:120] + "…") if len(a.message) > 120 else a.message,
            "url": f"/alerts?alert={a.id}",
        }
        for a in alerts
    ]

    changes = (
        db.query(ChangeRequest)
        .filter(ChangeRequest.description.ilike(like))
        .order_by(ChangeRequest.created_at.desc())
        .limit(PER_CATEGORY_LIMIT)
        .all()
    )
    change_results = [
        {
            "id": str(c.id),
            "title": (c.description[:80] + "…") if len(c.description) > 80 else c.description,
            "subtitle": c.priority.value if hasattr(c.priority, "value") else str(c.priority),
            "url": f"/change-requests?request={c.id}",
        }
        for c in changes
    ]

    config_results: list[dict] = []
    if len(q) >= CONFIG_MIN_QUERY_LENGTH:
        config_results = _search_configs_brief(db, q, limit=CONFIG_MATCH_LIMIT)

    return {
        "query": q,
        "devices": device_results,
        "groups": group_results,
        "alerts": alert_results,
        "change_requests": change_results,
        "configs": config_results,
    }


def _search_configs_brief(db: Session, query: str, limit: int) -> list[dict]:
    """Best-effort plain-substring config grep for the palette -- a
    trimmed version of config_search.search_configs's scan (no regex
    option, no per-device match list, just "which devices matched and
    the first matching line") since the palette only has room to show a
    device name + one line snippet per result, not a full match report.
    """
    import re

    from app.models.snapshot import ConfigSnapshot
    from app.services import snapshot_service

    pattern = re.compile(re.escape(query), re.IGNORECASE)
    results: list[dict] = []

    devices = db.query(Device).order_by(Device.hostname).all()
    for device in devices:
        if len(results) >= limit:
            break
        snap = (
            db.query(ConfigSnapshot)
            .filter(ConfigSnapshot.device_id == device.id)
            .order_by(ConfigSnapshot.seq.desc())
            .first()
        )
        if not snap:
            continue
        try:
            config_text = snapshot_service.decrypt_config(snap.running_config_encrypted)
        except Exception:
            continue
        for line in config_text.splitlines():
            if pattern.search(line):
                results.append(
                    {
                        "id": str(device.id),
                        "title": device.hostname,
                        "subtitle": line.strip()[:120],
                        "url": f"/config-search?q={query}&device_id={device.id}",
                    }
                )
                break
    return results
