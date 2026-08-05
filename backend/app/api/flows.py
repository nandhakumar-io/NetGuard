"""Traffic Analysis API -- NetFlow/sFlow/IPFIX-derived top talkers, top
conversations, protocol breakdown, bandwidth-over-time, and exporter
status. See app.services.flow_service for ingestion + query logic.

Known limitation: for NetFlow v9 / IPFIX exporters, only common standard
fields (addresses, ports, protocol, bytes/packets, AS numbers,
interfaces, timestamps) are decoded -- vendor-specific extension fields
are skipped, so an exporter that leans heavily on vendor extensions
still shows correct basic 5-tuple/byte-count data here, just nothing
beyond that. See app.services.flow_service's module docstring for
detail.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.schemas.flow import (
    BandwidthPoint,
    FlowExporter,
    ProtocolShare,
    TopConversation,
    TopTalker,
    TrafficSummary,
)
from app.services import flow_service

router = APIRouter(prefix="/flows", tags=["traffic-analysis"])


@router.get("/top-talkers", response_model=list[TopTalker])
def get_top_talkers(
    minutes: int = Query(60, ge=1, le=10080),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    return flow_service.top_talkers(db, minutes=minutes, limit=limit)


@router.get("/top-conversations", response_model=list[TopConversation])
def get_top_conversations(
    minutes: int = Query(60, ge=1, le=10080),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    return flow_service.top_conversations(db, minutes=minutes, limit=limit)


@router.get("/protocol-breakdown", response_model=list[ProtocolShare])
def get_protocol_breakdown(
    minutes: int = Query(60, ge=1, le=10080),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    return flow_service.protocol_breakdown(db, minutes=minutes)


@router.get("/bandwidth-timeseries", response_model=list[BandwidthPoint])
def get_bandwidth_timeseries(
    minutes: int = Query(60, ge=1, le=10080),
    bucket_minutes: int = Query(5, ge=1, le=60),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    return flow_service.bandwidth_timeseries(db, minutes=minutes, bucket_minutes=bucket_minutes)


@router.get("/exporters", response_model=list[FlowExporter])
def get_exporters(
    minutes: int = Query(60, ge=1, le=10080),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    return flow_service.exporters(db, minutes=minutes)


@router.get("/summary", response_model=TrafficSummary)
def get_traffic_summary(
    minutes: int = Query(60, ge=1, le=10080),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Single call for the Traffic Analysis page's initial load -- avoids
    5 separate round trips before the page has anything to show.
    """
    return TrafficSummary(
        window_minutes=minutes,
        top_talkers=flow_service.top_talkers(db, minutes=minutes, limit=10),
        top_conversations=flow_service.top_conversations(db, minutes=minutes, limit=10),
        protocol_breakdown=flow_service.protocol_breakdown(db, minutes=minutes),
        bandwidth_timeseries=flow_service.bandwidth_timeseries(db, minutes=minutes),
        exporters=flow_service.exporters(db, minutes=minutes),
    )