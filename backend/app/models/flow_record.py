import enum
import uuid

from sqlalchemy import BigInteger, Column, DateTime, Enum, ForeignKey, Integer, SmallInteger, String, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class FlowProtocolVersion(str, enum.Enum):
    """Wire protocol the exporter spoke, kept distinct from the observed
    IP protocol (TCP/UDP/...) below. NETFLOW_V5 is fully decoded (fixed
    48-byte record format). NETFLOW_V9 and IPFIX are template-based --
    packets are accepted and template-defined fields are decoded on a
    best-effort basis (see app.services.flow_service), so an exporter
    using either still shows up under Exporters / gets *a* row per flow
    even before every vendor-specific field type is mapped.
    SFLOW carries sampled raw packet headers rather than flow records, so
    its counters are already-extrapolated (sampled) byte/packet counts.
    """

    NETFLOW_V5 = "netflow_v5"
    NETFLOW_V9 = "netflow_v9"
    IPFIX = "ipfix"
    SFLOW = "sflow"


class FlowRecord(Base):
    """One exported traffic flow (a unidirectional src/dst/port/protocol
    conversation over some interval), from a NetFlow/IPFIX exporter or an
    sFlow agent running on a managed device or upstream router/switch.

    This is deliberately *not* full packet capture -- it's the same
    5-tuple + counters granularity NetFlow/sFlow/IPFIX itself exports,
    which is what "top talkers" / "top conversations" / protocol
    breakdown / traffic-aware alerting all need without the storage cost
    of raw packets. See app.services.flow_service for ingestion and
    app.api.flows for the query surface (Traffic Analysis page).
    """

    __tablename__ = "flow_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # The device that *exported* this flow (i.e. the router/switch/probe
    # whose interface the traffic crossed), resolved by exporter source
    # IP against app.models.device.Device.ip_address the same way
    # syslog_service._resolve_device matches inbound packets to a known
    # device. Nullable: an exporter that hasn't been added as a managed
    # Device yet still has its flows recorded (surfaced under an
    # "Unknown exporter" bucket) rather than silently dropped, since a
    # newly-cabled flow exporter is exactly the kind of source you want
    # visibility into before it's been fully onboarded.
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True, index=True)
    exporter_ip = Column(String, nullable=False, index=True)

    flow_version = Column(Enum(FlowProtocolVersion), nullable=False)
    # 1-in-N sampling rate reported by the exporter (NetFlow sampler-id/
    # IPFIX samplingInterval, or sFlow's own packet-sampling rate). 1
    # means unsampled. Byte/packet counters below are already
    # multiplied out to estimated real traffic (see
    # app.services.flow_service._apply_sampling) so callers never have
    # to remember to scale them -- this column is kept only so the UI
    # can show "(sampled 1:100)" as a caveat on the numbers.
    sampling_rate = Column(Integer, nullable=False, server_default="1")

    src_ip = Column(String, nullable=False, index=True)
    dst_ip = Column(String, nullable=False, index=True)
    src_port = Column(Integer, nullable=True)
    dst_port = Column(Integer, nullable=True)
    # IANA IP protocol number (6=TCP, 17=UDP, 1=ICMP, ...), not decoded
    # to a name here -- app.services.flow_service.PROTOCOL_NAMES does
    # that at the query layer so this table stays a cheap-to-index
    # SmallInteger.
    ip_protocol = Column(SmallInteger, nullable=False)
    tos = Column(SmallInteger, nullable=True)  # IP ToS/DSCP byte, when the exporter reports it

    src_as = Column(Integer, nullable=True)
    dst_as = Column(Integer, nullable=True)
    input_snmp_if = Column(Integer, nullable=True)
    output_snmp_if = Column(Integer, nullable=True)
    tcp_flags = Column(SmallInteger, nullable=True)

    bytes = Column(BigInteger, nullable=False)
    packets = Column(BigInteger, nullable=False)

    # Flow interval as reported by the exporter (NetFlow First/Last,
    # IPFIX flowStart/EndMilliseconds). Falls back to received_at for
    # sFlow counter samples / anything that doesn't report an interval,
    # so duration-based rate math never divides by a null.
    flow_start = Column(DateTime(timezone=True), nullable=True)
    flow_end = Column(DateTime(timezone=True), nullable=True)

    received_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)