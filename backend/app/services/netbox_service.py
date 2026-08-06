"""NetBox -> NetGuard device inventory pull-sync.

Most real MSP/enterprise networks already have NetBox (or a similar
NetBox-API-compatible IPAM/DCIM tool) as the source of truth for "what
devices exist". Right now every device in NetGuard has to be added one at
a time (manually or discovered via the GNS3 lab integration), which is a
constant source of "why isn't this device showing up" tickets whenever
inventory drifts between the two systems. This is a one-way pull
(NetBox -> NetGuard): NetGuard never writes back to NetBox.

Maps NetBox's `dcim.devices` list endpoint into NetGuard's Device model:

    NetBox field                       -> NetGuard Device field
    ------------------------------------------------------------
    name                                -> hostname
    primary_ip4.address (minus /prefix) -> ip_address
    device_type.manufacturer.slug       -> vendor (best-effort mapped)
    device_role.name / .slug            -> device_role
    site.name                           -> site
    device_type.model                   -> model
    serial                              -> serial_number
    id                                  -> netbox_id (sync match key)

Devices with no primary IP assigned in NetBox are skipped (NetGuard has
no use for a device it can't reach), and every skip is reported back to
the caller by reason so a sync run is diagnosable rather than a silent
"12 devices, only 7 imported".
"""
import datetime
import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.device import Device, DeviceVendor

logger = logging.getLogger("netguard.netbox_sync")

# NetBox manufacturer slugs -> NetGuard's fixed DeviceVendor enum. NetBox
# manufacturers are free-text/unbounded (any org can add "Fortinet",
# "Palo Alto", etc.), but NetGuard's protocol/SNMP/NAPALM integrations
# only know how to actually talk to these four -- anything else still
# gets imported (inventory value alone is useful even without live
# telemetry) but flagged so the sync summary can call it out rather than
# silently mis-tagging e.g. a Palo Alto firewall as "cisco".
_MANUFACTURER_SLUG_MAP = {
    "cisco": DeviceVendor.CISCO,
    "juniper": DeviceVendor.JUNIPER,
    "juniper-networks": DeviceVendor.JUNIPER,
    "arista": DeviceVendor.ARISTA,
    "arista-networks": DeviceVendor.ARISTA,
    "linux": DeviceVendor.LINUX,
}


class NetBoxSyncError(Exception):
    pass


def _client():
    """Lazy import (same convention as netmiko/napalm/ncclient elsewhere
    in this app) so environments that never configure NetBox don't need
    the dependency installed at all."""
    import httpx

    if not settings.NETBOX_URL or not settings.NETBOX_TOKEN:
        raise NetBoxSyncError(
            "NetBox is not configured -- set NETBOX_URL and NETBOX_TOKEN (see .env.example) before syncing."
        )
    return httpx.Client(
        base_url=settings.NETBOX_URL.rstrip("/"),
        headers={"Authorization": f"Token {settings.NETBOX_TOKEN}", "Accept": "application/json"},
        verify=settings.NETBOX_VERIFY_SSL,
        timeout=settings.NETBOX_TIMEOUT_SECONDS,
    )


def _fetch_all_devices(client) -> list[dict]:
    """NetBox paginates dcim/devices/ (default 50/page) -- follows `next`
    until exhausted. A single org's device count is small enough (low
    thousands at most) that pulling everything into memory before
    upserting is simpler and safer than trying to stream+upsert
    page-by-page against a DB session mid-iteration."""
    results: list[dict] = []
    url = "/api/dcim/devices/?limit=200"
    while url:
        resp = client.get(url)
        resp.raise_for_status()
        payload = resp.json()
        results.extend(payload.get("results", []))
        next_url = payload.get("next")
        # NetBox returns a fully-qualified next URL; httpx.Client with a
        # base_url set will still resolve a relative path correctly, but
        # the API gives us absolute -- strip the base so repeated calls
        # go through the same configured client (auth headers, verify,
        # timeout) instead of firing off an unauthenticated raw request.
        if next_url and next_url.startswith(str(client.base_url)):
            next_url = next_url[len(str(client.base_url)) :]
        url = next_url
    return results


def _map_vendor(manufacturer_slug: str | None) -> DeviceVendor | None:
    if not manufacturer_slug:
        return None
    return _MANUFACTURER_SLUG_MAP.get(manufacturer_slug.lower())


def sync_devices(db: Session, dry_run: bool = False) -> dict:
    """Pulls every device from NetBox and upserts it into NetGuard's
    Device table, matched by netbox_id (falling back to hostname for a
    device that predates this sync, so re-running sync on an existing
    manually-added device adopts it rather than creating a duplicate).

    dry_run=True does the full fetch + diff but makes no DB writes --
    lets an operator preview exactly what a sync would do (created vs.
    updated vs. skipped, with reasons) before committing to it.
    """
    client = _client()
    try:
        raw_devices = _fetch_all_devices(client)
    except Exception as exc:
        raise NetBoxSyncError(f"Could not reach NetBox: {exc}") from exc
    finally:
        client.close()

    created, updated, skipped, unmapped_vendor = [], [], [], []

    for nb in raw_devices:
        name = nb.get("name")
        if not name:
            skipped.append({"netbox_id": nb.get("id"), "reason": "No device name set in NetBox"})
            continue

        primary_ip = (nb.get("primary_ip4") or nb.get("primary_ip") or {}).get("address")
        if not primary_ip:
            skipped.append({"netbox_id": nb.get("id"), "hostname": name, "reason": "No primary IP assigned in NetBox"})
            continue
        ip_address = primary_ip.split("/")[0]

        manufacturer_slug = ((nb.get("device_type") or {}).get("manufacturer") or {}).get("slug")
        vendor = _map_vendor(manufacturer_slug)
        if vendor is None:
            unmapped_vendor.append({"hostname": name, "manufacturer": manufacturer_slug or "(none)"})
            vendor = DeviceVendor.CISCO  # inventory-only fallback; protocol/SNMP won't work until corrected

        device_role = (nb.get("device_role") or nb.get("role") or {}).get("name")
        site = (nb.get("site") or {}).get("name")
        model = (nb.get("device_type") or {}).get("model")
        serial = nb.get("serial") or None

        existing = db.query(Device).filter(Device.netbox_id == nb["id"]).first()
        if existing is None:
            # Adopt a pre-existing manually-added device with the same
            # hostname instead of creating a duplicate row for it.
            existing = db.query(Device).filter(Device.hostname == name).first()

        is_new = existing is None
        if dry_run:
            (created if is_new else updated).append({"hostname": name, "ip_address": ip_address})
            continue

        if is_new:
            existing = Device(hostname=name, vendor=vendor)
            db.add(existing)

        existing.hostname = name
        existing.ip_address = ip_address
        existing.vendor = vendor
        existing.device_role = device_role
        existing.site = site
        existing.model = model
        existing.serial_number = serial
        existing.netbox_id = nb["id"]
        existing.netbox_last_synced_at = datetime.datetime.now(datetime.timezone.utc)

        (created if is_new else updated).append({"hostname": name, "ip_address": ip_address})

    if not dry_run:
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise NetBoxSyncError(
                "Sync aborted: a hostname collision was found (two NetBox devices sharing a name, or a "
                "NetBox device reusing the hostname of an existing manually-added device). No changes "
                "were saved -- resolve the naming conflict in NetBox and retry."
            ) from exc

    return {
        "dry_run": dry_run,
        "netbox_devices_seen": len(raw_devices),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "unmapped_vendor": unmapped_vendor,
    }
