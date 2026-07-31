"""Zero-Touch Deployment Engine.

Thin wrapper around Netmiko/NAPALM so the rest of the app depends on a
small, stable interface. Swap out `_connect` internals per-vendor as needed.
"""
import time
from dataclasses import dataclass, field


@dataclass
class DeployResult:
    success: bool
    output: str
    error: str | None = None
    attempts: int = 1
    attempt_errors: list[str] = field(default_factory=list)


# Transient failure signatures worth retrying (network blips, device busy,
# SSH banner timing) as opposed to auth failures or bad config, which won't
# succeed no matter how many times we retry them.
_RETRYABLE_ERROR_SUBSTRINGS = (
    "timed out",
    "timeout",
    "connection refused",
    "connection reset",
    "socket is closed",
    "no route to host",
    "unable to connect",
    "eof",
)


def _is_retryable(error: str | None) -> bool:
    if not error:
        return False
    lowered = error.lower()
    return any(sig in lowered for sig in _RETRYABLE_ERROR_SUBSTRINGS)


def _attempt(
    device_type: str,
    ip_address: str,
    username: str,
    password: str,
    commands: list[str],
) -> DeployResult:
    try:
        from netmiko import ConnectHandler  # imported lazily to keep import cost low

        device = {
            "device_type": device_type,  # e.g. "cisco_ios", "juniper_junos", "arista_eos"
            "host": ip_address,
            "username": username,
            "password": password,
        }
        with ConnectHandler(**device) as conn:
            output = conn.send_config_set(commands)
            conn.save_config()
        return DeployResult(success=True, output=output)
    except Exception as exc:  # noqa: BLE001 - surface all failures to the caller/rollback engine
        return DeployResult(success=False, output="", error=str(exc))


def _deploy_with_retry(
    hostname: str,
    ip_address: str,
    device_type: str,
    username: str,
    password: str,
    commands: list[str],
    max_attempts: int = 3,
    backoff_seconds: float = 2.0,
) -> DeployResult:
    """Runs `_attempt` up to `max_attempts` times, retrying only on
    transient/connectivity errors with exponential backoff between tries.
    A non-retryable failure (auth error, bad config, etc) returns
    immediately on the first attempt so we don't waste time hammering a
    device with credentials that will never work.
    """
    attempt_errors: list[str] = []

    for attempt_num in range(1, max_attempts + 1):
        result = _attempt(device_type, ip_address, username, password, commands)
        result.attempts = attempt_num

        if result.success:
            result.attempt_errors = attempt_errors
            return result

        attempt_errors.append(f"attempt {attempt_num}: {result.error}")

        is_last_attempt = attempt_num == max_attempts
        if is_last_attempt or not _is_retryable(result.error):
            result.attempt_errors = attempt_errors
            if len(attempt_errors) > 1:
                result.error = (
                    f"Failed after {attempt_num} attempt(s) on {hostname} ({ip_address}). "
                    f"Last error: {result.error}"
                )
            return result

        time.sleep(backoff_seconds * (2 ** (attempt_num - 1)))  # 2s, 4s, 8s, ...

    return DeployResult(success=False, output="", error="Unknown deployment failure", attempt_errors=attempt_errors)


def deploy_config(
    hostname: str,
    ip_address: str,
    device_type: str,
    username: str,
    password: str,
    config_commands: list[str],
    max_attempts: int = 3,
) -> DeployResult:
    """Deploy a list of configuration commands to a device via Netmiko,
    retrying transient connection failures before declaring failure so a
    momentary network blip doesn't trigger an unnecessary rollback.

    Kept synchronous and side-effect isolated so it can be called from a
    Celery task for real deployments, or mocked in tests.
    """
    return _deploy_with_retry(hostname, ip_address, device_type, username, password, config_commands, max_attempts)


def rollback_config(
    hostname: str,
    ip_address: str,
    device_type: str,
    username: str,
    password: str,
    restore_commands: list[str],
    max_attempts: int = 3,
) -> DeployResult:
    """Restore a previous configuration. Same retry mechanics as
    deploy_config -- a rollback that fails due to a transient blip should
    also get a couple of tries before we declare it FAILED and page someone.
    """
    return _deploy_with_retry(hostname, ip_address, device_type, username, password, restore_commands, max_attempts)
