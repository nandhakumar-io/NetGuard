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


NAPALM_DRIVER_MAP = {
    "cisco_ios": "ios",
    "juniper_junos": "junos",
    "arista_eos": "eos",
}


_TELNET_DEVICE_TYPE_MAP = {
    "cisco_ios": "cisco_ios_telnet",
    "arista_eos": "arista_eos_telnet",
    # juniper_junos / linux have no Netmiko telnet variant -- callers get
    # None back for those exactly as if telnet weren't attempted at all.
}


def read_running_config(
    device_type: str,
    ip_address: str,
    username: str,
    password: str,
) -> tuple[str | None, str]:
    """Best-effort live read of a device's current running-config: SSH
    (via NAPALM) first, then falling back to Netmiko-over-Telnet if SSH's
    connection phase fails outright -- refused, timed out, no SSH server
    running at all. This matters most for devices that were never
    configured for SSH (GNS3 lab nodes before their one-time bootstrap,
    older gear managed only over telnet historically).

    Returns (config_text_or_None, protocol_used) where protocol_used is
    "ssh", "telnet", or "none" (nothing reachable) -- callers should
    surface a warning to the user whenever it's "telnet", since that
    session was unencrypted.

    A failed read is not an exception in either branch; callers already
    treat None as "couldn't confirm live state" (see the docstring below
    for why that's safe for the rollback pre-snapshot use case).
    """
    driver_name = NAPALM_DRIVER_MAP.get(device_type)
    if driver_name is not None:
        try:
            import napalm

            driver = napalm.get_network_driver(driver_name)
            device = driver(hostname=ip_address, username=username, password=password, timeout=10)
            device.open()
            try:
                config = device.get_config()
                running = config.get("running") or None
                if running:
                    return running, "ssh"
            finally:
                device.close()
        except Exception:  # noqa: BLE001 - fall through to telnet below
            pass

    telnet_device_type = _TELNET_DEVICE_TYPE_MAP.get(device_type)
    if telnet_device_type is None:
        return None, "none"

    try:
        from netmiko import ConnectHandler

        with ConnectHandler(
            device_type=telnet_device_type, host=ip_address, username=username, password=password, timeout=10,
        ) as conn:
            output = conn.send_command("show running-config")
        return (output or None), "telnet"
    except Exception:  # noqa: BLE001 - best-effort; caller treats None as unreachable
        return None, "none"


def read_startup_config(
    device_type: str,
    ip_address: str,
    username: str,
    password: str,
) -> str | None:
    """Best-effort live read of a device's startup-config via NAPALM's
    get_config() -- which already returns running/startup/candidate in one
    call, so this just asks for the same connection's "startup" key
    instead of "running" (see read_running_config above for the running-
    config equivalent, including a NAPALM->telnet fallback that startup
    doesn't have: `show startup-config` over a bare Netmiko/Telnet session
    is a reasonable fallback for *running* config, but startup-config
    handling varies enough across platforms via raw CLI that it's not
    worth guessing at here -- NAPALM's structured get_config() is the
    reliable path, and this simply returns None if that's unavailable).

    Returns None (not an exception) if the device doesn't support NAPALM
    for its type, or the connection/read fails for any reason -- callers
    already treat a None here as "startup-config wasn't captured this
    time", same as backup_config()'s existing best-effort NETCONF startup
    read.
    """
    driver_name = NAPALM_DRIVER_MAP.get(device_type)
    if driver_name is None:
        return None
    try:
        import napalm

        driver = napalm.get_network_driver(driver_name)
        device = driver(hostname=ip_address, username=username, password=password, timeout=10)
        device.open()
        try:
            config = device.get_config()
            return config.get("startup") or None
        finally:
            device.close()
    except Exception:  # noqa: BLE001 - best-effort, same policy as read_running_config
        return None


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
