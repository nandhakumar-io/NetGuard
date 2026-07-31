"""Zero-Touch Deployment Engine.

Thin wrapper around Netmiko/NAPALM so the rest of the app depends on a
small, stable interface. Swap out `_connect` internals per-vendor as needed.
"""
from dataclasses import dataclass


@dataclass
class DeployResult:
    success: bool
    output: str
    error: str | None = None


def deploy_config(
    hostname: str,
    ip_address: str,
    device_type: str,
    username: str,
    password: str,
    config_commands: list[str],
) -> DeployResult:
    """Deploy a list of configuration commands to a device via Netmiko.

    Kept synchronous and side-effect isolated so it can be called from a
    Celery task for real deployments, or mocked in tests.
    """
    try:
        from netmiko import ConnectHandler  # imported lazily to keep import cost low

        device = {
            "device_type": device_type,  # e.g. "cisco_ios", "juniper_junos", "arista_eos"
            "host": ip_address,
            "username": username,
            "password": password,
        }
        with ConnectHandler(**device) as conn:
            output = conn.send_config_set(config_commands)
            conn.save_config()
        return DeployResult(success=True, output=output)
    except Exception as exc:  # noqa: BLE001 - surface all failures to the caller/rollback engine
        return DeployResult(success=False, output="", error=str(exc))


def rollback_config(
    hostname: str,
    ip_address: str,
    device_type: str,
    username: str,
    password: str,
    restore_commands: list[str],
) -> DeployResult:
    """Restore a previous configuration. Same mechanics as deploy_config,
    kept as a separate function for clarity in the rollback workflow and logs.
    """
    return deploy_config(hostname, ip_address, device_type, username, password, restore_commands)
