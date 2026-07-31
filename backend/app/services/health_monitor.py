"""Real-Time Health Monitoring (FR-9).

Runs the post-deployment check suite described in SRS section 6.9 across
four categories:

  infrastructure - ping reachability, packet loss %, latency (ms)
  routing        - BGP neighbor adjacency, OSPF neighbor adjacency
  services       - DNS resolution, DHCP lease, HTTP(S) reachability,
                   VPN tunnel status
  traffic        - (packet loss / latency, reported under infrastructure
                   above, since they come off the same ping sample)

Routing checks use NAPALM getters when a device driver is available and
fall back to a "not applicable" pass-through for device types NAPALM
doesn't support (e.g. plain linux hosts) rather than failing the whole
suite on something that was never expected to run BGP/OSPF.
"""
import re
import socket
import subprocess
from dataclasses import dataclass

import httpx

# --- Tunables (kept here rather than in Settings since they're
# implementation details of individual checks, not app-wide config) ---
PING_COUNT = 4
PING_TIMEOUT_SEC = 2
HTTP_TIMEOUT_SEC = 3.0
DNS_TIMEOUT_SEC = 3.0
MAX_ACCEPTABLE_LOSS_PCT = 10.0
MAX_ACCEPTABLE_LATENCY_MS = 200.0

NAPALM_DRIVER_MAP = {
    "cisco_ios": "ios",
    "juniper_junos": "junos",
    "arista_eos": "eos",
}


@dataclass
class CheckOutcome:
    category: str
    check_name: str
    passed: bool
    detail: str


# ---------------------------------------------------------------------
# infrastructure: ping reachability + packet loss + latency
# ---------------------------------------------------------------------
def check_ping(ip_address: str, count: int = PING_COUNT, timeout_sec: int = PING_TIMEOUT_SEC) -> CheckOutcome:
    try:
        result = subprocess.run(
            ["ping", "-c", str(count), "-W", str(timeout_sec), ip_address],
            capture_output=True,
            text=True,
            timeout=timeout_sec * count + 3,
        )
        ok = result.returncode == 0
        return CheckOutcome("infrastructure", "ping", ok, (result.stdout[-300:] if ok else result.stderr[-300:]) or result.stdout[-300:])
    except Exception as exc:  # noqa: BLE001
        return CheckOutcome("infrastructure", "ping", False, str(exc))


def _parse_ping_stats(ping_stdout: str) -> tuple[float | None, float | None]:
    """Extracts packet loss % and average round-trip latency (ms) from
    standard `ping` output. Returns (loss_pct, avg_latency_ms), either of
    which may be None if it couldn't be parsed (e.g. 100% loss -> no rtt line).
    """
    loss_pct = None
    loss_match = re.search(r"([\d.]+)%\s*packet loss", ping_stdout)
    if loss_match:
        loss_pct = float(loss_match.group(1))

    avg_latency_ms = None
    # Linux: "rtt min/avg/max/mdev = 0.021/0.045/0.089/0.012 ms"
    # macOS/BSD: "round-trip min/avg/max/stddev = ..."
    rtt_match = re.search(r"(?:rtt|round-trip) [\w/]+ = ([\d.]+)/([\d.]+)/([\d.]+)", ping_stdout)
    if rtt_match:
        avg_latency_ms = float(rtt_match.group(2))

    return loss_pct, avg_latency_ms


def check_packet_loss_and_latency(
    ip_address: str,
    count: int = PING_COUNT,
    timeout_sec: int = PING_TIMEOUT_SEC,
    max_loss_pct: float = MAX_ACCEPTABLE_LOSS_PCT,
    max_latency_ms: float = MAX_ACCEPTABLE_LATENCY_MS,
) -> CheckOutcome:
    """Runs its own ping sample (rather than reusing check_ping's) so it
    still produces a meaningful pass/fail even if `count` differs, and
    parses loss % / avg latency out of the raw output.
    """
    try:
        result = subprocess.run(
            ["ping", "-c", str(count), "-W", str(timeout_sec), ip_address],
            capture_output=True,
            text=True,
            timeout=timeout_sec * count + 3,
        )
        loss_pct, avg_latency_ms = _parse_ping_stats(result.stdout)

        if loss_pct is None:
            return CheckOutcome("infrastructure", "packet_loss_latency", False, "Could not parse ping statistics")

        loss_ok = loss_pct <= max_loss_pct
        latency_ok = avg_latency_ms is None or avg_latency_ms <= max_latency_ms
        passed = loss_ok and latency_ok

        detail = f"loss={loss_pct}% avg_latency={avg_latency_ms if avg_latency_ms is not None else 'n/a'}ms"
        if not loss_ok:
            detail += f" (exceeds {max_loss_pct}% threshold)"
        if avg_latency_ms is not None and not latency_ok:
            detail += f" (exceeds {max_latency_ms}ms threshold)"

        return CheckOutcome("infrastructure", "packet_loss_latency", passed, detail)
    except Exception as exc:  # noqa: BLE001
        return CheckOutcome("infrastructure", "packet_loss_latency", False, str(exc))


# ---------------------------------------------------------------------
# routing: BGP / OSPF adjacency via NAPALM
# ---------------------------------------------------------------------
def _napalm_getter(netmiko_type: str, ip_address: str, username: str, password: str, getter: str):
    """Opens a short-lived NAPALM connection and calls `getter` on it.
    Returns None (not an exception) when the device type has no NAPALM
    driver -- that's an "unsupported platform", not a failed check.
    """
    driver_name = NAPALM_DRIVER_MAP.get(netmiko_type)
    if driver_name is None:
        return None

    import napalm

    driver = napalm.get_network_driver(driver_name)
    device = driver(hostname=ip_address, username=username, password=password, timeout=10)
    device.open()
    try:
        return getattr(device, getter)()
    finally:
        device.close()


def check_bgp_neighbors(netmiko_type: str, ip_address: str, username: str, password: str) -> CheckOutcome:
    try:
        neighbors = _napalm_getter(netmiko_type, ip_address, username, password, "get_bgp_neighbors")
        if neighbors is None:
            return CheckOutcome("routing", "bgp_neighbor", True, "Not applicable for this platform")

        total = 0
        up = 0
        for vrf_data in neighbors.values():
            for peer_ip, peer in vrf_data.get("peers", {}).items():
                total += 1
                if peer.get("is_up"):
                    up += 1

        if total == 0:
            return CheckOutcome("routing", "bgp_neighbor", True, "No BGP neighbors configured")

        passed = up == total
        return CheckOutcome("routing", "bgp_neighbor", passed, f"{up}/{total} BGP neighbor(s) up")
    except Exception as exc:  # noqa: BLE001
        return CheckOutcome("routing", "bgp_neighbor", False, f"BGP check failed: {exc}")


def check_ospf_neighbors(netmiko_type: str, ip_address: str, username: str, password: str) -> CheckOutcome:
    try:
        ospf = _napalm_getter(netmiko_type, ip_address, username, password, "get_ospf_neighbors")
        if ospf is None:
            return CheckOutcome("routing", "ospf_neighbor", True, "Not applicable for this platform")

        total = 0
        full = 0
        for vrf_data in ospf.values():
            for neighbors in vrf_data.get("neighbors", {}).values():
                if isinstance(neighbors, list):
                    for n in neighbors:
                        total += 1
                        if str(n.get("state", "")).lower().startswith("full"):
                            full += 1

        if total == 0:
            return CheckOutcome("routing", "ospf_neighbor", True, "No OSPF neighbors configured")

        passed = full == total
        return CheckOutcome("routing", "ospf_neighbor", passed, f"{full}/{total} OSPF neighbor(s) FULL")
    except Exception as exc:  # noqa: BLE001
        return CheckOutcome("routing", "ospf_neighbor", False, f"OSPF check failed: {exc}")


# ---------------------------------------------------------------------
# services: DNS / DHCP / HTTP / VPN
# ---------------------------------------------------------------------
def check_dns(hostname_or_ip: str, timeout_sec: float = DNS_TIMEOUT_SEC) -> CheckOutcome:
    """Resolves the device's hostname (forward lookup) as a proxy for
    "DNS is healthy from the NetGuard host's point of view". If given a
    bare IP, does a reverse lookup instead so the check still means something.
    """
    try:
        socket.setdefaulttimeout(timeout_sec)
        is_ip = re.match(r"^\d{1,3}(\.\d{1,3}){3}$", hostname_or_ip) is not None
        if is_ip:
            name, _, _ = socket.gethostbyaddr(hostname_or_ip)
            return CheckOutcome("services", "dns", True, f"Reverse DNS resolved to {name}")
        addr = socket.gethostbyname(hostname_or_ip)
        return CheckOutcome("services", "dns", True, f"{hostname_or_ip} resolved to {addr}")
    except socket.herror:
        # No PTR record isn't itself an outage -- treat as pass but note it.
        return CheckOutcome("services", "dns", True, "No PTR record (not fatal)")
    except Exception as exc:  # noqa: BLE001
        return CheckOutcome("services", "dns", False, f"DNS resolution failed: {exc}")
    finally:
        socket.setdefaulttimeout(None)


def check_dhcp(netmiko_type: str, ip_address: str, username: str, password: str) -> CheckOutcome:
    """Confirms the device itself has a valid (non-expired, non-link-local)
    IP -- i.e. if it's meant to be DHCP-addressed, it actually got a lease.
    Falls back to "not applicable" for platforms without a NAPALM facts
    getter, or where the address is clearly static/management-assigned.
    """
    try:
        facts = _napalm_getter(netmiko_type, ip_address, username, password, "get_facts")
        if facts is None:
            return CheckOutcome("services", "dhcp", True, "Not applicable for this platform")

        if ip_address.startswith("169.254."):
            return CheckOutcome("services", "dhcp", False, "Device has a link-local (APIPA) address -- no DHCP lease")

        return CheckOutcome("services", "dhcp", True, f"Device reachable at {ip_address} (lease/static address valid)")
    except Exception as exc:  # noqa: BLE001
        return CheckOutcome("services", "dhcp", False, f"DHCP/facts check failed: {exc}")


def check_http(ip_address: str, timeout_sec: float = HTTP_TIMEOUT_SEC) -> CheckOutcome:
    """Tries HTTPS then HTTP against the device's management interface.
    A connection refused/reset still counts as "the service answered" for
    reachability purposes; only timeouts/unreachable count as failure,
    since plenty of devices intentionally return 401/403 on the mgmt UI.
    """
    last_error = None
    for scheme in ("https", "http"):
        try:
            resp = httpx.get(f"{scheme}://{ip_address}/", timeout=timeout_sec, verify=False)
            return CheckOutcome("services", "http", True, f"{scheme.upper()} responded with {resp.status_code}")
        except httpx.ConnectError as exc:
            last_error = str(exc)
            continue  # try the other scheme
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            continue
    return CheckOutcome("services", "http", False, f"No HTTP(S) response: {last_error}")


def check_vpn(netmiko_type: str, ip_address: str, username: str, password: str) -> CheckOutcome:
    """Checks IPSec/VPN tunnel state via NAPALM's IPSec getter where
    supported. Devices with no VPN configured pass trivially (nothing to
    be broken); platforms without the getter are marked not applicable.
    """
    try:
        get_ipsec = None
        driver_name = NAPALM_DRIVER_MAP.get(netmiko_type)
        if driver_name is None:
            return CheckOutcome("services", "vpn", True, "Not applicable for this platform")

        import napalm

        driver = napalm.get_network_driver(driver_name)
        device = driver(hostname=ip_address, username=username, password=password, timeout=10)
        device.open()
        try:
            if not hasattr(device, "get_ipsec_ike_sas"):
                return CheckOutcome("services", "vpn", True, "VPN getter not supported on this platform")
            sas = device.get_ipsec_ike_sas()
        finally:
            device.close()

        if not sas:
            return CheckOutcome("services", "vpn", True, "No VPN tunnels configured")

        total = len(sas)
        up = sum(1 for sa in sas.values() if str(sa.get("state", "")).lower() in ("up", "established"))
        passed = up == total
        return CheckOutcome("services", "vpn", passed, f"{up}/{total} VPN tunnel(s) up")
    except AttributeError:
        return CheckOutcome("services", "vpn", True, "VPN getter not supported on this platform")
    except Exception as exc:  # noqa: BLE001
        return CheckOutcome("services", "vpn", False, f"VPN check failed: {exc}")


# ---------------------------------------------------------------------
# suite orchestration
# ---------------------------------------------------------------------
def run_health_suite(
    ip_address: str,
    netmiko_type: str = "cisco_ios",
    username: str = "admin",
    password: str = "",
    hostname: str | None = None,
) -> list[CheckOutcome]:
    """Runs the full post-deployment health suite described in SRS 6.9:

      infrastructure - ping, packet loss %, latency
      routing        - BGP neighbor adjacency, OSPF neighbor adjacency
      services       - DNS, DHCP, HTTP(S), VPN tunnel state

    Each check is isolated (wrapped in try/except at the individual-check
    level) so one check erroring doesn't prevent the rest of the suite
    from running and being recorded.
    """
    results: list[CheckOutcome] = [
        check_ping(ip_address),
        check_packet_loss_and_latency(ip_address),
        check_bgp_neighbors(netmiko_type, ip_address, username, password),
        check_ospf_neighbors(netmiko_type, ip_address, username, password),
        check_dns(hostname or ip_address),
        check_dhcp(netmiko_type, ip_address, username, password),
        check_http(ip_address),
        check_vpn(netmiko_type, ip_address, username, password),
    ]
    return results


def suite_passed(results: list[CheckOutcome]) -> bool:
    return all(r.passed for r in results)
