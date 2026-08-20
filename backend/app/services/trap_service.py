"""SNMP trap receiver.

Closes the gap the module docstring in snmp_service.py already flagged
("parses inbound SNMP traps... so 'Interface Down' looks the same in
the UI whether it came from a trap or a poll") but nothing ever actually
implemented: until this module, snmp_service.classify_trap() existed
and worked correctly, but nothing on the wire ever called it -- there
was no trap listener, no route, nothing. Every "port down" detection
was going through the scheduled SNMP_POLL_INTERVAL_SECONDS poll only
(60s default, so up to a full minute of lag), even though devices were
almost certainly already configured to *send* traps that nothing was
listening for.

Deliberately built on pysnmp's low-level BER decode API
(pysnmp.proto.api.v1/v2c + pyasn1's decoder), not the higher-level
entity/rfc3413 NotificationReceiver framework -- that framework owns
its own transport dispatch loop (SnmpEngine.transportDispatcher.
runDispatcher(), meant to *be* the process's main loop), which doesn't
compose cleanly with a datagram protocol living inside FastAPI's
already-running asyncio loop. Same reasoning as syslog_service's plain
`loop.create_datagram_endpoint` UDP listener: this decodes each inbound
packet directly, in an asyncio.DatagramProtocol, no second event loop
involved.

Same two-transport-shape and correlation pattern as syslog_service.py:
a real UDP listener (start_trap_listener) is the actual entry point
devices are pointed at (`snmp-server host <ip> version 2c <community>`
or the Junos/EOS equivalent); everything funnels through
ingest_trap(), which raises/auto-resolves an Alert via the exact same
alert_service path health polls and syslog correlation use, so a trap-
driven "Interface Down" is indistinguishable in Alert Center from a
poll-driven one except for its `source` field.
"""
from __future__ import annotations

import asyncio
import logging

from pyasn1.codec.ber import decoder as ber_decoder
from pyasn1.error import PyAsn1Error
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.alert import AlertSource
from app.models.device import Device
from app.models.interface_status import InterfaceStatus
from app.services import alert_service, metrics_service, snmp_service

logger = logging.getLogger("netguard.trap")

SNMP_TRAP_OID_VARBIND = "1.3.6.1.6.3.1.1.4.1.0"  # snmpTrapOID.0 (v2c/v3 only)


class ParsedTrap:
    __slots__ = ("trap_name", "community", "if_index", "raw_varbinds")

    def __init__(self, trap_name: str, community: str | None, if_index: str | None, raw_varbinds: list[tuple[str, str]]):
        self.trap_name = trap_name
        self.community = community
        self.if_index = if_index
        self.raw_varbinds = raw_varbinds


def _accepted_communities() -> set[str]:
    return {c.strip() for c in (settings.SNMP_TRAP_COMMUNITIES or "").split(",") if c.strip()}


def parse_trap_packet(data: bytes) -> ParsedTrap | None:
    """Best-effort decode of one inbound UDP datagram as an SNMP trap.
    Returns None for anything that isn't valid BER/isn't recognizable
    as v1 or v2c/v3 -- a malformed or unrelated packet on this port
    must never crash the listener, same contract as
    syslog_service.parse_syslog_line's "never raises" policy.

    v3 traps (SNMPv2-Trap-PDU wrapped in a ScopedPDU under USM
    encryption/auth) are deliberately not decrypted here -- that needs
    the full USM engine (per-user auth/priv keys), which is exactly the
    heavyweight entity-framework machinery this module avoids. A v3
    trap still gets *detected* (falls through to the "undecodable" path
    below and is logged), it just isn't parsed into an alert. Real
    fleets sending v3 traps specifically are the one case still worth
    revisiting with the full pysnmp entity/USM stack.
    """
    from pysnmp.proto import api as pysnmp_api

    try:
        msg_ver = int(pysnmp_api.decodeMessageVersion(data))
    except Exception:
        return None

    proto_mod = pysnmp_api.protoModules.get(msg_ver)
    if proto_mod is None:
        logger.info("Received SNMP trap with unsupported version %s (likely v3) -- skipping", msg_ver)
        return None

    try:
        rsp_msg, _rest = ber_decoder.decode(data, asn1Spec=proto_mod.Message())
    except (PyAsn1Error, Exception):  # noqa: BLE001 -- any decode failure is just "not a valid trap"
        return None

    try:
        community = str(proto_mod.apiMessage.getCommunity(rsp_msg))
    except Exception:
        community = None

    accepted = _accepted_communities()
    if accepted and community not in accepted:
        logger.info("Rejected SNMP trap with unrecognized community %r", community)
        return None

    try:
        pdu = proto_mod.apiMessage.getPDU(rsp_msg)
    except Exception:
        return None

    raw_varbinds: list[tuple[str, str]] = []
    trap_name: str | None = None
    if_index: str | None = None

    if msg_ver == 0:  # SNMPv1 Trap-PDU -- generic-trap field names the trap directly
        try:
            trap_name = str(proto_mod.apiTrapPDU.getGenericTrap(pdu))
        except Exception:
            trap_name = None
        try:
            # v1's TrapPDU is NOT shaped like a normal request/response PDU
            # (GetRequestPDU etc): variable-bindings sits at ASN.1 SEQUENCE
            # position 5 (after enterprise/agent-addr/generic-trap/
            # specific-trap/timestamp), not position 3 like every other
            # PDU type. apiPDU.getVarBinds() unconditionally reads position
            # 3, which for a TrapPDU is the specific-trap Integer, not a
            # varbind list -- iterating it raised TypeError every time,
            # silently swallowed by the except below, so if_index was
            # *always* None for every v1 trap regardless of what the
            # device actually sent. apiTrapPDU.getVarBinds() is the v1-
            # TrapPDU-shape-aware accessor and reads the right position.
            for oid, val in proto_mod.apiTrapPDU.getVarBinds(pdu):
                oid_str, val_str = str(oid), str(val)
                raw_varbinds.append((oid_str, val_str))
                if oid_str == snmp_service.IF_INDEX_VARBIND_OID:
                    if_index = val_str
        except Exception:
            pass
    else:  # SNMPv2-Trap-PDU (v2c or v3-without-decrypt) -- trap identity is a varbind
        try:
            for oid, val in proto_mod.apiPDU.getVarBinds(pdu):
                oid_str, val_str = str(oid), str(val)
                raw_varbinds.append((oid_str, val_str))
                if oid_str == SNMP_TRAP_OID_VARBIND:
                    trap_name = snmp_service.TRAP_OID_NAMES.get(val_str, val_str)
                elif oid_str == snmp_service.IF_INDEX_VARBIND_OID:
                    if_index = val_str
        except Exception:
            pass

    if not trap_name:
        return None

    return ParsedTrap(trap_name=trap_name, community=community, if_index=if_index, raw_varbinds=raw_varbinds)


def _resolve_if_descr(db: Session, device: Device, if_index: str) -> str:
    """Best-effort ifIndex -> ifDescr for the trap message text, from
    the InterfaceStatus history the periodic poll already maintains --
    a DB lookup, not a live SNMP GET, so handling a trap never adds a
    network round trip back to the device that just paged us (that
    would undercut the entire point of "instant").  Falls back to a
    generic "ifN" label if this ifIndex has never been seen by a poll
    yet (e.g. trap arrived before the device's first poll completed).
    """
    latest = (
        db.query(InterfaceStatus)
        .filter(InterfaceStatus.device_id == device.id, InterfaceStatus.if_index == if_index)
        .order_by(InterfaceStatus.changed_at.desc())
        .first()
    )
    return latest.if_descr if latest is not None else f"if{if_index}"


def ingest_trap(db: Session, *, source_ip: str, parsed: ParsedTrap) -> None:
    """Resolves the sending device, then either routes into the shared
    interface-transition path (linkDown/linkUp -- same alert/notify
    logic a poll uses, see metrics_service.record_interface_transition)
    or raises a generic classified alert for everything else
    (coldStart, authenticationFailure, ...) via
    snmp_service.classify_trap, same category vocabulary as any other
    alert source.
    """
    device = db.query(Device).filter(Device.ip_address == source_ip).first()
    if device is None:
        logger.info("Trap %r from unmanaged source %s -- ignored (no matching device)", parsed.trap_name, source_ip)
        return

    if parsed.trap_name in ("linkDown", "linkUp") and parsed.if_index:
        if_descr = _resolve_if_descr(db, device, parsed.if_index)
        changed = metrics_service.record_interface_transition(
            db,
            device,
            if_index=parsed.if_index,
            if_descr=if_descr,
            is_up=(parsed.trap_name == "linkUp"),
            source=AlertSource.SNMP_TRAP,
        )
        db.commit()
        if changed:
            logger.info("Trap-driven %s: %s %s (ifIndex %s)", parsed.trap_name, device.hostname, if_descr, parsed.if_index)
        return

    severity, category = snmp_service.classify_trap(parsed.trap_name)
    alert, is_new = alert_service.raise_alert(
        db,
        device_id=device.id,
        severity=severity,
        source=AlertSource.SNMP_TRAP,
        category=category,
        message=f"{device.hostname}: received {category} trap ({parsed.trap_name})",
    )
    db.commit()
    if is_new:
        from app.services import notification_service

        notification_service.notify(
            event=category,
            message=f"{device.hostname}: received {category} trap ({parsed.trap_name})",
            severity=severity,
            device_hostname=device.hostname,
        )


def _ingest_sync(source_ip: str, raw: bytes) -> None:
    parsed = parse_trap_packet(raw)
    if parsed is None:
        return
    db = SessionLocal()
    try:
        ingest_trap(db, source_ip=source_ip, parsed=parsed)
    except Exception:
        logger.exception("Failed to ingest SNMP trap from %s", source_ip)
        db.rollback()
    finally:
        db.close()


class SnmpTrapUDPProtocol(asyncio.DatagramProtocol):
    """Minimal asyncio UDP trap receiver -- same shape as
    syslog_service.SyslogUDPProtocol. Decode + DB work is dispatched to
    a worker thread so a slow ingest never blocks the event loop that's
    also receiving the next packet, which matters here even more than
    for syslog: a burst of traps during a real outage (a switch's whole
    uplink flapping, each port firing its own linkDown) must never back
    up behind a slow one.
    """

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        source_ip = addr[0]
        asyncio.create_task(asyncio.to_thread(_ingest_sync, source_ip, data))


async def start_trap_listener(host: str = "0.0.0.0", port: int | None = None) -> asyncio.DatagramTransport | None:
    """Starts the UDP SNMP trap listener, returning its transport (so
    app.main can close it on shutdown) or None if binding failed --
    logged, not raised, same policy as start_syslog_listener: a trap
    port conflict must never prevent the rest of the API from starting.
    """
    port = port or settings.SNMP_TRAP_UDP_PORT
    loop = asyncio.get_running_loop()
    try:
        transport, _protocol = await loop.create_datagram_endpoint(SnmpTrapUDPProtocol, local_addr=(host, port))
        logger.info("SNMP trap UDP listener started on %s:%s", host, port)
        return transport
    except OSError:
        logger.exception(
            "Could not bind SNMP trap UDP listener on %s:%s -- traps will not be received; "
            "interface-down detection falls back to the regular SNMP_POLL_INTERVAL_SECONDS poll.",
            host,
            port,
        )
        return None
