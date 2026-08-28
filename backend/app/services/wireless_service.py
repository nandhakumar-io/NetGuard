"""Wireless AP / SSID monitoring via SNMP.

Polls a Cisco AireOS WLC (or compatible SNMP-enabled wireless controller)
using AIRESPACE-WIRELESS-MIB OIDs and upserts the results into
wireless_aps / wireless_ssids (see migration 0104 and app.models.wireless).

Design notes
------------
* Best-effort: if a device isn't a WLC (walk returns empty) the function
  returns an empty snapshot without error -- callers don't need to filter
  devices before calling.
* Uses the _walk/_get_first_table_value helpers already in snmp_service
  rather than duplicating SNMP session setup.
* Per-band client split: AireOS SNMP doesn't expose this directly in a
  simple scalar -- the clearest way is to walk
  bsnMobileStationPhyType (1.3.6.1.4.1.14179.2.1.4.1.25) and count
  by radio band.  That requires a full client table walk which can be
  large on busy controllers; we omit it in this first pass and leave
  band_2g_clients / band_5g_clients as None (displayed as "n/a" in UI).
  A future follow-up can add the client table walk behind a feature flag.
"""
import datetime
import logging
import uuid

from sqlalchemy.orm import Session

logger = logging.getLogger("netguard.wireless")

# ---------------------------------------------------------------------------
# Cisco AireOS AIRESPACE-WIRELESS-MIB OIDs
# ---------------------------------------------------------------------------
# bsnAPTable (1.3.6.1.4.1.14179.2.2.1.1) -- one row per AP
_OID_AP_NAME = "1.3.6.1.4.1.14179.2.2.1.1.3"       # bsnAPName
_OID_AP_OPER = "1.3.6.1.4.1.14179.2.2.1.1.6"       # bsnAPOperationStatus (1=associated)
_OID_AP_MODEL = "1.3.6.1.4.1.14179.2.2.1.1.16"     # bsnAPModel
_OID_AP_IP = "1.3.6.1.4.1.14179.2.2.1.1.19"        # bsnApIpAddress
_OID_AP_CLIENTS = "1.3.6.1.4.1.14179.2.2.1.1.38"   # bsnApNumOfUsers

# bsnDot11EssTable (1.3.6.1.4.1.14179.2.1.2.1) -- one row per SSID profile
_OID_ESS_SSID = "1.3.6.1.4.1.14179.2.1.2.1.1"       # bsnDot11EssSsid
_OID_ESS_ADMIN = "1.3.6.1.4.1.14179.2.1.2.1.2"      # bsnDot11EssAdminStatus
_OID_ESS_CLIENTS = "1.3.6.1.4.1.14179.2.1.2.1.38"   # bsnDot11EssNumberOfMobileStations


def _safe_int(val: str | None) -> int | None:
    """Parse an SNMP integer string tolerantly; return None on failure."""
    if val is None:
        return None
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return None


def poll_wireless_controller(db: Session, device) -> dict:
    """Walk a WLC's SNMP tables and upsert wireless_aps / wireless_ssids.

    Parameters
    ----------
    db     : active SQLAlchemy session
    device : app.models.device.Device instance with SNMP config

    Returns
    -------
    dict with keys ``ap_count``, ``ssid_count`` for logging / Celery task
    result propagation.  Both are 0 when the device is not an SNMP WLC.
    """
    from app.models.wireless import WirelessAP, WirelessSSID
    from app.services.snmp_service import SnmpAuthConfig, _walk

    # Build SNMP auth from the device's stored config (same as snmp_service).
    if not device.supports_snmp:
        return {"ap_count": 0, "ssid_count": 0, "error": "snmp_disabled"}

    auth = SnmpAuthConfig(
        version=device.snmp_version or "v2c",
        community=None,  # populated below for v1/v2c
        port=device.snmp_port or 161,
        username=device.snmp_username,
        security_level=device.snmp_security_level,
        auth_protocol=device.snmp_auth_protocol,
        priv_protocol=device.snmp_priv_protocol,
    )

    # Resolve secrets for community string and v3 keys if they exist.
    # Uses the same lazy-import + try/except pattern as metrics_service to
    # avoid circular imports.
    try:
        from app.services.secrets_service import resolve_secret
        if device.snmp_version in ("v1", "v2c"):
            auth.community = resolve_secret(db, device.snmp_community_ref) or "public"
        if device.snmp_version == "v3":
            auth.auth_key = resolve_secret(db, device.snmp_auth_credential_ref)
            auth.priv_key = resolve_secret(db, device.snmp_privacy_credential_ref)
    except Exception:  # noqa: BLE001
        if device.snmp_version in ("v1", "v2c"):
            auth.community = "public"

    ip = device.ip_address
    timeout = 5.0

    # ------------------------------------------------------------------
    # AP table walk -- anchor on AP name (bsnAPName) since it's the most
    # universally populated column.  If empty → not a WLC, bail cleanly.
    # ------------------------------------------------------------------
    ap_names = _walk(ip, auth, _OID_AP_NAME, timeout)
    if not ap_names:
        logger.debug("wireless: %s returned no AireOS AP rows -- not a WLC or SNMP unreachable", ip)
        return {"ap_count": 0, "ssid_count": 0}

    ap_oper = _walk(ip, auth, _OID_AP_OPER, timeout)
    ap_model = _walk(ip, auth, _OID_AP_MODEL, timeout)
    ap_ip = _walk(ip, auth, _OID_AP_IP, timeout)
    ap_clients = _walk(ip, auth, _OID_AP_CLIENTS, timeout)

    now = datetime.datetime.now(datetime.timezone.utc)
    ap_count = 0

    for idx, name in ap_names.items():
        existing = (
            db.query(WirelessAP)
            .filter_by(controller_device_id=device.id, ap_index=idx)
            .first()
        )
        if existing is None:
            existing = WirelessAP(
                id=uuid.uuid4(),
                controller_device_id=device.id,
                ap_index=idx,
            )
            db.add(existing)

        existing.ap_name = str(name).strip() or None
        existing.ap_model = ap_model.get(idx)
        existing.ap_ip_address = ap_ip.get(idx)
        existing.oper_status = _safe_int(ap_oper.get(idx))
        existing.client_count = _safe_int(ap_clients.get(idx))
        # Band split intentionally left as None in this pass (see module docstring).
        existing.band_2g_clients = None
        existing.band_5g_clients = None
        existing.polled_at = now
        ap_count += 1

    # ------------------------------------------------------------------
    # SSID table walk
    # ------------------------------------------------------------------
    ess_ssids = _walk(ip, auth, _OID_ESS_SSID, timeout)
    ess_admin = _walk(ip, auth, _OID_ESS_ADMIN, timeout)
    ess_clients = _walk(ip, auth, _OID_ESS_CLIENTS, timeout)

    ssid_count = 0
    for idx, ssid_name in ess_ssids.items():
        ssid_str = str(ssid_name).strip()
        if not ssid_str:
            continue
        existing = (
            db.query(WirelessSSID)
            .filter_by(controller_device_id=device.id, ssid_index=idx)
            .first()
        )
        if existing is None:
            existing = WirelessSSID(
                id=uuid.uuid4(),
                controller_device_id=device.id,
                ssid_index=idx,
                ssid_name=ssid_str,
            )
            db.add(existing)

        existing.ssid_name = ssid_str
        existing.admin_status = _safe_int(ess_admin.get(idx))
        existing.mobile_station_count = _safe_int(ess_clients.get(idx))
        existing.polled_at = now
        ssid_count += 1

    db.commit()
    logger.info("wireless: polled %s → %d APs, %d SSIDs", ip, ap_count, ssid_count)
    return {"ap_count": ap_count, "ssid_count": ssid_count}


def get_aps_for_controller(db: Session, controller_device_id) -> list:
    """Return all WirelessAP rows for a given controller, most-recently
    polled first."""
    from app.models.wireless import WirelessAP
    return (
        db.query(WirelessAP)
        .filter_by(controller_device_id=str(controller_device_id))
        .order_by(WirelessAP.polled_at.desc(), WirelessAP.ap_name)
        .all()
    )


def get_ssids_for_controller(db: Session, controller_device_id) -> list:
    """Return all WirelessSSID rows for a given controller."""
    from app.models.wireless import WirelessSSID
    return (
        db.query(WirelessSSID)
        .filter_by(controller_device_id=str(controller_device_id))
        .order_by(WirelessSSID.ssid_name)
        .all()
    )


def get_wireless_summary(db: Session, controller_device_id, controller_hostname: str | None = None) -> dict:
    """Aggregate snapshot stats for a single controller."""

    aps = get_aps_for_controller(db, controller_device_id)
    ssids = get_ssids_for_controller(db, controller_device_id)

    aps_up = sum(1 for a in aps if a.oper_status == 1)
    total_clients = sum(a.client_count or 0 for a in aps)
    band_2g = sum(a.band_2g_clients or 0 for a in aps)
    band_5g = sum(a.band_5g_clients or 0 for a in aps)
    polled_at = max((a.polled_at for a in aps), default=None) if aps else None

    return {
        "controller_device_id": str(controller_device_id),
        "controller_hostname": controller_hostname,
        "total_aps": len(aps),
        "aps_up": aps_up,
        "aps_down": len(aps) - aps_up,
        "total_clients": total_clients,
        "band_2g_clients": band_2g,
        "band_5g_clients": band_5g,
        "ssid_count": len(ssids),
        "polled_at": polled_at,
    }
