import enum
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base


class PathTraceStatus(str, enum.Enum):
    COMPLETE = "complete"  # reached the target
    PARTIAL = "partial"  # ran out of hops / graph without confirming the target was reached
    FAILED = "failed"  # couldn't even resolve a first hop


class HopStatus(str, enum.Enum):
    OK = "ok"  # responded, latency/loss within normal range
    DEGRADED = "degraded"  # responded but with elevated latency or partial loss
    TIMEOUT = "timeout"  # no response at all (classic "silent hop" -- may just be an ICMP-rate-limiting router)
    UNKNOWN = "unknown"  # hop identified (e.g. via topology graph) but not independently probed


class PathTrace(Base):
    """One hop-by-hop path trace run (NetPath-style route visualization).

    Two hop sources, tried in order, mirroring how topology_service already
    ranks its own edge sources by trust:

      1. A real `traceroute`/`tracepath` run (see path_trace_service), when
         that binary is available in this runtime -- actual L3 hop-by-hop
         RTT/loss, the same data NetPath shows.
      2. A fallback that walks the existing topology graph
         (topology_service.build_topology, built from confirmed LLDP/CDP/
         GNS3 adjacency) via BFS from source to target device, and probes
         each intermediate *managed* device individually with the same
         reachability check used for Device.status
         (reachability_service.is_reachable) plus an ICMP RTT sample --
         useful precisely in containerized/restricted environments where
         raw traceroute either isn't installed or lacks the capabilities
         it needs, which is the common case for this app's own runtime.

    `hop_source` on the trace (not stored per-hop, since a single run uses
    one method throughout) records which of the two produced this result,
    so the UI can label a topology-derived trace as such rather than
    implying it has real L3 RTT data at every hop.
    """

    __tablename__ = "path_traces"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    source_device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True, index=True)
    source_ip = Column(String, nullable=False)

    # Target can be a managed device (target_device_id set) or an
    # arbitrary IP/hostname typed in by the operator (target_device_id
    # NULL) -- NetPath-style tracing is just as useful toward an
    # unmanaged Internet endpoint as toward another inventory device.
    target_device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True, index=True)
    target_input = Column(String, nullable=False)  # exactly what the operator typed/selected
    target_resolved_ip = Column(String, nullable=True)

    hop_source = Column(String, nullable=False, default="topology")  # "mtr" | "traceroute" | "topology"
    status = Column(Enum(PathTraceStatus), nullable=False, default=PathTraceStatus.PARTIAL)
    total_hops = Column(Integer, nullable=False, default=0)
    reached_target = Column(Boolean, nullable=False, default=False, server_default="false")

    requested_by = Column(String, nullable=True)  # user email, mirrors AuditLog's actor convention
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    hops = relationship(
        "PathHop", back_populates="trace", order_by="PathHop.hop_index", cascade="all, delete-orphan"
    )


class PathHop(Base):
    """One hop within a PathTrace, ordered by `hop_index` (1-based, matching
    conventional traceroute TTL numbering)."""

    __tablename__ = "path_hops"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    path_trace_id = Column(UUID(as_uuid=True), ForeignKey("path_traces.id"), nullable=False, index=True)

    hop_index = Column(Integer, nullable=False)
    ip_address = Column(String, nullable=True)  # NULL for a fully unresponsive hop (timeout, address unknown)
    hostname = Column(String, nullable=True)

    # Best-effort link back to inventory if this hop's IP matches a known
    # Device -- lets the UI render a hop as a clickable managed device
    # (with its live health color) instead of a bare IP.
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True, index=True)

    rtt_ms = Column(Float, nullable=True)  # average RTT for this hop, ms (mtr's "Avg" column when available)
    packet_loss_pct = Column(Float, nullable=True)  # 0-100
    status = Column(Enum(HopStatus), nullable=False, default=HopStatus.UNKNOWN)

    # Extra per-hop statistics from a real mtr run (NULL for topology-
    # fallback hops, which only ever have a single ICMP sample and so
    # can't populate best/worst/stddev/sent).
    sent = Column(Integer, nullable=True)  # probe cycles sent for this hop
    last_rtt_ms = Column(Float, nullable=True)
    best_rtt_ms = Column(Float, nullable=True)
    worst_rtt_ms = Column(Float, nullable=True)
    stddev_rtt_ms = Column(Float, nullable=True)

    trace = relationship("PathTrace", back_populates="hops")
