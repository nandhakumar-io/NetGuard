"""NETCONF configuration service (ncclient).

Implements the standard safe config-push sequence:

    lock -> edit-config -> validate -> commit -> unlock
    (rollback + unlock on any failure)

Kept as a small, dependency-isolated module (ncclient imported lazily,
same convention as netmiko/napalm in deployment_engine.py) so the rest of
the app depends on the `NetconfResult` dataclass, not on ncclient's API
directly.
"""
import logging
import time
from dataclasses import dataclass

from app.services.config_format_service import (
    cli_to_netconf_config,
    looks_like_xml,
    pretty_xml,
    strip_rpc_envelope,
)

logger = logging.getLogger("netguard.netconf")


def _pretty_xml(xml_text: str) -> str:
    """Thin wrapper around config_format_service.pretty_xml that falls
    back to the original text (instead of None) when it isn't parseable
    XML, so every call site here can use it unconditionally.

    ncclient's `str(reply)` gives back the exact bytes the device sent,
    which for most NETCONF servers (including the Cisco IOS-XE devices
    this app targets) is a single unbroken line with no whitespace
    between elements. Every caller that stores this text downstream --
    ConfigSnapshot/GoldenConfig backups, drift baselines, the live-config
    read used for drift comparison -- ends up treating "the whole config"
    as one diff_engine.generate_diff() line, which has two bad effects
    that show up together on the Drift page:

      1. The AI/rule-based drift summary becomes unreadable: instead of
         "Interface GigabitEthernet0/3 was administratively shut down" it
         shows the *entire* multi-KB XML blob as a single "Removed:"/
         "Added:" line.
      2. Keyword rules (e.g. the `shutdown` risk rule) scan that entire
         blob as "changed", so a `<shutdown/>` element on some unrelated,
         already-shut interface that hasn't changed at all still fires a
         false-positive "Interface shutdown detected" finding on every
         scan, just because it happens to exist somewhere in a config
         that differs from baseline for a completely different reason.

    Pretty-printing so each element lands on its own line fixes both:
    line-level diffing then only flags lines that actually changed, and
    the resulting diff/summary is something a human can actually read.
    """
    return pretty_xml(xml_text) or xml_text


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


def get_junos_interface_information(
    ip_address: str,
    port: int,
    username: str,
    password: str,
) -> NetconfResult:
    """Junos-specific operational interface state via the vendor RPC
    <get-interface-information>, NOT <get-config>.

    Why this exists: get_interfaces() used to call plain get_config(source
    ="running") for every vendor, then hand the result to
    config_format_service.parse_interfaces(). That's correct-ish for
    Cisco IOS-XE (its config-get response happens to carry admin state,
    and the generic ietf-interfaces parser can find enough to show
    *something*), but it's fundamentally wrong for Junos: <get-config>
    only ever returns *configuration* (what's provisioned), never
    *operational* state (what's actually up/down right now) -- Junos
    keeps those completely separate, unlike some IOS-XE YANG models. So
    every real Juniper device came back with admin_status/oper_status
    both None ("Unknown" in the UI) no matter what was actually
    configured, because the field the parser was looking for
    (oper-status) simply doesn't exist anywhere in a Junos config-get
    response -- it's not a parsing bug, it's asking the wrong RPC.

    ncclient's junos device_params handler exposes vendor ops as normal
    method calls (conn.get_interface_information(...)) instead of raw
    dispatch, which is what makes this a few lines instead of hand-built
    RPC XML.
    """
    start = time.perf_counter()
    request_xml = "<get-interface-information/>"
    try:
        with _connect(ip_address, port, username, password, vendor="juniper") as conn:
            reply = conn.get_interface_information()
            elapsed = (time.perf_counter() - start) * 1000
            content = strip_rpc_envelope(str(reply)) or str(reply)
            return NetconfResult(True, request_xml, _pretty_xml(content), elapsed)
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.perf_counter() - start) * 1000
        return NetconfResult(False, request_xml, "", elapsed, error=str(exc))


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
            content = strip_rpc_envelope(str(reply)) or str(reply)
            return NetconfResult(True, request_xml, _pretty_xml(content), elapsed)
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

    `target="candidate"` is only a *request*, not a guarantee -- plenty
    of real devices (classic IOS, most lab/virtual images) never
    advertise the :candidate capability in their NETCONF <hello> at all
    and only support editing :running directly. ncclient itself raises
    "Unsupported capability :candidate" the moment `lock`/`edit_config`
    is called with target="candidate" against one of those devices --
    every deploy against them failed at that exact step even though the
    device is perfectly capable of taking the same config against
    :running. Once connected (server_capabilities is only known *after*
    the NETCONF <hello> exchange, so this can't be decided before
    `_connect`), we downgrade target to "running" when :candidate isn't
    advertised, and skip the candidate-only `commit()` step to match --
    an edit-config against :running applies immediately, there's nothing
    to commit.

    That preemptive check isn't quite enough on its own, though: some
    IOS-XE images (several of the GNS3 lab Catalyst 8000v boxes included)
    advertise `:candidate` in their <hello> capabilities but reject an
    actual `<lock>`/`<edit-config>` against it with that exact same
    "Unsupported capability :candidate" error -- i.e. the capability list
    doesn't reliably reflect whether the candidate datastore is actually
    usable on that box. So in addition to the preemptive check, a
    candidate-target attempt that still fails with a capability-shaped
    error is retried once, live, against :running before giving up --
    this is what actually fixes deploys against those boxes; the
    preemptive check above just avoids the round trip for devices that
    are known upfront not to advertise it at all.
    """
    start = time.perf_counter()

    # Every change request in this app is authored, validated
    # (validation_engine), diffed (diff_engine), and risk-scored
    # (risk_engine) as plain IOS-style CLI text -- there's no separate
    # "XML mode" a caller opts into. Handing that straight to ncclient's
    # edit_config() as the NETCONF `config` payload fails immediately
    # with an XML parser error ("Start tag expected, '<' not found") --
    # it was never going to parse as XML, this is a payload-shape
    # mismatch, not a malformed-XML bug. Convert it here, once, right
    # before it goes over the wire, rather than pushing the conversion
    # requirement onto every caller of push_config.
    used_cli_config_data = False
    if not looks_like_xml(config_xml):
        if (vendor or "cisco").lower() != "cisco":
            elapsed = (time.perf_counter() - start) * 1000
            return NetconfResult(
                False, config_xml, "", elapsed,
                error=(
                    f"proposed_config is plain CLI text, not XML, and vendor="
                    f"'{vendor}' has no CLI-to-NETCONF translation configured "
                    "(only Cisco IOS-XE's cli-config-data extension is "
                    "supported today) -- NETCONF push needs an XML <config> "
                    "payload for this vendor."
                ),
            )
        config_xml = cli_to_netconf_config(config_xml)
        used_cli_config_data = True
    else:
        stripped = strip_rpc_envelope(config_xml)
        if stripped is not None and stripped != config_xml:
            config_xml = stripped

        # We do NOT add a <config> envelope here because ncclient's
        # `conn.edit_config(..., config=config_xml)` method AUTOMATICALLY
        # wraps XML element strings in a `<config>` top-level node. Passing
        # an explicit `<config>` tag strings causes double `<config><config>`
        # nesting which the server rejects with bad-element config.

    def _push_once(conn, push_target: str, caps) -> tuple[str, list[str]]:
        """One lock -> edit-config -> validate -> commit -> unlock
        sequence against `push_target`. Returns (request_xml, responses)
        or raises on failure -- callers decide whether a failure here is
        worth retrying against a different target."""
        responses: list[str] = []
        request_xml = f'<edit-config><target><{push_target}/></target>{config_xml}</edit-config>'
        if use_lock:
            conn.lock(target=push_target)
        try:
            edit_reply = conn.edit_config(target=push_target, config=config_xml)
            responses.append(_pretty_xml(str(edit_reply)))

            if any("validate" in c for c in caps):
                validate_reply = conn.validate(source=push_target)
                responses.append(_pretty_xml(str(validate_reply)))

            if push_target == "candidate":
                commit_reply = conn.commit()
                responses.append(_pretty_xml(str(commit_reply)))

            return request_xml, responses
        except Exception:
            try:
                if push_target == "candidate":
                    conn.discard_changes()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
            raise
        finally:
            if use_lock:
                try:
                    conn.unlock(target=push_target)
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass

    request_xml = f'<edit-config><target><{target}/></target>{config_xml}</edit-config>'
    try:
        with _connect(ip_address, port, username, password, vendor=vendor) as conn:
            caps = list(conn.server_capabilities or [])

            if used_cli_config_data and not any("cisco-ia" in c.lower() for c in caps):
                # This device's NETCONF <hello> doesn't advertise Cisco's
                # cli-config-data YANG extension at all -- pushing anyway
                # gets a device-side rpc-error (tag "unknown-element",
                # bad-element "cli-config-data") only after a full
                # lock/edit-config round trip, which reads like a bug in
                # our payload rather than what it actually is: this image/
                # platform doesn't support turning CLI text into NETCONF
                # edits this way. Fail fast with a message that says what
                # to actually do about it, instead of a raw rpc-error dump.
                elapsed = (time.perf_counter() - start) * 1000
                return NetconfResult(
                    False, request_xml, "", elapsed,
                    error=(
                        "This device's NETCONF server doesn't advertise Cisco's "
                        "cli-config-data extension (capability containing "
                        "'cisco-ia'), which is what NETCONF pushes use to apply "
                        "plain CLI text on IOS-XE. That extension isn't present "
                        "on every IOS-XE image/platform -- deploy this change "
                        "over SSH instead (same CLI commands, no translation "
                        "needed), or confirm 'netconf-yang' and the Cisco-IA "
                        "model are enabled on this device."
                    ),
                )

            effective_target = target
            if effective_target == "candidate" and not any(":candidate" in c for c in caps):
                logger.debug(
                    "%s does not advertise :candidate NETCONF capability; editing :running directly",
                    ip_address,
                )
                effective_target = "running"

            try:
                request_xml, responses = _push_once(conn, effective_target, caps)
            except Exception as first_exc:
                is_candidate_capability_error = (
                    effective_target == "candidate" and "candidate" in str(first_exc).lower()
                )
                if not is_candidate_capability_error:
                    raise
                logger.warning(
                    "%s advertised :candidate but rejected it at edit-config time (%s); "
                    "retrying against :running",
                    ip_address, first_exc,
                )
                request_xml, responses = _push_once(conn, "running", caps)
                effective_target = "running"

            elapsed = (time.perf_counter() - start) * 1000
            return NetconfResult(True, request_xml, "\n".join(responses), elapsed)
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.perf_counter() - start) * 1000
        return NetconfResult(False, request_xml, "", elapsed, error=str(exc))


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
