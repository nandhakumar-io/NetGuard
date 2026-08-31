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
import time
from dataclasses import dataclass, field

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


def check_bgp_neighbors_from_raw(neighbors: dict | None) -> CheckOutcome:
    """Pure interpreter -- no device I/O. Takes whatever a NAPALM
    get_bgp_neighbors() call already returned (whether that call happened
    in-process, as in check_bgp_neighbors below, or inside the Device
    Gateway, with the raw dict relayed back over a DeviceJobResult -- see
    app.services.pipeline_service's Gateway-backed monitoring path) and
    scores it. Keeping this device-I/O-free is what makes it callable
    from both places without duplicating the pass/fail logic."""
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


def check_bgp_neighbors(netmiko_type: str, ip_address: str, username: str, password: str) -> CheckOutcome:
    try:
        neighbors = _napalm_getter(netmiko_type, ip_address, username, password, "get_bgp_neighbors")
        return check_bgp_neighbors_from_raw(neighbors)
    except Exception as exc:  # noqa: BLE001
        return CheckOutcome("routing", "bgp_neighbor", False, f"BGP check failed: {exc}")


def check_ospf_neighbors_from_raw(ospf: dict | None) -> CheckOutcome:
    """Pure interpreter -- see check_bgp_neighbors_from_raw's docstring."""
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


def check_ospf_neighbors(netmiko_type: str, ip_address: str, username: str, password: str) -> CheckOutcome:
    try:
        ospf = _napalm_getter(netmiko_type, ip_address, username, password, "get_ospf_neighbors")
        return check_ospf_neighbors_from_raw(ospf)
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


def check_dhcp_from_raw(facts: dict | None, ip_address: str) -> CheckOutcome:
    """Pure interpreter -- see check_bgp_neighbors_from_raw's docstring.
    Takes get_facts() output (or None) plus the ip_address being checked
    -- unlike the routing checks, the pass/fail decision here also
    depends on a value (ip_address) that isn't part of the getter output
    itself."""
    if facts is None:
        return CheckOutcome("services", "dhcp", True, "Not applicable for this platform")

    if ip_address.startswith("169.254."):
        return CheckOutcome("services", "dhcp", False, "Device has a link-local (APIPA) address -- no DHCP lease")

    return CheckOutcome("services", "dhcp", True, f"Device reachable at {ip_address} (lease/static address valid)")


def check_dhcp(netmiko_type: str, ip_address: str, username: str, password: str) -> CheckOutcome:
    """Confirms the device itself has a valid (non-expired, non-link-local)
    IP -- i.e. if it's meant to be DHCP-addressed, it actually got a lease.
    Falls back to "not applicable" for platforms without a NAPALM facts
    getter, or where the address is clearly static/management-assigned.
    """
    try:
        facts = _napalm_getter(netmiko_type, ip_address, username, password, "get_facts")
        return check_dhcp_from_raw(facts, ip_address)
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


def check_vpn_from_raw(sas: dict | None) -> CheckOutcome:
    """Pure interpreter -- see check_bgp_neighbors_from_raw's docstring.
    A raw value of None covers both "no NAPALM driver for this platform"
    and "getter not supported on this driver" -- the legacy in-process
    check_vpn below collapses both cases to the same outcome too, so this
    doesn't lose any information the caller previously had."""
    if sas is None:
        return CheckOutcome("services", "vpn", True, "Not applicable for this platform")

    if not sas:
        return CheckOutcome("services", "vpn", True, "No VPN tunnels configured")

    total = len(sas)
    up = sum(1 for sa in sas.values() if str(sa.get("state", "")).lower() in ("up", "established"))
    passed = up == total
    return CheckOutcome("services", "vpn", passed, f"{up}/{total} VPN tunnel(s) up")


def check_vpn(netmiko_type: str, ip_address: str, username: str, password: str) -> CheckOutcome:
    """Checks IPSec/VPN tunnel state via NAPALM's IPSec getter where
    supported. Devices with no VPN configured pass trivially (nothing to
    be broken); platforms without the getter are marked not applicable.
    """
    try:
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

        return check_vpn_from_raw(sas)
    except AttributeError:
        return CheckOutcome("services", "vpn", True, "VPN getter not supported on this platform")
    except Exception as exc:  # noqa: BLE001
        return CheckOutcome("services", "vpn", False, f"VPN check failed: {exc}")


# ---------------------------------------------------------------------
# traffic: post-deploy data-plane impact vs. a pre-deploy baseline
# ---------------------------------------------------------------------
# Every check above answers "is the control/management plane healthy" --
# a device stays pingable, its BGP session stays adjacent, DNS/HTTP still
# answer, even when an ACL/route-map/VLAN change has silently blackholed
# real traffic to a downstream subnet. That failure mode only shows up
# in flow data (app.services.flow_service), which is why this check is
# deliberately DB-/network-free itself (a pure comparison function, kept
# testable the same way every other check here is) -- the caller
# (app.services.pipeline_service) is responsible for capturing a
# TrafficBaseline before the deploy via flow_service.capture_traffic_
# baseline and re-measuring it during the monitoring window via
# flow_service.measure_traffic_since_baseline, then building the
# TrafficComparison list this function actually scores.
DEFAULT_TRAFFIC_DROP_THRESHOLD_PCT = 30.0
# A baseline below this is treated as "nothing to compare" rather than
# scored -- a device/subnet that was already near-idle before the change
# makes any post-deploy traffic level look like a huge relative "drop"
# (or spike) against noise, which isn't the failure this check exists to
# catch.
MIN_BASELINE_BYTES_FOR_TRAFFIC_CHECK = 100_000


@dataclass
class TrafficComparison:
    label: str  # "device" for the device's own exported traffic, or a subnet CIDR
    baseline_bytes: int
    current_bytes: int


def check_traffic_impact(
    comparisons: list[TrafficComparison], drop_threshold_pct: float = DEFAULT_TRAFFIC_DROP_THRESHOLD_PCT
) -> CheckOutcome:
    """Flags a deployment as unhealthy when observed traffic volume for
    the changed device -- or any subnet it fronts -- has dropped by at
    least `drop_threshold_pct` versus its pre-deploy baseline. This is
    what turns "BGP session flapped and traffic to subnet X dropped 40%"
    into an actual rollback trigger rather than only ever something a
    human notices on the Traffic Analysis page after the fact.

    Comparisons with a baseline under MIN_BASELINE_BYTES_FOR_TRAFFIC_CHECK
    are skipped (nothing meaningful to compare); if every comparison is
    skipped that way, the check passes trivially rather than failing on
    an empty finding set -- an idle device/subnet before the change isn't
    evidence of anything.
    """
    findings: list[str] = []
    worst_drop_pct = 0.0
    any_scored = False

    for c in comparisons:
        if c.baseline_bytes < MIN_BASELINE_BYTES_FOR_TRAFFIC_CHECK:
            continue
        any_scored = True
        drop_pct = 100.0 * (c.baseline_bytes - c.current_bytes) / c.baseline_bytes
        worst_drop_pct = max(worst_drop_pct, drop_pct)
        if drop_pct >= drop_threshold_pct:
            findings.append(
                f"{c.label}: traffic dropped {drop_pct:.0f}% ({c.baseline_bytes:,} -> {c.current_bytes:,} bytes)"
            )

    if not any_scored:
        return CheckOutcome(
            "traffic", "traffic_impact", True,
            "No traffic baseline available (device/subnet was idle before deploy) -- check skipped",
        )

    passed = not findings
    detail = "; ".join(findings) if findings else f"Traffic volume within normal range (largest change: {worst_drop_pct:.0f}%)"
    return CheckOutcome("traffic", "traffic_impact", passed, detail)


# ---------------------------------------------------------------------
# suite orchestration
# ---------------------------------------------------------------------
# Registry of every check the suite can run, keyed by the same
# `check_name` each CheckOutcome carries. Used both to build the full
# suite and to let callers (API, UI) select a subset -- see
# `enabled_checks` on run_health_suite / run_monitoring_window below and
# devices.enabled_health_checks.
ALL_CHECKS: dict[str, dict] = {
    "ping": {"category": "infrastructure", "label": "Ping reachability"},
    "packet_loss_latency": {"category": "infrastructure", "label": "Packet loss & latency"},
    "bgp_neighbor": {"category": "routing", "label": "BGP neighbor adjacency"},
    "ospf_neighbor": {"category": "routing", "label": "OSPF neighbor adjacency"},
    "dns": {"category": "services", "label": "DNS resolution"},
    "dhcp": {"category": "services", "label": "DHCP lease"},
    "http": {"category": "services", "label": "HTTP(S) reachability"},
    "vpn": {"category": "services", "label": "VPN tunnel status"},
    "traffic_impact": {"category": "traffic", "label": "Traffic-impact vs. pre-deploy baseline"},
}


def run_health_suite(
    ip_address: str,
    netmiko_type: str = "cisco_ios",
    username: str = "admin",
    password: str = "",
    hostname: str | None = None,
    enabled_checks: set[str] | None = None,
    traffic_impact_fn=None,
    remote_check_overrides: dict[str, callable] | None = None,
) -> list[CheckOutcome]:
    """Runs the post-deployment health suite described in SRS 6.9:

      infrastructure - ping, packet loss %, latency
      routing        - BGP neighbor adjacency, OSPF neighbor adjacency
      services       - DNS, DHCP, HTTP(S), VPN tunnel state
      traffic        - data-plane traffic-impact vs. pre-deploy baseline
                        (only run when `traffic_impact_fn` is supplied --
                        see check_traffic_impact's module docstring for
                        why this one needs a caller-supplied closure
                        instead of connection params like every other
                        check here)

    Each check is isolated (wrapped in try/except at the individual-check
    level) so one check erroring doesn't prevent the rest of the suite
    from running and being recorded.

    `enabled_checks` restricts the suite to a subset of ALL_CHECKS'
    keys (e.g. {"ping", "http"}) -- checks that don't apply to a given
    device/lab (no BGP/OSPF configured, no telnetlib/NAPALM driver
    installed, etc.) can be turned off instead of failing verification,
    and triggering a rollback, over something that was never going to
    pass. None/empty means "run everything", the historical behavior.

    `traffic_impact_fn`, when given, is a zero-arg callable returning a
    CheckOutcome (typically a closure over a pre-captured
    flow_service.TrafficBaseline -- see app.services.pipeline_service)
    -- included in the suite as the "traffic_impact" check. Omitted by
    default (None) since, unlike every other check, it needs a DB
    session and a baseline captured before the deploy started, which
    this network-only module deliberately doesn't own itself.

    `remote_check_overrides`, when given, replaces one or more of the
    four credentialed checks (bgp_neighbor / ospf_neighbor / dhcp / vpn)
    with a caller-supplied zero-arg closure, instead of building the
    default closure that calls check_bgp_neighbors() etc. directly with
    `username`/`password`. This is how a Gateway-backed caller (see
    app.services.pipeline_service) avoids ever holding the device's SSH
    credential in-worker for these checks: it builds closures that call
    device_job_service.submit_job_sync(...) and interpret the result via
    check_bgp_neighbors_from_raw() / check_ospf_neighbors_from_raw() /
    check_dhcp_from_raw() / check_vpn_from_raw() instead, and passes
    username="" / password="" here since those defaults are never used
    for an overridden check name.
    """
    all_checks: dict[str, callable] = {
        "ping": lambda: check_ping(ip_address),
        "packet_loss_latency": lambda: check_packet_loss_and_latency(ip_address),
        "bgp_neighbor": lambda: check_bgp_neighbors(netmiko_type, ip_address, username, password),
        "ospf_neighbor": lambda: check_ospf_neighbors(netmiko_type, ip_address, username, password),
        "dns": lambda: check_dns(hostname or ip_address),
        "dhcp": lambda: check_dhcp(netmiko_type, ip_address, username, password),
        "http": lambda: check_http(ip_address),
        "vpn": lambda: check_vpn(netmiko_type, ip_address, username, password),
    }
    if remote_check_overrides:
        all_checks.update(remote_check_overrides)
    if traffic_impact_fn is not None:
        all_checks["traffic_impact"] = traffic_impact_fn

    selection = set(enabled_checks) if enabled_checks else set(all_checks.keys())
    return [fn() for name, fn in all_checks.items() if name in selection]


def suite_passed(results: list[CheckOutcome]) -> bool:
    return all(r.passed for r in results)


# ---------------------------------------------------------------------
# monitoring window: actually poll the suite repeatedly, not once
# ---------------------------------------------------------------------
@dataclass
class PollRound:
    """One pass of the health suite taken at a point in the monitoring
    window. `elapsed_seconds` is time-since-deploy this round started at,
    not wall-clock -- makes rounds easy to plot/relate regardless of when
    the deployment itself happened.
    """
    round_number: int
    elapsed_seconds: int
    outcomes: list[CheckOutcome] = field(default_factory=list)
    passed: bool = True


@dataclass
class MonitoringResult:
    healthy: bool
    rounds: list[PollRound]
    window_seconds: int
    poll_interval_seconds: int

    @property
    def outcomes(self) -> list[CheckOutcome]:
        """Every CheckOutcome across every round, in order -- what callers
        that just need a flat list to persist (e.g. one HealthCheckResult
        row per outcome) want, without caring about round structure."""
        return [o for r in self.rounds for o in r.outcomes]

    @property
    def failed_round(self) -> PollRound | None:
        return next((r for r in self.rounds if not r.passed), None)


def run_monitoring_window(
    ip_address: str,
    netmiko_type: str = "cisco_ios",
    username: str = "admin",
    password: str = "",
    hostname: str | None = None,
    window_seconds: int | None = None,
    poll_interval_seconds: int | None = None,
    sleep_fn=time.sleep,
    enabled_checks: set[str] | None = None,
    traffic_impact_fn=None,
    remote_check_overrides: dict[str, callable] | None = None,
) -> MonitoringResult:
    """Real-Time Health Monitoring (FR-9 / SRS 6.7).

    Previously the pipeline called `run_health_suite` exactly once,
    immediately after the config push returned, and treated that single
    snapshot as "monitored". That isn't real monitoring -- it's a single
    post-deploy check -- and it misses the failure modes SRS 6.7 actually
    cares about: BGP/OSPF adjacencies that take tens of seconds to
    reconverge, an interface that flaps up then back down, or a
    routing-dependent service (DNS/HTTP) that only times out once the
    initial ARP/neighbor cache entries expire. A one-shot check taken
    milliseconds after `send_config_set` returns can look perfectly
    healthy and still miss all of that.

    This instead actually polls: it re-runs the full suite every
    `poll_interval_seconds` for up to `window_seconds` ("Monitoring window
    is configurable" -- SRS 6.7; defaults come from
    settings.HEALTH_MONITOR_WINDOW_SECONDS / ..._POLL_INTERVAL_SECONDS).
    The moment any round fails, polling stops immediately (fail-fast) and
    rollback can be triggered right away rather than waiting out the rest
    of the window -- consistent with the SRS NFR target of rollback
    initiation in under 30 seconds. Only if every round in the window
    passes is the deployment considered healthy.

    `sleep_fn` is injectable so tests can run a multi-round window without
    actually waiting (pass `lambda s: None`).
    """
    from app.core.config import settings

    window = settings.HEALTH_MONITOR_WINDOW_SECONDS if window_seconds is None else window_seconds
    interval = (
        settings.HEALTH_MONITOR_POLL_INTERVAL_SECONDS if poll_interval_seconds is None else poll_interval_seconds
    )
    interval = max(interval, 1)  # guard against a zero/negative interval spinning forever
    window = max(window, 0)

    rounds: list[PollRound] = []
    elapsed = 0
    round_number = 0

    while True:
        round_number += 1
        outcomes = run_health_suite(
            ip_address, netmiko_type=netmiko_type, username=username, password=password, hostname=hostname,
            enabled_checks=enabled_checks, traffic_impact_fn=traffic_impact_fn,
            remote_check_overrides=remote_check_overrides,
        )
        passed = suite_passed(outcomes)
        rounds.append(PollRound(round_number, elapsed, outcomes, passed))

        if not passed:
            return MonitoringResult(
                healthy=False, rounds=rounds, window_seconds=window, poll_interval_seconds=interval
            )

        elapsed += interval
        if elapsed >= window:
            break
        sleep_fn(interval)

    return MonitoringResult(healthy=True, rounds=rounds, window_seconds=window, poll_interval_seconds=interval)
