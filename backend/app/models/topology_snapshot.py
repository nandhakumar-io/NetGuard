import uuid

from sqlalchemy import Column, DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class TopologySnapshot(Base):
    """A point-in-time capture of the topology graph app.services.
    topology_service.build_topology() produces, stored so the Topology
    view can answer "what changed in the network graph since last
    week" -- the live view alone is a snapshot with no history, so
    without this table there was no way to see a node/edge appear or
    disappear over time the way Drift already does for config changes.

    Nodes/edges are stored as JSON (node id / edge src-dst pairs plus
    just enough metadata to render a human-readable diff line) rather
    than as normalized rows -- a snapshot is written wholesale and read
    wholesale (compared two-at-a-time in app.services.topology_service.
    diff_snapshots), never queried by individual node, so normalizing it
    would only add write-amplification for no query benefit.
    """

    __tablename__ = "topology_snapshots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    nodes_json = Column(Text, nullable=False)  # JSON list of {id, hostname, ip_address}
    edges_json = Column(Text, nullable=False)  # JSON list of {source, target, link_type}

    node_count = Column(Integer, nullable=False)
    edge_count = Column(Integer, nullable=False)

    captured_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
