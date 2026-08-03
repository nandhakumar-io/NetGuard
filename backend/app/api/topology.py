from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.schemas.topology import TopologyResponse
from app.services import topology_service

router = APIRouter(prefix="/topology", tags=["topology"])


@router.get("", response_model=TopologyResponse)
def get_topology(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Fleet-wide network topology: one node per device. Edges come from
    two sources, merged in app.services.topology_service.build_topology:
    real LLDP/CDP-confirmed adjacency persisted from SNMP Discovery runs
    (edge.link_source == "lldp"/"cdp"), and, where no discovery data
    exists for a pair, a same-subnet inference from each device's latest
    config snapshot (edge.link_source == "subnet").

    Available to any authenticated user (read-only), consistent with the
    other dashboard/summary endpoints in this API.
    """
    graph = topology_service.build_topology(db)
    return TopologyResponse(
        nodes=[
            {
                "id": n.id,
                "hostname": n.hostname,
                "ip_address": n.ip_address,
                "vendor": n.vendor,
                "site": n.site,
                "device_type": n.device_type,
                "status": n.status,
                "flagged_unstable": n.flagged_unstable,
                "has_config_on_file": n.has_config_on_file,
            }
            for n in graph.nodes
        ],
        edges=[
            {
                "source": e.source,
                "target": e.target,
                "subnet": e.subnet,
                "source_ip": e.source_ip,
                "target_ip": e.target_ip,
                "link_source": e.link_source,
                "local_port": e.local_port,
                "neighbor_port": e.neighbor_port,
            }
            for e in graph.edges
        ],
    )