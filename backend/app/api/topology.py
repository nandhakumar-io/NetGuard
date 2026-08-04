from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.schemas.topology import TopologyResponse
from app.services import topology_service

router = APIRouter(prefix="/topology", tags=["topology"])


@router.get("", response_model=TopologyResponse)
def get_topology(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Fleet-wide network topology: one node per device, edges inferred
    from shared interface subnets found in each device's latest config
    snapshot (see app.services.topology_service for how links are
    derived -- there's no CDP/LLDP discovery in NetGuard, so this is the
    data-grounded substitute).

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
                "health_color": n.health_color,
                "health_score": n.health_score,
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
            }
            for e in graph.edges
        ],
    )