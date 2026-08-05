import datetime
import uuid

from pydantic import BaseModel, ConfigDict

from app.models.path_trace import HopStatus, PathTraceStatus


class PathHopRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    hop_index: int
    ip_address: str | None = None
    hostname: str | None = None
    device_id: uuid.UUID | None = None
    rtt_ms: float | None = None
    packet_loss_pct: float | None = None
    status: HopStatus


class PathTraceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_device_id: uuid.UUID | None = None
    source_hostname: str | None = None  # joined in -- see app.api.path_trace
    source_ip: str
    target_device_id: uuid.UUID | None = None
    target_hostname: str | None = None  # joined in
    target_input: str
    target_resolved_ip: str | None = None
    hop_source: str
    status: PathTraceStatus
    total_hops: int
    reached_target: bool
    requested_by: str | None = None
    created_at: datetime.datetime
    hops: list[PathHopRead] = []


class PathTraceRequest(BaseModel):
    source_device_id: uuid.UUID
    # Either target_device_id (trace to a managed device) or a raw
    # hostname/IP the operator typed -- at least one must resolve.
    target_device_id: uuid.UUID | None = None
    target_input: str | None = None