from pydantic import BaseModel


class TopTalker(BaseModel):
    ip_address: str
    bytes: int
    packets: int


class TopConversation(BaseModel):
    src_ip: str
    dst_ip: str
    protocol: str
    bytes: int
    packets: int


class ProtocolShare(BaseModel):
    protocol: str
    bytes: int
    pct: float


class BandwidthPoint(BaseModel):
    timestamp: str
    bytes_per_sec: float


class FlowExporter(BaseModel):
    exporter_ip: str
    flow_version: str
    hostname: str | None
    last_seen: str | None
    flow_count: int


class TrafficSummary(BaseModel):
    window_minutes: int
    top_talkers: list[TopTalker]
    top_conversations: list[TopConversation]
    protocol_breakdown: list[ProtocolShare]
    bandwidth_timeseries: list[BandwidthPoint]
    exporters: list[FlowExporter]