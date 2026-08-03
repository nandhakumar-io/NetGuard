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


class TopologyEdgeRead(BaseModel):
    source: str
    target: str
    subnet: str | None = None
    source_ip: str | None = None
    target_ip: str | None = None
    link_source: str = "subnet"  # "lldp" | "cdp" | "gns3" | "subnet"
    local_port: str | None = None
    neighbor_port: str | None = None


class TopologyResponse(BaseModel):
    nodes: list[TopologyNodeRead]
    edges: list[TopologyEdgeRead]