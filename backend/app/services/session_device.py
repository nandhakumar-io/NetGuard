"""Turns the raw `User-Agent` header and client IP captured on a
RefreshToken into the friendly "Firefox on Linux" / "172.21.0.4 · Chennai,
IN" labels shown in Security > Active Sessions.

Both are best-effort and never block login/refresh/session-listing if
they fail: a device label always falls back to something reasonable, and
a location that can't be resolved (private IP, no egress, third-party
service down) is simply omitted rather than raising.
"""
from __future__ import annotations

import ipaddress
import logging

import httpx

try:
    from user_agents import parse as _parse_ua
except ImportError:  # pragma: no cover - guards against an environment
    # that hasn't picked up the new dependency yet (e.g. a backend
    # container that hasn't been rebuilt since this was added).
    _parse_ua = None

logger = logging.getLogger(__name__)

# Best-effort, in-process cache so repeated session-list requests (or many
# users behind the same NAT) don't hammer the geolocation service. Not
# shared across processes/replicas -- fine for a "nice to have" label.
_LOCATION_CACHE: dict[str, str | None] = {}
_LOCATION_CACHE_MAX = 500

_GEOIP_TIMEOUT_SECONDS = 1.5


def device_label(user_agent: str | None) -> str:
    """'Firefox on Linux', 'Chrome on Android', 'Safari on iOS' -- or a
    sane fallback for missing/unparseable/non-browser User-Agents (CLI
    tools, curl, the mobile app's native HTTP client, rows written
    before this field existed)."""
    if not user_agent:
        return "Unknown device"
    if _parse_ua is None:
        return "Unknown device"
    try:
        ua = _parse_ua(user_agent)
    except Exception:  # noqa: BLE001 - never let a weird UA string 500 this
        return "Unknown device"

    browser = ua.browser.family or "Unknown browser"
    if ua.is_bot:
        return browser
    os_family = ua.os.family or "Unknown OS"
    device = "Unknown device"
    if ua.is_mobile or ua.is_tablet:
        device = os_family
    elif ua.is_pc:
        device = os_family
    else:
        device = os_family
    return f"{browser} on {device}"


def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
    )


def location_label(ip_address: str | None) -> str | None:
    """Best-effort 'City, Country' for a public client IP. Returns None
    (never raises) for a missing IP, a private/loopback/reserved address
    (Docker/LAN networking -- geolocation is meaningless there), or if the
    lookup service can't be reached, e.g. no outbound network access.
    """
    if not ip_address:
        return None
    if not _is_public_ip(ip_address):
        return None
    if ip_address in _LOCATION_CACHE:
        return _LOCATION_CACHE[ip_address]

    label: str | None = None
    try:
        resp = httpx.get(
            f"http://ip-api.com/json/{ip_address}",
            params={"fields": "status,city,country,countryCode"},
            timeout=_GEOIP_TIMEOUT_SECONDS,
        )
        data = resp.json()
        if data.get("status") == "success":
            city = data.get("city")
            country = data.get("countryCode") or data.get("country")
            label = ", ".join(p for p in (city, country) if p) or None
    except Exception:  # noqa: BLE001 - best-effort only, e.g. no egress
        logger.debug("session_device: geolocation lookup failed for %s", ip_address, exc_info=True)
        label = None

    if len(_LOCATION_CACHE) >= _LOCATION_CACHE_MAX:
        _LOCATION_CACHE.clear()
    _LOCATION_CACHE[ip_address] = label
    return label


def client_ip(request) -> str | None:
    """Best real client IP for a FastAPI/Starlette Request: honors
    X-Forwarded-For (set by nginx/the load balancer in front of the API
    container -- request.client.host would otherwise always be the proxy's
    address) and falls back to the direct connection.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else None
