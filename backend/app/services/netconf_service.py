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


def _connect(ip_address: str, port: int, username: str, password: str, device_params: dict | None = None):
    from ncclient import manager

    return manager.connect(
        host=ip_address,
        port=port or 830,
        username=username,
        password=password,
        hostkey_verify=False,
        device_params=device_params or {"name": "default"},
        timeout=30,
    )


def get_config(
    ip_address: str,
    port: int,
    username: str,
    password: str,
    source: str = "running",
) -> NetconfResult:
    """<get-config> for a datastore (running/candidate/startup). Used to
    pull the current config before a Digital Twin simulation or drift
    comparison.
    """
    start = time.perf_counter()
    request_xml = f'<get-config><source><{source}/></source></get-config>'
    try:
        with _connect(ip_address, port, username, password) as conn:
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
) -> NetconfResult:
    """Lock -> edit-config -> validate -> commit -> unlock, in order.

    Any failure (lock contention, invalid config, failed validation,
    commit rejection) triggers a `discard-changes` + `unlock` best-effort
    cleanup before returning success=False, so a failed push never leaves
    the candidate datastore locked or dirty for the next caller.
    """
    start = time.perf_counter()
    request_xml = (
        f'<edit-config><target><{target}/></target>'
        f'<config>{config_xml}</config></edit-config>'
    )
    responses: list[str] = []
    try:
        with _connect(ip_address, port, username, password) as conn:
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
                try:
                    conn.unlock(target=target)
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.perf_counter() - start) * 1000
        return NetconfResult(False, request_xml, "\n".join(responses), elapsed, error=str(exc))


def discover_capabilities(ip_address: str, port: int, username: str, password: str) -> list[str] | None:
    """Returns the server's advertised NETCONF <hello> capabilities, or
    None if the device couldn't be reached over NETCONF at all -- used to
    populate Device.capabilities and confirm supports_netconf.
    """
    try:
        with _connect(ip_address, port, username, password) as conn:
            return list(conn.server_capabilities)
    except Exception:  # noqa: BLE001
        return None