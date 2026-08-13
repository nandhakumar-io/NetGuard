from pydantic import BaseModel


class TopologyNodeRead(BaseModel):
    id: str
    hostname: str
    ip_address: str
    vendor: str
    site: str | None = None
    device_type: str | None = None
    status: str
    flagged_unstable: bool = False
    has_config_on_file: bool = False
    health_color: str | None = None
    health_score: int | None = None
    data_center: str | None = None
    rack: str | None = None
    device_role: str | None = None
    interface_error_rate: int | None = None
    active_alert_severity: str | None = None


class TopologyEdgeRead(BaseModel):
    source: str
    target: str
    subnet: str | None = None
    source_ip: str | None = None
    target_ip: str | None = None
    link_source: str = "subnet"  # "lldp" | "cdp" | "gns3" | "subnet"
    local_port: str | None = None
    neighbor_port: str | None = None
    utilization_pct: int | None = None
    last_confirmed_at: str | None = None
    stale: bool = False


class TopologyResponse(BaseModel):
    nodes: list[TopologyNodeRead]
    edges: list[TopologyEdgeRead]


class TopologySnapshotRead(BaseModel):
    id: str
    node_count: int
    edge_count: int
    captured_at: str | None


class TopologyDiffResponse(BaseModel):
    older_snapshot_id: str
    newer_snapshot_id: str
    older_captured_at: str | None
    newer_captured_at: str | None
    added_nodes: list[dict]
    removed_nodes: list[dict]
    added_edges: list[dict]
    removed_edges: list[dict]
    unchanged_node_count: int
    unchanged_edge_count: int
