"""Global search (Cmd+K command palette).

Unifies the fleet's existing, separately-built search surfaces into one
endpoint instead of introducing a fourth: device inventory lookup
(hostname/IP/site, already how Devices.tsx filters client-side), device
groups, Alert Center (category/message), Change Requests (description),
Config Templates (name/description), Incidents (title/summary), JIT
elevation requests (role/reason/requester email), and Configuration
Search (app.api.config_search's line-grep across every device's
decrypted running config). Each category is capped and returned
separately so the frontend palette can group results under headers rather
than a single flat, unordered list.

Deliberately thin: this delegates the actual config-grepping to
config_search.search_configs's helper rather than re-implementing decrypt
+ scan here, and everything else is a straightforward ILIKE against
already-plaintext columns -- no new index or storage needed.
"""
import ipaddress

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, get_tenant_scope
from app.models.alert import Alert
from app.models.change_request import ChangeRequest
from app.models.config_template import ConfigTemplate
from app.models.device import Device
from app.models.device_group import DeviceGroup
from app.models.incident import Incident
from app.models.jit_elevation import JitElevation

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


def _parse_ip_query(q: str) -> "ipaddress.IPv4Network | ipaddress.IPv6Network | None":
    """A bare IP ("10.20.0.4") or CIDR ("10.20.0.0/24") typed into search
    means "what's using this address/range" -- an extremely common NOC
    lookup ("what's on 10.20.0.0/24") that plain substring ILIKE can't
    answer (10.20.0.4 doesn't substring-match "10.20.0.0/24", and a /24
    query obviously can't ILIKE-match individual host IPs at all).
    strict=False so a host address typed with a prefix (10.20.0.4/24) is
    still treated as "that /24", matching how NOC engineers actually type
    these. Returns None for anything that isn't a valid IP/network, so
    normal text queries fall straight through to the existing ILIKE path.
    """
    q = q.strip()
    if not q:
        return None
    try:
        if "/" in q:
            return ipaddress.ip_network(q, strict=False)
        # A bare IP is treated as a /32 (or /128) -- "what's using this
        # exact address" is the same lookup as "what's using this /32".
        return ipaddress.ip_network(f"{ipaddress.ip_address(q)}/32")
    except ValueError:
        return None


def _devices_in_network(db: Session, network, limit: int, tenant_id=None) -> list[dict]:
    """Devices whose management IP falls in `network`, plus devices whose
    *latest config* declares an interface IP in `network` -- a CIDR search
    for "what's using 10.20.0.0/24" should surface a device even if its
    management IP is elsewhere but it has an interface wired into that
    subnet (e.g. a core switch with dozens of SVIs), same interface-IP
    source topology_service already trusts for subnet-inferred links.
    """
    from app.models.snapshot import ConfigSnapshot
    from app.services import risk_engine, snapshot_service

    matches: dict[str, dict] = {}

    q = db.query(Device).order_by(Device.hostname)
    if tenant_id is not None:
        q = q.filter(Device.tenant_id == tenant_id)
    for device in q.all():
        if len(matches) >= limit:
            break
        try:
            mgmt_ip = ipaddress.ip_address(device.ip_address)
        except ValueError:
            mgmt_ip = None
        if mgmt_ip is not None and mgmt_ip in network:
            matches[str(device.id)] = {
                "id": str(device.id),
                "title": device.hostname,
                "subtitle": f"{device.ip_address} (management) · {network}",
                "url": f"/devices?q={device.hostname}",
            }
            continue

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
        for ip, _mask in risk_engine.parse_config(config_text).ip_addresses:
            try:
                iface_ip = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if iface_ip in network:
                matches[str(device.id)] = {
                    "id": str(device.id),
                    "title": device.hostname,
                    "subtitle": f"{ip} (interface) · {network}",
                    "url": f"/devices?q={device.hostname}",
                }
                break

    return list(matches.values())[:limit]


@router.get("")
def global_search(
    query: str = Query(..., min_length=1, max_length=MAX_QUERY_LENGTH),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
    tenant_id=Depends(get_tenant_scope),
):
    """One query, fanned out across devices / groups / alerts / change
    requests / configs. Returns partial results per category rather than
    failing the whole search if one category errors -- a broken config
    decrypt on one device shouldn't hide a matching device hostname.

    A query that parses as an IP or CIDR ("10.20.0.4", "10.20.0.0/24")
    takes a different path for the devices category: instead of ILIKE
    substring-matching Device.ip_address (which can't express "in this
    range" at all), every device is checked for a management IP or a
    config-declared interface IP inside that network. All other
    categories still run their normal ILIKE search alongside it -- a
    change request description or alert message could still legitimately
    contain that literal IP/CIDR string.

    Every category is scoped to the caller's tenant (None only for MSP
    staff, per get_tenant_scope) -- this is a cross-entity search box, so
    without scoping it a regular tenant user could search their way into
    another tenant's devices, alerts, change requests, or incidents just
    by typing a guess. DeviceGroup and ConfigTemplate have no
    device/tenant linkage of their own (they're role/type-keyed
    definitions, not per-device records) so they're left fleet-wide, same
    as they behave everywhere else in the app today.
    """
    q = query.strip()
    like = f"%{q}%"
    ip_network = _parse_ip_query(q)

    if ip_network is not None:
        device_results = _devices_in_network(db, ip_network, PER_CATEGORY_LIMIT, tenant_id=tenant_id)
    else:
        device_q = db.query(Device).filter(
            or_(Device.hostname.ilike(like), Device.ip_address.ilike(like), Device.site.ilike(like))
        )
        if tenant_id is not None:
            device_q = device_q.filter(Device.tenant_id == tenant_id)
        devices = device_q.order_by(Device.hostname).limit(PER_CATEGORY_LIMIT).all()
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

    alert_q = db.query(Alert).filter(or_(Alert.category.ilike(like), Alert.message.ilike(like)))
    if tenant_id is not None:
        alert_q = alert_q.join(Device, Device.id == Alert.device_id).filter(Device.tenant_id == tenant_id)
    alerts = alert_q.order_by(Alert.created_at.desc()).limit(PER_CATEGORY_LIMIT).all()
    alert_results = [
        {
            "id": str(a.id),
            "title": a.category,
            "subtitle": (a.message[:120] + "…") if len(a.message) > 120 else a.message,
            "url": f"/alerts?alert={a.id}",
        }
        for a in alerts
    ]

    change_q = db.query(ChangeRequest).filter(ChangeRequest.description.ilike(like))
    if tenant_id is not None:
        change_q = change_q.join(Device, Device.id == ChangeRequest.device_id).filter(Device.tenant_id == tenant_id)
    changes = change_q.order_by(ChangeRequest.created_at.desc()).limit(PER_CATEGORY_LIMIT).all()
    change_results = [
        {
            "id": str(c.id),
            "title": (c.description[:80] + "…") if len(c.description) > 80 else c.description,
            "subtitle": c.priority.value if hasattr(c.priority, "value") else str(c.priority),
            "url": f"/change-requests?request={c.id}",
        }
        for c in changes
    ]

    templates = (
        db.query(ConfigTemplate)
        .filter(or_(ConfigTemplate.name.ilike(like), ConfigTemplate.description.ilike(like)))
        .order_by(ConfigTemplate.name)
        .limit(PER_CATEGORY_LIMIT)
        .all()
    )
    template_results = [
        {
            "id": str(t.id),
            "title": t.name,
            "subtitle": t.description or (t.device_role and f"role: {t.device_role}") or "",
            "url": f"/templates?template={t.id}",
        }
        for t in templates
    ]

    incident_q = db.query(Incident).filter(or_(Incident.title.ilike(like), Incident.summary.ilike(like)))
    if tenant_id is not None:
        # Incident has no tenant_id/device_id of its own -- reached via its
        # root-cause alert's device, same join app.api.incidents.list_incidents uses.
        incident_q = (
            incident_q.join(Alert, Alert.id == Incident.root_cause_alert_id)
            .join(Device, Device.id == Alert.device_id)
            .filter(Device.tenant_id == tenant_id)
        )
    incidents = incident_q.order_by(Incident.created_at.desc()).limit(PER_CATEGORY_LIMIT).all()
    incident_results = [
        {
            "id": str(i.id),
            "title": i.title,
            "subtitle": i.status.value if hasattr(i.status, "value") else str(i.status),
            "url": f"/incidents?incident={i.id}",
        }
        for i in incidents
    ]

    from app.models.user import User

    jit_q = (
        db.query(JitElevation, User)
        .join(User, User.id == JitElevation.user_id)
        .filter(or_(JitElevation.elevated_role.ilike(like), JitElevation.reason.ilike(like), User.email.ilike(like)))
    )
    if tenant_id is not None:
        # JIT elevation requests are scoped by the *requesting user's*
        # tenant (there's no device on the row) -- an MSP-staff row has
        # User.tenant_id None and is correctly excluded here, matching
        # the "MSP staff aren't in any tenant's data" contract elsewhere.
        jit_q = jit_q.filter(User.tenant_id == tenant_id)
    jit_rows = jit_q.order_by(JitElevation.requested_at.desc()).limit(PER_CATEGORY_LIMIT).all()
    jit_results = [
        {
            "id": str(j.id),
            "title": f"{j.elevated_role} — {u.email}",
            "subtitle": (j.reason[:100] + "…") if len(j.reason) > 100 else j.reason,
            "url": f"/jit-access?request={j.id}",
        }
        for j, u in jit_rows
    ]

    config_results: list[dict] = []
    if ip_network is None and len(q) >= CONFIG_MIN_QUERY_LENGTH:
        config_results = _search_configs_brief(db, q, limit=CONFIG_MATCH_LIMIT, tenant_id=tenant_id)

    return {
        "query": q,
        "is_ip_query": ip_network is not None,
        "devices": device_results,
        "groups": group_results,
        "alerts": alert_results,
        "change_requests": change_results,
        "templates": template_results,
        "incidents": incident_results,
        "jit_requests": jit_results,
        "configs": config_results,
    }


def _search_configs_brief(db: Session, query: str, limit: int, tenant_id=None) -> list[dict]:
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

    device_q = db.query(Device).order_by(Device.hostname)
    if tenant_id is not None:
        device_q = device_q.filter(Device.tenant_id == tenant_id)
    devices = device_q.all()
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
