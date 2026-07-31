"""One-time console bootstrap for GNS3 lab nodes.

A freshly-created GNS3 node (IOSv, vIOS-L2, ...) boots with no management
IP and no SSH -- there's nothing for Netmiko/NAPALM to connect to yet. This
module is the one piece of the GNS3 integration that talks to a node over
its *console* (telnet, via the GNS3 controller's console port) instead of
its management plane, purely to push the handful of commands that give it
a reachable IP and enable SSH. After this runs successfully, the device is
indistinguishable from a physical one to the rest of the app -- every
later deploy/validate/rollback goes over normal SSH to `ip_address`,
through deployment_engine/protocol_manager exactly as for real hardware.

Only Cisco IOS is implemented (the overwhelmingly common GNS3 lab
appliance). Other vendors can be bootstrapped manually over their GNS3
console (`gns3_service.get_node` returns the console host/port for that)
and then added to inventory as an ordinary Device once reachable.
"""
import time
from dataclasses import dataclass


@dataclass
class BootstrapResult:
    success: bool
    output: str
    error: str | None = None


def bootstrap_cisco_ios(
    console_host: str,
    console_port: int,
    mgmt_interface: str,
    mgmt_ip: str,
    mgmt_subnet_mask: str,
    ssh_username: str,
    ssh_password: str,
    enable_password: str | None = None,
    hostname: str | None = None,
    ready_timeout_seconds: float = 45.0,
) -> BootstrapResult:
    """Connects to a bare Cisco IOS/IOSv node's console over telnet and
    pushes just enough config to make it SSH-reachable at `mgmt_ip`:

      - a hostname (optional, cosmetic)
      - an IP on `mgmt_interface`, brought up with `no shutdown`
      - a local user + `line vty` SSH access, RSA keypair for SSH itself

    Idempotent-ish: re-running against a node that already has these
    commands applied just re-applies the same config, which IOS accepts
    without error.
    """
    try:
        from netmiko import ConnectHandler
        from netmiko.exceptions import NetmikoTimeoutError, NetmikoAuthenticationException
    except ImportError as exc:  # pragma: no cover - dependency always present, defensive only
        return BootstrapResult(success=False, output="", error=f"netmiko not available: {exc}")

    device = {
        "device_type": "cisco_ios_telnet",
        "host": console_host,
        "port": console_port,
        # A bare node's console has no login prompt yet, so there's
        # nothing to authenticate with at this stage -- Netmiko is told
        # not to expect one.
        "username": "",
        "password": "",
        "timeout": ready_timeout_seconds,
    }

    commands = []
    if hostname:
        commands.append(f"hostname {hostname}")
    commands += [
        "no ip domain-lookup",
        f"interface {mgmt_interface}",
        f"ip address {mgmt_ip} {mgmt_subnet_mask}",
        "no shutdown",
        "exit",
        "ip domain-name netguard.lab",
        "crypto key generate rsa modulus 2048",
        f"username {ssh_username} privilege 15 secret {ssh_password}",
        "line vty 0 4",
        "login local",
        "transport input ssh",
        "exit",
    ]
    if enable_password:
        commands.append(f"enable secret {enable_password}")

    deadline = time.monotonic() + ready_timeout_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            with ConnectHandler(**device) as conn:
                conn.enable()
                output = conn.send_config_set(commands)
                conn.save_config()
                return BootstrapResult(success=True, output=output)
        except (NetmikoTimeoutError, NetmikoAuthenticationException, OSError) as exc:
            # Node likely still booting / console not accepting connections
            # yet -- retry until ready_timeout_seconds elapses rather than
            # failing on the first attempt.
            last_error = str(exc)
            time.sleep(3)
        except Exception as exc:  # noqa: BLE001 - surface all other failures immediately
            return BootstrapResult(success=False, output="", error=str(exc))

    return BootstrapResult(
        success=False, output="",
        error=f"Console at {console_host}:{console_port} did not become ready within "
              f"{ready_timeout_seconds}s. Last error: {last_error}",
    )