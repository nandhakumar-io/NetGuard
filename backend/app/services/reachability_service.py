"""Device reachability (ping) sweep.

Device.status previously only ever got set to ONLINE in one place --
app.api.gns3, when a device is imported from a GNS3 lab topology -- and
was never otherwise updated. Any manually-added ("non-lab") device stayed
at its creation-time default (UNKNOWN) forever, regardless of whether it
was actually reachable, since nothing else in the codebase ever touched
Device.status.

This runs independently of SNMP polling (app.services.metrics_service):
plenty of real devices don't have SNMP configured at all, but should
still show accurate online/offline status from a basic reachability
check. Devices that *are* SNMP-monitored get a slightly richer status:
reachable + a recent unhealthy (yellow/red) reading is reported as
DEGRADED rather than a flat ONLINE.
"""
import datetime
import platform
import socket
import subprocess

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.alert import AlertSource
from app.models.device import Device, DeviceStatus
from app.models.device_metric import DeviceMetric, HealthColor
from app.models.device_status_history import DeviceStatusHistory
from app.services import alert_service, event_bus, notification_service

# Ports tried, in order, for the TCP-connect reachability check -- this is
# the *primary* signal (see is_reachable() below), not ICMP ping.
# Rationale: ICMP ping requires either the `ping` binary being present in
# the runtime image (frequently missing from slim/distroless containers)
# or raw-socket privileges (CAP_NET_RAW, routinely dropped by container
# runtimes/orchestrators for security) -- so a container that's otherwise
# perfectly able to reach a device over the network can still make every
# single ping fail, which would make Device.status *worse* than the
# UNKNOWN it replaces (a confidently wrong OFFLINE instead of an honest
# "don't know yet"). A plain TCP connect needs neither: it's a normal
# outbound socket, same as any other network call this app already makes.
# SSH (22) covers the overwhelming majority of managed network gear;
# 443/80 catch REST/web-managed devices; 23 catches older Telnet-only kit.
_TCP_PROBE_PORTS = (22, 443, 80, 23)


def _tcp_probe(ip_address: str, timeout: float) -> bool:
    for port in _TCP_PROBE_PORTS:
        try:
            with socket.create_connection((ip_address, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _icmp_ping(ip_address: str, timeout: float = 1.0, count: int = 1) -> bool | None:
    """Best-effort ICMP ping. Returns None (not False) if the `ping`
    binary itself isn't available, so callers can tell "confirmed
    unreachable" apart from "couldn't even try" -- collapsing those into
    the same False would make an environment with no `ping` binary look
    identical to a genuinely dead device.
    """
    is_windows = platform.system().lower() == "windows"
    count_flag = "-n" if is_windows else "-c"
    timeout_flag = "-w" if is_windows else "-W"
    timeout_value = str(int(timeout * 1000)) if is_windows else str(max(1, int(timeout)))

    try:
        result = subprocess.run(
            ["ping", count_flag, str(count), timeout_flag, timeout_value, ip_address],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout * count + 2,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return None  # no `ping` binary in this image -- not a reachability signal either way
    except Exception:  # noqa: BLE001 - any other ping failure reads as "not confirmed up"
        return False


def is_reachable(ip_address: str, timeout: float = 1.0) -> bool:
    """A device counts as reachable if EITHER a TCP connect to one of its
    likely management ports succeeds, OR ICMP ping gets a reply. TCP is
    checked first (and is the one guaranteed to work everywhere -- see
    module docstring); ICMP is an extra positive signal for devices that
    only answer ping and have no management port open to this host
    (e.g. behind an ACL that permits ICMP but not the probed TCP ports).
    """
    if _tcp_probe(ip_address, timeout):
        return True
    return bool(_icmp_ping(ip_address, timeout))


def ping_host(ip_address: str, timeout: float = 1.0, count: int = 1) -> bool:
    """Backwards-compatible alias -- prefer is_reachable() for new code;
    this name is kept because it's the more obviously-named entry point
    for anyone skimming for "how do we check if a device is up"."""
    return is_reachable(ip_address, timeout=timeout)


def check_device(db: Session, device: Device) -> DeviceStatus:
    """Pings a device and updates+commits its Device.status, returning
    the new status. Reachable devices are further downgraded to DEGRADED
    if their most recent SNMP health reading (if any, within the last two
    poll intervals so stale readings don't linger) was yellow/red --
    "reachable but unhealthy" is a more useful status than a flat ONLINE
    for a device already flagged as struggling.
    """
    reachable = is_reachable(device.ip_address, timeout=settings.REACHABILITY_PING_TIMEOUT_SECONDS)

    if not reachable:
        new_status = DeviceStatus.OFFLINE
    else:
        new_status = DeviceStatus.ONLINE
        if device.supports_snmp:
            freshness_cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
                seconds=settings.SNMP_POLL_INTERVAL_SECONDS * 2
            )
            latest = (
                db.query(DeviceMetric)
                .filter(DeviceMetric.device_id == device.id, DeviceMetric.polled_at >= freshness_cutoff)
                .order_by(DeviceMetric.polled_at.desc())
                .first()
            )
            if latest is not None and latest.health_color in (HealthColor.YELLOW, HealthColor.RED):
                new_status = DeviceStatus.DEGRADED

    if device.status != new_status:
        was_offline = device.status == DeviceStatus.OFFLINE
        db.add(DeviceStatusHistory(device_id=device.id, status=new_status, previous_status=device.status))
        device.status = new_status
        db.commit()

        # Push the new node color to every open Topology tab immediately --
        # a NOC wall display watching this page needs to see a device drop
        # offline the moment it's detected, not on the next manual refresh.
        event_bus.publish_event(
            "device_status_changed",
            channel=event_bus.TOPOLOGY_CHANNEL,
            device_id=str(device.id),
            status=new_status.value,
        )

        # NOC-style alerting: a device dropping off the network entirely
        # (unplugged switch, powered off, cable pulled) is the single most
        # important event this sweep can detect -- raise it the moment
        # status flips to OFFLINE, and clear it automatically the moment
        # the device answers again, rather than requiring SNMP (which
        # obviously can't reach an offline device either) to notice.
        if new_status == DeviceStatus.OFFLINE:
            alert, is_new = alert_service.raise_alert(
                db,
                device_id=device.id,
                severity="critical",
                source=AlertSource.HEALTH_POLL,
                category="Device Unreachable",
                message=f"{device.hostname} ({device.ip_address}) is not responding to ping/TCP probes",
            )
            if is_new:
                notification_service.notify(
                    event="Device Unreachable",
                    message=f"{device.hostname} ({device.ip_address}) is down",
                    severity="critical",
                )
        elif was_offline:
            alert_service.auto_resolve(
                db,
                device_id=device.id,
                category="Device Unreachable",
                note=f"{device.hostname} ({device.ip_address}) is responding again",
            )

    return new_status
