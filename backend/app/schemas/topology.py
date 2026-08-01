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
    subnet: str
    source_ip: str
    target_ip: str


class TopologyResponse(BaseModel):
    nodes: list[TopologyNodeRead]
    edges: list[TopologyEdgeRead]