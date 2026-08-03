"""NETCONF configuration service (ncclient).

Implements the standard safe config-push sequence:

    lock -> edit-config -> validate -> commit -> unlock
    (rollback + unlock on any failure)

Kept as a small, dependency-isolated module (ncclient imported lazily,
same convention as netmiko/napalm in deployment_engine.py) so the rest of
the app depends on the `NetconfResult` dataclass, not on ncclient's API
directly.
"""
import time
from dataclasses import dataclass


@dataclass
class NetconfResult:
    success: bool
    request_xml: str
    response_xml: str
    execution_time_ms: float
    error: str | None = None


# Maps app.models.device.DeviceVendor values to the ncclient device_params
# "name" that selects its vendor-specific NETCONF handler (XML quirks,
# RPC dialect differences, etc. -- ncclient ships ~9 of these; "default"
# is the plain-IETF handler that plenty of platforms don't actually need).
# Cisco is IOS-XE by default here since that's the mainstream NETCONF-
# capable Cisco platform (classic IOS/IOS-XR don't speak NETCONF the same
# way); Arista EOS's NETCONF support is close enough to the plain IETF
# model that ncclient has no dedicated handler for it, so it stays on
# "default" -- same as Linux, which isn't a NETCONF target at all.
_VENDOR_DEVICE_PARAMS = {
    "cisco": "iosxe",
    "juniper": "junos",
    "arista": "default",
    "linux": "default",
}


def _device_params_for_vendor(vendor: str | None) -> dict:
    name = _VENDOR_DEVICE_PARAMS.get((vendor or "").lower(), "default")
    return {"name": name}


def _connect(
    ip_address: str,
    port: int,
    username: str,
    password: str,
    device_params: dict | None = None,
    vendor: str | None = None,
):
    from ncclient import manager

    return manager.connect(
        host=ip_address,
        port=port or 830,
        username=username,
        password=password,
        hostkey_verify=False,
        device_params=device_params or _device_params_for_vendor(vendor),
        timeout=30,
    )


def get_config(
    ip_address: str,
    port: int,
    username: str,
    password: str,
    source: str = "running",
    vendor: str | None = None,
) -> NetconfResult:
    """<get-config> for a datastore (running/candidate/startup). Used to
    pull the current config before a Digital Twin simulation or drift
    comparison.
    """
    start = time.perf_counter()
    request_xml = f'<get-config><source><{source}/></source></get-config>'
    try:
        with _connect(ip_address, port, username, password, vendor=vendor) as conn:
            reply = conn.get_config(source=source)
            elapsed = (time.perf_counter() - start) * 1000
            return NetconfResult(True, request_xml, str(reply), elapsed)
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.perf_counter() - start) * 1000
        return NetconfResult(False, request_xml, "", elapsed, error=str(exc))


def push_config(
    ip_address: str,
    port: int,
    username: str,
    password: str,
    config_xml: str,
    target: str = "candidate",
    vendor: str | None = None,
    use_lock: bool = True,
) -> NetconfResult:
    """Lock -> edit-config -> validate -> commit -> unlock, in order.

    Any failure (lock contention, invalid config, failed validation,
    commit rejection) triggers a `discard-changes` + `unlock` best-effort
    cleanup before returning success=False, so a failed push never leaves
    the candidate datastore locked or dirty for the next caller.

    `use_lock` (per-device Device.netconf_use_lock) skips the <lock>/
    <unlock> calls entirely -- an escape hatch for agents that either
    don't implement <lock> or reject it outright, which would otherwise
    fail every push against that device at the lock step even though
    edit-config itself would have gone through fine.
    """
    start = time.perf_counter()
    request_xml = (
        f'<edit-config><target><{target}/></target>'
        f'<config>{config_xml}</config></edit-config>'
    )
    responses: list[str] = []
    try:
        with _connect(ip_address, port, username, password, vendor=vendor) as conn:
            if use_lock:
                conn.lock(target=target)
            try:
                edit_reply = conn.edit_config(target=target, config=config_xml)
                responses.append(str(edit_reply))

                if conn.server_capabilities and any("validate" in c for c in conn.server_capabilities):
                    validate_reply = conn.validate(source=target)
                    responses.append(str(validate_reply))

                if target == "candidate":
                    commit_reply = conn.commit()
                    responses.append(str(commit_reply))

                elapsed = (time.perf_counter() - start) * 1000
                return NetconfResult(True, request_xml, "\n".join(responses), elapsed)
            except Exception as inner_exc:  # noqa: BLE001
                try:
                    if target == "candidate":
                        conn.discard_changes()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
                raise inner_exc
            finally:
                if use_lock:
                    try:
                        conn.unlock(target=target)
                    except Exception:  # noqa: BLE001 - best-effort cleanup
                        pass
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.perf_counter() - start) * 1000
        return NetconfResult(False, request_xml, "\n".join(responses), elapsed, error=str(exc))


def discover_capabilities(
    ip_address: str, port: int, username: str, password: str, vendor: str | None = None
) -> list[str] | None:
    """Returns the server's advertised NETCONF <hello> capabilities, or
    None if the device couldn't be reached over NETCONF at all -- used to
    populate Device.capabilities and confirm supports_netconf.
    """
    try:
        with _connect(ip_address, port, username, password, vendor=vendor) as conn:
            return list(conn.server_capabilities)
    except Exception:  # noqa: BLE001
        return None