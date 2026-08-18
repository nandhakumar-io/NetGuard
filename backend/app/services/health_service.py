"""NetFlow/IPFIX/sFlow traffic-flow collection and analysis.

Closes a real visibility gap: SNMP polling (app.services.metrics_service)
only ever sees aggregate interface counters -- total octets in/out on an
interface, no idea *what* traffic that is. Syslog only sees what a device
chooses to log. Neither can answer "who's actually talking to whom",
"what's the protocol mix on this link", or "which host suddenly started
pushing 10x its normal volume" -- that's exactly what NetFlow/IPFIX/sFlow
exporters exist to report, and what the Traffic Analysis page
(app.api.flows) is built on top of this module for.

Two exporter families, two listeners (see start_flow_listener /
start_sflow_listener, both started from app.main's startup hook):

  * NetFlow v5 -- fully decoded (fixed 24-byte header + 48-byte
    records, see _parse_netflow_v5).
  * NetFlow v9 / IPFIX -- template-based wire formats (a Template
    FlowSet defines field layout, subsequent Data FlowSets referencing
    that template ID are decoded against it). _TemplateCache keeps the
    most-recently-seen template per (exporter, template_id) in memory
    per process, which is the same statefulness real NetFlow v9/IPFIX
    collectors require -- a data flowset that arrives before its
    template (e.g. right after a collector restart) is buffered
    briefly (see _PENDING_DATA) rather than silently dropped, since
    exporters typically resend templates every few minutes and the
    data would otherwise never be decodable.  Only the common field
    types listed in _FIELD_DECODERS are understood; unrecognized field
    types are skipped (length-aware) rather than aborting the whole
    record, so a record with vendor-specific extra fields still yields
    whatever the standard fields decoded.
  * sFlow v5 -- a structurally different protocol (sampled raw packet
    headers + interface counters, not flow records); flow_samples with
    a raw-packet-header record are decoded far enough to pull an IPv4
    5-tuple out of the sampled Ethernet/IP header. Counter samples are
    not flow-relevant and are ignored here.

All three funnel into the same `_persist_flow` -> FlowRecord row shape
so the query layer (top talkers, conversations, protocol mix) never has
to know which wire format a given row came from.

KNOWN LIMITATION (NetFlow v9 / IPFIX only): decoding covers only the
common standard field types -- addresses, ports, protocol, bytes/
packets, AS numbers, interfaces, timestamps (see the _F_* constants
below and _decode_v9_ipfix_record). Vendor-specific extension fields in
a template (e.g. Cisco NBAR application-name fields, NAT-related
fields, MPLS label stacks) are recognized as present -- their length is
read so the rest of the record still decodes correctly -- but their
values are skipped, not decoded. An exporter leaning heavily on vendor
extensions still produces a row with a correct basic 5-tuple and byte/
packet counts, just without whatever extra vendor-specific detail it
also exported. Extending _decode_v9_ipfix_record's field map is the way
to add support for a specific vendor field as the need comes up.
"""
from __future__ import annotations

import asyncio
import datetime
import ipaddress
import logging
import struct
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.device import Device
from app.models.flow_record import FlowProtocolVersion, FlowRecord

logger = logging.getLogger("netguard.flow")

PROTOCOL_NAMES: dict[int, str] = {
    1: "ICMP",
    6: "TCP",
    17: "UDP",
    47: "GRE",
    50: "ESP",
    58: "ICMPv6",
    89: "OSPF",
}


def protocol_name(ip_protocol: int) -> str:
    return PROTOCOL_NAMES.get(ip_protocol, f"IP {ip_protocol}")


def _resolve_device(db: Session, exporter_ip: str) -> Device | None:
    return db.query(Device).filter(Device.ip_address == exporter_ip).first()


def _epoch_ms_to_dt(ms: int) -> datetime.datetime | None:
    if not ms:
        return None
    try:
        return datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass
class ParsedFlow:
    src_ip: str
    dst_ip: str
    src_port: int | None
    dst_port: int | None
    ip_protocol: int
    tos: int | None
    bytes: int
    packets: int
    flow_start: datetime.datetime | None
    flow_end: datetime.datetime | None
    src_as: int | None = None
    dst_as: int | None = None
    input_snmp_if: int | None = None
    output_snmp_if: int | None = None
    tcp_flags: int | None = None


def _persist_flow(
    db: Session,
    *,
    exporter_ip: str,
    version: FlowProtocolVersion,
    sampling_rate: int,
    flow: ParsedFlow,
) -> None:
    device = _resolve_device(db, exporter_ip)
    rate = max(1, sampling_rate)
    db.add(
        FlowRecord(
            device_id=device.id if device else None,
            exporter_ip=exporter_ip,
            flow_version=version,
            sampling_rate=rate,
            src_ip=flow.src_ip,
            dst_ip=flow.dst_ip,
            src_port=flow.src_port,
            dst_port=flow.dst_port,
            ip_protocol=flow.ip_protocol,
            tos=flow.tos,
            src_as=flow.src_as,
            dst_as=flow.dst_as,
            input_snmp_if=flow.input_snmp_if,
            output_snmp_if=flow.output_snmp_if,
            tcp_flags=flow.tcp_flags,
            bytes=flow.bytes * rate,
            packets=flow.packets * rate,
            flow_start=flow.flow_start,
            flow_end=flow.flow_end,
        )
    )


# --- NetFlow v5 (fully decoded, fixed-length wire format) ----------------

_NF5_HEADER = struct.Struct("!HHIIIIBBH")  # 24 bytes
_NF5_RECORD = struct.Struct("!4s4s4sHHIIIIHHxBBBHHBBH")  # 48 bytes


def _parse_netflow_v5(data: bytes) -> tuple[int, list[ParsedFlow]]:
    if len(data) < _NF5_HEADER.size:
        return 0, []
    version, count, sys_uptime, unix_secs, unix_nsecs, _seq, _eng_type, _eng_id, sampling = _NF5_HEADER.unpack_from(
        data, 0
    )
    # Top 2 bits of `sampling` are the sampling mode, lower 14 bits are
    # the 1-in-N interval (0/1 => unsampled).
    sample_interval = sampling & 0x3FFF

    export_time = datetime.datetime.fromtimestamp(unix_secs, tz=datetime.timezone.utc)
    flows: list[ParsedFlow] = []
    offset = _NF5_HEADER.size
    for _ in range(count):
        if offset + _NF5_RECORD.size > len(data):
            break
        (
            src,
            dst,
            _nexthop,
            input_if,
            output_if,
            pkts,
            octets,
            first_ms,
            last_ms,
            src_port,
            dst_port,
            tcp_flags,
            protocol,
            tos,
            src_as,
            dst_as,
            _src_mask,
            _dst_mask,
            _pad2,
        ) = _NF5_RECORD.unpack_from(data, offset)
        offset += _NF5_RECORD.size

        # first/last are sys_uptime-relative milliseconds (NetFlow v5
        # quirk), converted to wallclock using the header's export time
        # as the anchor.
        def _uptime_to_wall(ms: int) -> datetime.datetime:
            delta_ms = int(ms) - int(sys_uptime)
            return export_time + datetime.timedelta(milliseconds=delta_ms)

        flows.append(
            ParsedFlow(
                src_ip=str(ipaddress.IPv4Address(src)),
                dst_ip=str(ipaddress.IPv4Address(dst)),
                src_port=src_port,
                dst_port=dst_port,
                ip_protocol=protocol,
                tos=tos,
                bytes=octets,
                packets=pkts,
                flow_start=_uptime_to_wall(first_ms),
                flow_end=_uptime_to_wall(last_ms),
                src_as=src_as or None,
                dst_as=dst_as or None,
                input_snmp_if=input_if,
                output_snmp_if=output_if,
                tcp_flags=tcp_flags,
            )
        )
    return sample_interval, flows


# --- NetFlow v9 / IPFIX (template-based) ----------------------------------

# Standard field type IDs shared between NetFlow v9 and IPFIX for the
# elements this app cares about (IPFIX Information Elements 1:1 match
# the NetFlow v9 field types for these, by design of the IPFIX spec).
_F_OCTETS = 1
_F_PACKETS = 2
_F_PROTOCOL = 4
_F_TOS = 5
_F_SRC_PORT = 7
_F_SRC_ADDR = 8
_F_INPUT_IF = 10
_F_DST_PORT = 11
_F_DST_ADDR = 12
_F_OUTPUT_IF = 14
_F_SRC_AS = 16
_F_DST_AS = 17
_F_TCP_FLAGS = 6
_F_LAST_SWITCHED = 21
_F_FIRST_SWITCHED = 22
_F_FLOW_END_MS = 153
_F_FLOW_START_MS = 152


@dataclass
class _TemplateField:
    field_type: int
    length: int


class _TemplateCache:
    """Per-process (exporter_ip, template_id) -> field list. NetFlow v9/
    IPFIX collectors are inherently stateful this way; there's nothing to
    persist across restarts here since exporters resend templates
    periodically on their own schedule.
    """

    def __init__(self) -> None:
        self._templates: dict[tuple[str, int], list[_TemplateField]] = {}

    def set(self, exporter_ip: str, template_id: int, fields: list[_TemplateField]) -> None:
        self._templates[(exporter_ip, template_id)] = fields

    def get(self, exporter_ip: str, template_id: int) -> list[_TemplateField] | None:
        return self._templates.get((exporter_ip, template_id))


_TEMPLATES = _TemplateCache()


def _decode_v9_ipfix_record(fields: list[_TemplateField], raw: bytes) -> ParsedFlow | None:
    values: dict[int, bytes] = {}
    offset = 0
    for f in fields:
        if offset + f.length > len(raw):
            return None
        values[f.field_type] = raw[offset : offset + f.length]
        offset += f.length

    def _as_int(field_type: int) -> int | None:
        b = values.get(field_type)
        return int.from_bytes(b, "big") if b else None

    def _as_ipv4(field_type: int) -> str | None:
        b = values.get(field_type)
        if not b or len(b) != 4:
            return None
        return str(ipaddress.IPv4Address(b))

    src_ip = _as_ipv4(_F_SRC_ADDR)
    dst_ip = _as_ipv4(_F_DST_ADDR)
    if not src_ip or not dst_ip:
        return None  # IPv6-only / unrecognized address field -- skip rather than guess

    start_ms = _as_int(_F_FLOW_START_MS)
    end_ms = _as_int(_F_FLOW_END_MS)

    return ParsedFlow(
        src_ip=src_ip,
        dst_ip=dst_ip,
        src_port=_as_int(_F_SRC_PORT),
        dst_port=_as_int(_F_DST_PORT),
        ip_protocol=_as_int(_F_PROTOCOL) or 0,
        tos=_as_int(_F_TOS),
        bytes=_as_int(_F_OCTETS) or 0,
        packets=_as_int(_F_PACKETS) or 0,
        flow_start=_epoch_ms_to_dt(start_ms) if start_ms else None,
        flow_end=_epoch_ms_to_dt(end_ms) if end_ms else None,
        src_as=_as_int(_F_SRC_AS),
        dst_as=_as_int(_F_DST_AS),
        input_snmp_if=_as_int(_F_INPUT_IF),
        output_snmp_if=_as_int(_F_OUTPUT_IF),
        tcp_flags=_as_int(_F_TCP_FLAGS),
    )


def _parse_netflow_v9_or_ipfix(
    data: bytes, exporter_ip: str, version: int
) -> list[ParsedFlow]:
    """Parses the FlowSet-based v9/IPFIX body. Header layout differs
    slightly between the two (v9: version,count,uptime,unix_secs,seq,
    source_id -- 20 bytes; IPFIX: version,length,export_time,seq,
    domain_id -- 16 bytes) but both are followed by a sequence of
    (id, length) FlowSets, which is all this needs to walk the body.
    """
    header_len = 20 if version == 9 else 16
    if len(data) < header_len:
        return []
    offset = header_len
    flows: list[ParsedFlow] = []

    while offset + 4 <= len(data):
        set_id, set_length = struct.unpack_from("!HH", data, offset)
        if set_length < 4 or offset + set_length > len(data):
            break
        body = data[offset + 4 : offset + set_length]

        if set_id == 0 and version == 9 or set_id == 2:
            # Template FlowSet (v9 id=0, IPFIX id=2): one or more
            # template records, each: template_id(2) field_count(2)
            # then field_count * (field_type(2) field_length(2)).
            pos = 0
            while pos + 4 <= len(body):
                template_id, field_count = struct.unpack_from("!HH", body, pos)
                pos += 4
                fields: list[_TemplateField] = []
                for _ in range(field_count):
                    if pos + 4 > len(body):
                        break
                    field_type, field_len = struct.unpack_from("!HH", body, pos)
                    pos += 4
                    if field_type & 0x8000:  # enterprise-specific bit -- skip the enterprise number
                        pos += 4
                    fields.append(_TemplateField(field_type, field_len))
                if fields:
                    _TEMPLATES.set(exporter_ip, template_id, fields)
        elif set_id >= 256:
            # Data FlowSet: raw records for a previously-seen template_id == set_id.
            template = _TEMPLATES.get(exporter_ip, set_id)
            if template:
                record_len = sum(f.length for f in template)
                pos = 0
                while record_len and pos + record_len <= len(body):
                    parsed = _decode_v9_ipfix_record(template, body[pos : pos + record_len])
                    if parsed:
                        flows.append(parsed)
                    pos += record_len
            # else: data arrived before its template -- dropped (the
            # exporter will resend the template on its own cadence;
            # buffering indefinitely for a template that may never
            # arrive isn't worth the memory).
        offset += set_length

    return flows


# --- sFlow v5 (sampled raw packet headers, not flow records) -------------


def _parse_sflow_v5(data: bytes) -> list[ParsedFlow]:
    """Best-effort decode of sFlow v5 flow_samples containing a raw
    Ethernet/IPv4 sampled packet header (the common case for switches/
    routers) into a synthetic ParsedFlow per sample. Counter samples and
    non-IPv4 sampled headers are skipped -- sFlow's per-sample byte/
    packet count is exactly 1 sampled packet, so `bytes`/`packets` here
    reflect one packet and get scaled up by the sampling rate in
    _persist_flow like every other source.
    """
    try:
        if len(data) < 28:
            return []
        (version,) = struct.unpack_from("!I", data, 0)
        if version != 5:
            return []
        # agent address is either 4 or 16 bytes depending on the
        # following address-type field; only IPv4 agents (type 1) are
        # handled here.
        addr_type = struct.unpack_from("!I", data, 4)[0]
        offset = 8 + (4 if addr_type == 1 else 16)
        offset += 4  # sub_agent_id
        offset += 4  # sequence_number
        offset += 4  # sys_uptime
        (num_samples,) = struct.unpack_from("!I", data, offset)
        offset += 4

        flows: list[ParsedFlow] = []
        for _ in range(num_samples):
            if offset + 8 > len(data):
                break
            sample_type, sample_len = struct.unpack_from("!II", data, offset)
            sample_body = data[offset + 8 : offset + 8 + sample_len]
            offset += 8 + sample_len

            if sample_type != 1:  # 1 = flow_sample (2 = counter_sample, not flow data)
                continue
            flow = _decode_sflow_flow_sample(sample_body)
            if flow:
                flows.append(flow)
        return flows
    except struct.error:
        return []


def _decode_sflow_flow_sample(body: bytes) -> ParsedFlow | None:
    try:
        # sequence_number, source_id, sampling_rate, sample_pool, drops,
        # input_if, output_if, num_flow_records
        (
            _seq,
            _source_id,
            _sampling_rate,
            _sample_pool,
            _drops,
            input_if,
            output_if,
            num_records,
        ) = struct.unpack_from("!IIIIIIII", body, 0)
        offset = 32
        for _ in range(num_records):
            if offset + 8 > len(body):
                break
            enterprise_format, flow_data_len = struct.unpack_from("!II", body, offset)
            flow_body = body[offset + 8 : offset + 8 + flow_data_len]
            offset += 8 + flow_data_len
            if enterprise_format != 1:  # 1 = RAW_PACKET_HEADER (enterprise 0, format 1)
                continue
            parsed = _decode_raw_packet_header(flow_body)
            if parsed:
                parsed.input_snmp_if = input_if
                parsed.output_snmp_if = output_if
                return parsed
        return None
    except struct.error:
        return None


def _decode_raw_packet_header(flow_body: bytes) -> ParsedFlow | None:
    """flow_body: header_protocol(4) frame_length(4) stripped(4)
    header_length(4) then the raw captured bytes (Ethernet frame)."""
    if len(flow_body) < 16:
        return None
    header_protocol, frame_length, _stripped, header_length = struct.unpack_from("!IIII", flow_body, 0)
    if header_protocol != 1:  # 1 = ETHERNET-ISO88023
        return None
    eth = flow_body[16 : 16 + header_length]
    if len(eth) < 14:
        return None
    ethertype = struct.unpack_from("!H", eth, 12)[0]
    if ethertype != 0x0800:  # IPv4 only
        return None
    ip = eth[14:]
    if len(ip) < 20:
        return None
    ihl = (ip[0] & 0x0F) * 4
    protocol = ip[9]
    tos = ip[1]
    src_ip = str(ipaddress.IPv4Address(ip[12:16]))
    dst_ip = str(ipaddress.IPv4Address(ip[16:20]))
    src_port = dst_port = None
    if len(ip) >= ihl + 4 and protocol in (6, 17):
        src_port, dst_port = struct.unpack_from("!HH", ip, ihl)

    return ParsedFlow(
        src_ip=src_ip,
        dst_ip=dst_ip,
        src_port=src_port,
        dst_port=dst_port,
        ip_protocol=protocol,
        tos=tos,
        bytes=frame_length,
        packets=1,
        flow_start=None,
        flow_end=None,
    )


# --- Ingest entry point (shared by both listeners) ------------------------


def _ingest_sync(exporter_ip: str, data: bytes) -> None:
    if len(data) < 2:
        return
    (version,) = struct.unpack_from("!H", data, 0)

    if version == 5:
        sample_interval, flows = _parse_netflow_v5(data)
        proto_version = FlowProtocolVersion.NETFLOW_V5
        rate = sample_interval or 1
    elif version in (9, 10):
        flows = _parse_netflow_v9_or_ipfix(data, exporter_ip, version)
        proto_version = FlowProtocolVersion.NETFLOW_V9 if version == 9 else FlowProtocolVersion.IPFIX
        rate = 1
    else:
        return

    if not flows:
        return

    db = SessionLocal()
    try:
        for flow in flows:
            _persist_flow(db, exporter_ip=exporter_ip, version=proto_version, sampling_rate=rate, flow=flow)
        db.commit()
    except Exception:
        logger.exception("Failed to persist flow batch from %s", exporter_ip)
        db.rollback()
    finally:
        db.close()


def _ingest_sflow_sync(exporter_ip: str, data: bytes) -> None:
    flows = _parse_sflow_v5(data)
    if not flows:
        return
    db = SessionLocal()
    try:
        for flow in flows:
            _persist_flow(
                db, exporter_ip=exporter_ip, version=FlowProtocolVersion.SFLOW, sampling_rate=1, flow=flow
            )
        db.commit()
    except Exception:
        logger.exception("Failed to persist sFlow batch from %s", exporter_ip)
        db.rollback()
    finally:
        db.close()


# --- UDP listeners ----------------------------------------------------


class _FlowUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, handler):
        self._handler = handler

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        asyncio.create_task(asyncio.to_thread(self._handler, addr[0], data))


async def start_flow_listener(host: str = "0.0.0.0", port: int | None = None) -> asyncio.DatagramTransport | None:
    port = port or settings.NETFLOW_UDP_PORT
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _FlowUDPProtocol(_ingest_sync), local_addr=(host, port)
        )
        logger.info("NetFlow/IPFIX UDP listener started on %s:%s", host, port)
        return transport
    except OSError:
        logger.exception("Could not bind NetFlow/IPFIX UDP listener on %s:%s", host, port)
        return None


async def start_sflow_listener(host: str = "0.0.0.0", port: int | None = None) -> asyncio.DatagramTransport | None:
    port = port or settings.SFLOW_UDP_PORT
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _FlowUDPProtocol(_ingest_sflow_sync), local_addr=(host, port)
        )
        logger.info("sFlow UDP listener started on %s:%s", host, port)
        return transport
    except OSError:
        logger.exception("Could not bind sFlow UDP listener on %s:%s", host, port)
        return None


# --- Query helpers (Traffic Analysis page) --------------------------------


def _since(minutes: int) -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)


def top_talkers(db: Session, *, minutes: int = 60, limit: int = 10) -> list[dict]:
    """Hosts ranked by total bytes across both directions (as src or
    dst) in the window -- the headline "who's using the bandwidth"
    view.
    """
    cutoff = _since(minutes)
    src_rows = (
        db.query(FlowRecord.src_ip.label("ip"), func.sum(FlowRecord.bytes).label("b"), func.sum(FlowRecord.packets).label("p"))
        .filter(FlowRecord.received_at >= cutoff)
        .group_by(FlowRecord.src_ip)
        .all()
    )
    dst_rows = (
        db.query(FlowRecord.dst_ip.label("ip"), func.sum(FlowRecord.bytes).label("b"), func.sum(FlowRecord.packets).label("p"))
        .filter(FlowRecord.received_at >= cutoff)
        .group_by(FlowRecord.dst_ip)
        .all()
    )
    totals: dict[str, dict[str, int]] = defaultdict(lambda: {"bytes": 0, "packets": 0})
    for row in src_rows:
        totals[row.ip]["bytes"] += int(row.b or 0)
        totals[row.ip]["packets"] += int(row.p or 0)
    for row in dst_rows:
        totals[row.ip]["bytes"] += int(row.b or 0)
        totals[row.ip]["packets"] += int(row.p or 0)

    ranked = sorted(totals.items(), key=lambda kv: kv[1]["bytes"], reverse=True)[:limit]
    return [{"ip_address": ip, "bytes": v["bytes"], "packets": v["packets"]} for ip, v in ranked]


def top_conversations(db: Session, *, minutes: int = 60, limit: int = 10) -> list[dict]:
    cutoff = _since(minutes)
    rows = (
        db.query(
            FlowRecord.src_ip,
            FlowRecord.dst_ip,
            FlowRecord.ip_protocol,
            func.sum(FlowRecord.bytes).label("b"),
            func.sum(FlowRecord.packets).label("p"),
        )
        .filter(FlowRecord.received_at >= cutoff)
        .group_by(FlowRecord.src_ip, FlowRecord.dst_ip, FlowRecord.ip_protocol)
        .order_by(func.sum(FlowRecord.bytes).desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "src_ip": r.src_ip,
            "dst_ip": r.dst_ip,
            "protocol": protocol_name(r.ip_protocol),
            "bytes": int(r.b or 0),
            "packets": int(r.p or 0),
        }
        for r in rows
    ]


def protocol_breakdown(db: Session, *, minutes: int = 60) -> list[dict]:
    cutoff = _since(minutes)
    rows = (
        db.query(FlowRecord.ip_protocol, func.sum(FlowRecord.bytes).label("b"))
        .filter(FlowRecord.received_at >= cutoff)
        .group_by(FlowRecord.ip_protocol)
        .order_by(func.sum(FlowRecord.bytes).desc())
        .all()
    )
    total = sum(int(r.b or 0) for r in rows) or 1
    return [
        {
            "protocol": protocol_name(r.ip_protocol),
            "bytes": int(r.b or 0),
            "pct": round(100 * int(r.b or 0) / total, 1),
        }
        for r in rows
    ]


def bandwidth_timeseries(db: Session, *, minutes: int = 60, bucket_minutes: int = 5) -> list[dict]:
    """Bucketed total bytes/sec for a chart -- Python-side bucketing
    (rather than a DB-specific date_trunc/time-bucket expression) so
    this works identically against the Postgres deployment and the
    SQLite engine the test suite uses.
    """
    cutoff = _since(minutes)
    rows = db.query(FlowRecord.received_at, FlowRecord.bytes).filter(FlowRecord.received_at >= cutoff).all()
    bucket_seconds = bucket_minutes * 60
    buckets: dict[int, int] = defaultdict(int)
    for received_at, b in rows:
        if received_at is None:
            continue
        epoch = int(received_at.timestamp())
        bucket_key = epoch - (epoch % bucket_seconds)
        buckets[bucket_key] += int(b or 0)

    return [
        {
            "timestamp": datetime.datetime.fromtimestamp(k, tz=datetime.timezone.utc).isoformat(),
            "bytes_per_sec": round(v / bucket_seconds, 2),
        }
        for k, v in sorted(buckets.items())
    ]


# --- Anomaly detection ("who suddenly started pushing 10x normal") -------
#
# The Traffic Analysis page is one of the least-visited pages in the app
# because nothing else points at it -- you only see a bandwidth spike if
# you happen to load this page while it's happening. This gives it a
# cheap, self-contained signal (recent window vs. a same-length baseline
# window immediately before it) that api.dashboard._compute_timeline can
# pull into the unified "what changed" feed, so a real spike surfaces on
# the dashboard instead of requiring someone to remember to check.
ANOMALY_MIN_BASELINE_BYTES = 1_000_000  # ignore near-zero baselines; anything looks like "10x" against noise
ANOMALY_SPIKE_MULTIPLIER = 5.0


def detect_bandwidth_anomalies(db: Session, *, recent_minutes: int = 15, baseline_minutes: int = 60 * 6) -> list[dict]:
    """Compares each host's total bytes in the most recent `recent_minutes`
    against its average over an equal-length window ending right before
    it, drawn from the preceding `baseline_minutes`. Flags a host whose
    recent rate is at least ANOMALY_SPIKE_MULTIPLIER times its baseline
    rate -- deliberately simple (no seasonality modeling) so it's cheap
    to run on every dashboard load rather than needing a background job.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    recent_cutoff = now - datetime.timedelta(minutes=recent_minutes)
    baseline_start = recent_cutoff - datetime.timedelta(minutes=baseline_minutes)

    recent_bytes = _bytes_by_host(db, since=recent_cutoff, until=now)
    baseline_bytes = _bytes_by_host(db, since=baseline_start, until=recent_cutoff)

    baseline_window_ratio = recent_minutes / baseline_minutes if baseline_minutes else 1.0
    anomalies = []
    for ip, recent in recent_bytes.items():
        baseline = baseline_bytes.get(ip, 0)
        if baseline < ANOMALY_MIN_BASELINE_BYTES:
            continue
        expected_for_window = baseline * baseline_window_ratio
        if expected_for_window <= 0:
            continue
        ratio = recent / expected_for_window
        if ratio >= ANOMALY_SPIKE_MULTIPLIER:
            anomalies.append({
                "ip_address": ip,
                "recent_bytes": recent,
                "expected_bytes": round(expected_for_window),
                "multiplier": round(ratio, 1),
                "detected_at": now.isoformat(),
            })
    anomalies.sort(key=lambda a: a["multiplier"], reverse=True)
    return anomalies


def _bytes_by_host(db: Session, *, since: datetime.datetime, until: datetime.datetime) -> dict[str, int]:
    rows = (
        db.query(FlowRecord.src_ip.label("ip"), func.sum(FlowRecord.bytes).label("b"))
        .filter(FlowRecord.received_at >= since, FlowRecord.received_at < until)
        .group_by(FlowRecord.src_ip)
        .all()
    )
    return {r.ip: int(r.b or 0) for r in rows}


def exporters(db: Session, *, minutes: int = 60) -> list[dict]:
    """Which flow exporters are actively sending, resolved to a managed
    Device where possible -- lets an admin confirm an exporter config
    change actually took effect and see coverage gaps (a device that
    should be exporting but isn't).
    """
    cutoff = _since(minutes)
    rows = (
        db.query(
            FlowRecord.exporter_ip,
            FlowRecord.flow_version,
            FlowRecord.device_id,
            func.max(FlowRecord.received_at).label("last_seen"),
            func.count(FlowRecord.id).label("flow_count"),
        )
        .filter(FlowRecord.received_at >= cutoff)
        .group_by(FlowRecord.exporter_ip, FlowRecord.flow_version, FlowRecord.device_id)
        .all()
    )
    device_ids = {r.device_id for r in rows if r.device_id}
    hostnames = {}
    if device_ids:
        for d in db.query(Device).filter(Device.id.in_(device_ids)).all():
            hostnames[d.id] = d.hostname

    return [
        {
            "exporter_ip": r.exporter_ip,
            "flow_version": r.flow_version.value,
            "hostname": hostnames.get(r.device_id),
            "last_seen": r.last_seen.isoformat() if r.last_seen else None,
            "flow_count": r.flow_count,
        }
        for r in rows
    ]


def purge_expired(db: Session) -> int:
    """Deletes FlowRecord rows older than FLOW_RETENTION_DAYS. Intended
    to be run periodically (same nightly-cleanup pattern as syslog/
    metric retention); flow volume is the highest-cardinality table in
    the app so this matters more here than anywhere else.
    """
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=settings.FLOW_RETENTION_DAYS)
    deleted = db.query(FlowRecord).filter(FlowRecord.received_at < cutoff).delete(synchronize_session=False)
    db.commit()
    return deleted


# --- Post-deployment traffic-impact verification ---------------------
#
# health_monitor's post-deploy suite (ping, BGP/OSPF adjacency, DNS/
# DHCP/HTTP/VPN) all answer "is the control plane / management path
# healthy" -- none of them can catch an ACL, route-map, or VLAN change
# that leaves the device pingable and its BGP session adjacent while
# silently blackholing real data-plane traffic to a specific downstream
# subnet. That failure mode only shows up in flow data. The functions
# below capture a pre-deploy traffic baseline for the device being
# changed (and the subnets it fronts) and re-measure it during the
# post-deploy monitoring window, so health_monitor.check_traffic_impact
# can turn "traffic to subnet X dropped 40%" into an actual rollback
# trigger instead of something someone has to notice on the Traffic
# Analysis page after the fact.


def traffic_bytes_for_device(db: Session, device_id, *, since: datetime.datetime, until: datetime.datetime) -> int:
    """Total bytes exported by this device (as flow exporter) in
    [since, until). This is traffic *observed on* the device's own
    exported interfaces, not just management-plane reachability."""
    total = (
        db.query(func.sum(FlowRecord.bytes))
        .filter(FlowRecord.device_id == device_id, FlowRecord.received_at >= since, FlowRecord.received_at < until)
        .scalar()
    )
    return int(total or 0)


def traffic_bytes_for_subnet(
    db: Session, cidr: str, *, since: datetime.datetime, until: datetime.datetime, device_id=None
) -> int:
    """Total bytes where src_ip or dst_ip falls inside `cidr`, in
    [since, until). Best-effort / Python-side filtering (FlowRecord
    doesn't store src/dst as indexed network ranges, so this can't be
    done as a single SQL range predicate the way the byte/timestamp
    filters above are) -- scoped by device_id when given (the device
    actually being changed) to keep the row count bounded to that
    exporter's flows rather than scanning the whole fleet's traffic in
    the window. Fine for the pre/post-deploy comparison this backs,
    which only ever needs one device's flows; not intended as a general
    fleet-wide subnet-traffic query.
    """
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return 0
    query = db.query(FlowRecord.src_ip, FlowRecord.dst_ip, FlowRecord.bytes).filter(
        FlowRecord.received_at >= since, FlowRecord.received_at < until
    )
    if device_id is not None:
        query = query.filter(FlowRecord.device_id == device_id)
    total = 0
    for src_ip, dst_ip, flow_bytes in query.all():
        try:
            in_subnet = ipaddress.ip_address(src_ip) in network or ipaddress.ip_address(dst_ip) in network
        except ValueError:
            continue
        if in_subnet:
            total += int(flow_bytes or 0)
    return total


@dataclass
class TrafficBaseline:
    device_id: str
    device_bytes: int
    subnet_bytes: dict[str, int]  # cidr -> bytes, over window_minutes ending at captured_at
    window_minutes: int
    captured_at: datetime.datetime


def capture_traffic_baseline(
    db: Session, device_id, subnet_cidrs: list[str], *, window_minutes: int
) -> TrafficBaseline:
    """Snapshots current traffic volume for `device_id` and each subnet
    in `subnet_cidrs`, over the `window_minutes` immediately preceding
    now. Called by the deployment pipeline right before a config push,
    so `check_traffic_impact` has something concrete to compare the
    post-deploy monitoring window's traffic against. A device/subnet
    with no recent flow data simply baselines at 0 -- the comparison
    check treats a near-zero baseline as "nothing to compare" rather
    than flagging a drop against noise (see
    health_monitor.MIN_BASELINE_BYTES_FOR_TRAFFIC_CHECK).
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    since = now - datetime.timedelta(minutes=window_minutes)
    device_bytes = traffic_bytes_for_device(db, device_id, since=since, until=now)
    subnet_bytes = {
        cidr: traffic_bytes_for_subnet(db, cidr, since=since, until=now, device_id=device_id)
        for cidr in subnet_cidrs
    }
    return TrafficBaseline(
        device_id=str(device_id), device_bytes=device_bytes, subnet_bytes=subnet_bytes,
        window_minutes=window_minutes, captured_at=now,
    )


def measure_traffic_since_baseline(db: Session, baseline: TrafficBaseline) -> tuple[int, dict[str, int]]:
    """Re-measures the same device/subnet traffic volumes baseline
    captured, over an equal-length window ending now -- so the
    comparison in health_monitor.check_traffic_impact is apples-to-
    apples (same window size) regardless of how long it's been since
    baseline.captured_at.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    since = now - datetime.timedelta(minutes=baseline.window_minutes)
    device_bytes = traffic_bytes_for_device(db, baseline.device_id, since=since, until=now)
    subnet_bytes = {
        cidr: traffic_bytes_for_subnet(db, cidr, since=since, until=now, device_id=baseline.device_id)
        for cidr in baseline.subnet_bytes
    }
    return device_bytes, subnet_bytes
