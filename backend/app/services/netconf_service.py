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
from xml.sax.saxutils import escape as _xml_escape

from app.services.config_format_service import (
    cli_to_netconf_config,
    looks_like_xml,
    pretty_xml,
    strip_junos_readonly_attrs,
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
    from ncclient.transport.errors import SSHError

    from app.core.config import settings

    resolved_device_params = device_params or _device_params_for_vendor(vendor)
    attempts = max(1, settings.NETCONF_CONNECT_RETRIES + 1)
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            conn = manager.connect(
                host=ip_address,
                port=port or 830,
                username=username,
                password=password,
                hostkey_verify=False,
                device_params=resolved_device_params,
                # Was a hardcoded 30s -- see NETCONF_CONNECT_TIMEOUT_SECONDS in
                # app.core.config for why that's too slow for a page-load-time
                # fetch across a fleet with more than a couple of NETCONF
                # devices.
                timeout=settings.NETCONF_CONNECT_TIMEOUT_SECONDS,
            )
            # ncclient reuses the `timeout` kwarg above as the default reply
            # timeout for every RPC on this session too, not just the SSH
            # handshake (ncclient.manager.Manager.timeout). Once the session is
            # actually up, widen that to NETCONF_OPERATION_TIMEOUT_SECONDS so a
            # legitimately slow-but-healthy RPC (e.g. Junos
            # <get-interface-information> on a many-port EX3400) has room to
            # finish instead of being cut off at the same 10s budget meant for
            # detecting an unreachable device. A still-unreachable device fails at
            # connect() above, before this line ever runs, so this can't make an
            # actually-down device hang longer.
            conn.timeout = settings.NETCONF_OPERATION_TIMEOUT_SECONDS
            return conn
        except (SSHError, OSError, TimeoutError) as exc:
            last_exc = exc
            is_last_attempt = attempt == attempts - 1
            # Only retry the specific "never got a TCP/SSH handshake at
            # all" failure modes (raw socket connect timeout/refused, or
            # ncclient's own "Could not open socket" -- see
            # ncclient.transport.ssh.SSHSession.connect) -- an auth
            # failure or a malformed-response error is a real,
            # non-transient problem that retrying won't fix, so those
            # still raise on the first attempt.
            if is_last_attempt:
                if isinstance(exc, SSHError) and "could not open socket" in str(exc).lower():
                    raise SSHError(
                        f"{exc} -- device did not accept a NETCONF/SSH session on port "
                        f"{port or 830} after {attempts} attempt(s). If NETCONF is "
                        "already enabled (`set system services netconf ssh`), check "
                        "for a loopback (lo0) firewall filter blocking this host, or "
                        "that the switch's management plane isn't overloaded."
                    ) from exc
                raise
            # Exponential backoff (delay * 2**attempt: 2s, 4s, 8s, ...)
            # instead of a fixed interval, so a switch that's still busy
            # after the first retry gets progressively more room instead
            # of being polled again at the same fixed cadence it just
            # missed.
            backoff = settings.NETCONF_CONNECT_RETRY_DELAY_SECONDS * (2 ** attempt)
            logger.info(
                "NETCONF connect to %s:%s failed on attempt %d/%d (%s), retrying in %.1fs",
                ip_address, port or 830, attempt + 1, attempts, exc, backoff,
            )
            time.sleep(backoff)
    # Unreachable in practice (the loop always returns or raises), but
    # keeps type-checkers happy and fails loudly instead of returning
    # None if the retry-count math above is ever changed incorrectly.
    raise last_exc or RuntimeError("NETCONF connect failed with no recorded exception")


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
            try:
                reply = conn.get_interface_information()
            except Exception as exc:  # noqa: BLE001
                # Full <get-interface-information/> asks Junos to render
                # every physical+logical interface's complete detail
                # block (queue stats, media diagnostics, per-VLAN
                # logical-interface data, ...). On a densely-populated
                # many-port switch -- EX3400 in the field is the
                # recurring case -- that response can still blow the RPC
                # budget (or, on some EX3400 firmware, trip an internal
                # mgd rendering limit and reset the NETCONF session
                # outright) even with the widened
                # NETCONF_OPERATION_TIMEOUT_SECONDS above, while the same
                # box answers <get-config> for the Configuration tab just
                # fine because that RPC only ever reads the (much
                # smaller) provisioned config, not per-interface
                # operational detail for every port. This app only ever
                # parses name/admin-status/oper-status/speed/mac out of
                # the reply (see config_format_service's junos-opstate
                # parser) -- exactly what Junos's lighter `terse` detail
                # level already contains -- so retry once with
                # <terse/> before giving up. terse is a small fraction of
                # the size/render cost of the full reply and is the
                # standard "just tell me up/down" mode operators already
                # reach for by hand on a big switch (`show interfaces
                # terse`).
                logger.info(
                    "Full get-interface-information failed for %s, retrying with terse: %s",
                    ip_address, exc,
                )
                reply = conn.get_interface_information(terse=True)
            elapsed = (time.perf_counter() - start) * 1000
            content = strip_rpc_envelope(str(reply)) or str(reply)
            return NetconfResult(True, request_xml, _pretty_xml(content), elapsed)
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.perf_counter() - start) * 1000
        return NetconfResult(False, request_xml, "", elapsed, error=str(exc))


def get_junos_switchport_config(
    ip_address: str,
    port: int,
    username: str,
    password: str,
) -> NetconfResult:
    """Junos-specific switchport (port mode / VLAN / RSTP edge) config via
    a filtered <get-config>, used to populate the Interfaces tab's Port
    Mode / VLAN / Edge Port columns for Juniper devices.

    Why this exists: those columns were previously filled in only via
    SNMP (snmp_service.walk_switchport_vlans / walk_stp_edge_ports),
    which (a) requires SNMP to be configured on the device at all, (b)
    depends on the SNMP-reported ifDescr lining up exactly with the
    NETCONF-reported interface name to join the two data sets, and (c)
    for edge-port specifically, only ever works on Cisco -- it walks
    CISCO-STP-EXTENSIONS-MIB, which Juniper doesn't implement, so
    edge_port was unconditionally empty for every Juniper device
    regardless of SNMP config. Since NetGuard already talks NETCONF to
    every Juniper device to get everything else on this tab, pulling
    port-mode/VLAN/edge straight from the device's own configuration
    sidesteps all three problems for Juniper specifically.

    Uses a subtree filter scoped to just `interfaces` and `protocols
    rstp` -- enough to answer port-mode/vlan/edge, without pulling (and
    parsing) the entire running config just for this.
    """
    start = time.perf_counter()
    # NOTE: this is the *criteria* only -- ncclient's get_config(filter=...)
    # wraps whatever's passed here in its own <filter type="subtree">...
    # </filter> element. Passing an already-wrapped <filter> (as this used
    # to) made ncclient nest a second <filter> around it, which every real
    # Junos device rejects as a malformed subtree filter -- so this call
    # always raised and Port Mode/VLAN/Edge Port silently fell through to
    # SNMP (or stayed blank for devices with no SNMP configured), even
    # though the NETCONF session itself was working fine for everything
    # else on the Interfaces tab.
    filter_criteria = (
        "<configuration>"
        "<interfaces/>"
        "<protocols><rstp/></protocols>"
        "</configuration>"
    )
    request_xml = (
        f"<get-config><source><running/></source>"
        f'<filter type="subtree">{filter_criteria}</filter></get-config>'
    )
    try:
        with _connect(ip_address, port, username, password, vendor="juniper") as conn:
            reply = conn.get_config(source="running", filter=("subtree", filter_criteria))
            elapsed = (time.perf_counter() - start) * 1000
            content = strip_rpc_envelope(str(reply)) or str(reply)
            return NetconfResult(True, request_xml, content, elapsed)
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
    # True when this push goes over Junos's <load-configuration
    # action="set"> RPC instead of the generic <edit-config> path -- see
    # _push_once_junos_set below. Kept separate from used_cli_config_data
    # (which gates the Cisco-specific cli-config-data capability check
    # further down) since the two paths have nothing in common besides
    # both starting from plain-text input.
    junos_set_push = False
    if not looks_like_xml(config_xml):
        vendor_lower = (vendor or "cisco").lower()
        if vendor_lower == "cisco":
            config_xml = cli_to_netconf_config(config_xml)
            used_cli_config_data = True
        elif vendor_lower == "juniper":
            # Junos takes plain `set ...` configuration-mode commands
            # directly via <load-configuration action="set">, no XML
            # wrapping needed -- config_xml is left as raw text and
            # pushed via _push_once_junos_set below instead of
            # edit-config. IMPORTANT: this assumes config_xml is already
            # Junos `set` syntax (e.g. "set interfaces ge-0/0/1 disable"),
            # NOT the IOS-style CLI text ("interface Gi0/1" / "shutdown")
            # that validation_engine/risk_engine currently author every
            # change request in. Translating IOS syntax to Junos `set`
            # syntax is a separate, unimplemented step -- callers
            # targeting Juniper devices must supply Junos `set` commands
            # in proposed_config today, or this will fail at
            # load-configuration with a Junos parse error rather than
            # silently doing the wrong thing.
            junos_set_push = True
        else:
            elapsed = (time.perf_counter() - start) * 1000
            return NetconfResult(
                False, config_xml, "", elapsed,
                error=(
                    f"proposed_config is plain CLI text, not XML, and vendor="
                    f"'{vendor}' has no CLI-to-NETCONF translation configured "
                    "(only Cisco IOS-XE's cli-config-data extension and "
                    "Junos 'set' commands are supported today) -- NETCONF "
                    "push needs an XML <config> payload for this vendor."
                ),
            )
    else:
        stripped = strip_rpc_envelope(config_xml)
        if stripped is not None and stripped != config_xml:
            config_xml = stripped

        # Junos-specific: a <configuration> payload straight out of
        # <get-config> (e.g. a restore/rollback pushing a prior snapshot's
        # running-config text back to the device) still carries Junos's
        # read-only junos:changed-*/commit-* attributes at this point --
        # strip_rpc_envelope only removed the outer rpc-reply/data
        # envelope, not these. Left in place, Junos's edit-config schema
        # validator rejects the whole payload (see
        # strip_junos_readonly_attrs's docstring for the exact error).
        # No-op for anything that isn't a Junos <configuration> document.
        if (vendor or "").lower() == "juniper":
            config_xml = strip_junos_readonly_attrs(config_xml) or config_xml
            # Junos's edit-config schema validator requires the payload's
            # top-level element to be exactly <configuration> -- any XML
            # fragment authored/rendered without that wrapper (e.g. a
            # GitOps/config-template payload that's just
            # "<interfaces>...</interfaces>", or one with *several*
            # sibling top-level elements like
            # "<interfaces>...</interfaces><vlans>...</vlans>") gets
            # rejected with:
            #   Element [{http://xml.juniper.net/xnm/1.1/xnm}configuration]
            #   does not meet requirement
            # which is the exact same error strip_junos_readonly_attrs's
            # docstring documents for the *attribute* case, but this is
            # the *missing element* case: Junos names "configuration" as
            # the element that didn't meet its requirement because that's
            # the element it expected at the top and didn't get. Wrap
            # rather than reject so any well-formed Junos config fragment
            # deploys correctly regardless of whether the caller
            # remembered the outer tag.
            try:
                from lxml import etree as _lxml_etree
            except ImportError:
                _lxml_etree = None
            if _lxml_etree is not None:
                try:
                    root = _lxml_etree.fromstring(config_xml.encode("utf-8"))
                    root_local = root.tag.rsplit("}", 1)[-1] if root.tag.startswith("{") else root.tag
                    if root_local != "configuration":
                        config_xml = f"<configuration>{config_xml}</configuration>"
                except _lxml_etree.XMLSyntaxError:
                    # A single well-formed element parses fine above and
                    # only needs wrapping when its tag isn't
                    # "configuration". This branch is for input that isn't
                    # parseable *as a standalone document at all* -- most
                    # commonly several sibling top-level elements
                    # ("<a/><b/>") with no single enclosing root, which is
                    # a well-formed *fragment* but not a well-formed
                    # *document*, so fromstring raises here instead of
                    # returning a root to inspect. Wrapping fixes exactly
                    # this case (the wrapped result *is* a valid document),
                    # so do it unconditionally rather than treating the
                    # parse failure as "leave it alone, edit_config will
                    # explain it" -- that fallback is for genuinely
                    # malformed XML, and silently applied here it was
                    # actually the majority cause of this exact Junos
                    # schema-validation failure in practice: multi-element
                    # fragments, not missing-wrapper single elements.
                    config_xml = f"<configuration>{config_xml}</configuration>"
                except Exception:
                    # Not parseable as XML for some other reason (e.g.
                    # binary/garbage input) -- leave as-is, edit_config
                    # will surface its own (unrelated) parser error.
                    pass

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

    def _push_once_junos_set(conn, push_target: str, caps) -> tuple[str, list[str]]:
        """Junos equivalent of _push_once above: lock -> load-configuration
        (action="set") -> validate -> commit -> unlock. Junos's
        <load-configuration action="set"> RPC takes raw `set` command
        text directly and loads it into the candidate database in one
        step -- there's no separate edit-config call the way there is
        for XML-shaped config, so this is a distinct function rather
        than a branch inside _push_once.
        """
        responses: list[str] = []
        request_xml = (
            f'<load-configuration action="set">'
            f'<configuration-set>{_xml_escape(config_xml)}</configuration-set>'
            f'</load-configuration>'
        )
        if use_lock:
            conn.lock(target=push_target)
        try:
            load_reply = conn.load_configuration(action="set", config=config_xml)
            responses.append(_pretty_xml(str(load_reply)))

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

    if junos_set_push:
        request_xml = (
            f'<load-configuration action="set">'
            f'<configuration-set>{_xml_escape(config_xml)}</configuration-set>'
            f'</load-configuration>'
        )
    else:
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

            push_once = _push_once_junos_set if junos_set_push else _push_once
            try:
                request_xml, responses = push_once(conn, effective_target, caps)
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
                request_xml, responses = push_once(conn, "running", caps)
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
