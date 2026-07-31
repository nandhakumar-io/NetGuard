"""Real-Time Health Monitoring.

Runs a configurable set of post-deployment checks across four categories:
infrastructure, routing, services, and traffic. Returns a flat list of
results that can be persisted as HealthCheckResult rows.
"""
import subprocess
from dataclasses import dataclass


@dataclass
class CheckOutcome:
    category: str
    check_name: str
    passed: bool
    detail: str


def check_ping(ip_address: str, count: int = 2, timeout_sec: int = 2) -> CheckOutcome:
    try:
        result = subprocess.run(
            ["ping", "-c", str(count), "-W", str(timeout_sec), ip_address],
            capture_output=True,
            text=True,
            timeout=timeout_sec * count + 3,
        )
        ok = result.returncode == 0
        return CheckOutcome("infrastructure", "ping", ok, result.stdout[-300:] if ok else result.stderr[-300:])
    except Exception as exc:  # noqa: BLE001
        return CheckOutcome("infrastructure", "ping", False, str(exc))


def run_health_suite(ip_address: str) -> list[CheckOutcome]:
    """Runs the default post-deployment health suite.

    Additional checks (SSH reachability, BGP/OSPF adjacency, DNS/DHCP/HTTP/VPN,
    packet loss & latency) should be added here as device-specific integrations
    (e.g. via NAPALM getters) are implemented.
    """
    results: list[CheckOutcome] = [check_ping(ip_address)]
    return results


def suite_passed(results: list[CheckOutcome]) -> bool:
    return all(r.passed for r in results)
