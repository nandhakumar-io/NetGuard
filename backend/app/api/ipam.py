"""IPAM: subnet inventory, utilization, free-IP lookup, and static-
assignment conflict detection. See app.services.ipam_service for the
address-math core; this module is CRUD + response shaping on top of it.
"""
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.subnet import IPAddressState, IPReservation, Subnet
from app.models.user import UserRole
from app.schemas.subnet import (
    ConflictReport,
    FreeIPResult,
    IPConflict,
    IPReservationCreate,
    IPReservationRead,
    SubnetAddressEntry,
    SubnetCreate,
    SubnetFingerprintResult,
    SubnetRead,
    SubnetScanResult,
    SubnetUpdate,
)
from app.services import ipam_service

router = APIRouter(prefix="/ipam", tags=["ipam"])

# Same posture as devices/device-groups: everyone authenticated can view
# the address plan, only Network Admins create/edit/delete subnets and
# reservations.
IPAM_MANAGER_ROLES = require_roles(UserRole.NETWORK_ADMIN, UserRole.NETWORK_ENGINEER)


def _tags_to_json(tags: list[str] | None) -> str | None:
    return json.dumps(tags) if tags else None


def _tags_from_json(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return []


def _to_read(db: Session, subnet: Subnet) -> SubnetRead:
    util = ipam_service.subnet_utilization(db, subnet)
    return SubnetRead(
        id=subnet.id,
        cidr=subnet.cidr,
        name=subnet.name,
        vlan_id=subnet.vlan_id,
        site=subnet.site,
        description=subnet.description,
        tags=_tags_from_json(subnet.tags),
        created_at=subnet.created_at,
        updated_at=subnet.updated_at,
        auto_rescan_enabled=subnet.auto_rescan_enabled,
        rescan_interval_hours=subnet.rescan_interval_hours,
        **util,
    )


@router.get("", response_model=list[SubnetRead])
def list_subnets(site: str | None = None, db: Session = Depends(get_db), _=Depends(get_current_user)):
    q = db.query(Subnet)
    if site:
        q = q.filter(Subnet.site == site)
    subnets = q.order_by(Subnet.cidr.asc()).all()
    return [_to_read(db, s) for s in subnets]


@router.post("", response_model=SubnetRead, status_code=201)
def create_subnet(payload: SubnetCreate, db: Session = Depends(get_db), _=Depends(IPAM_MANAGER_ROLES)):
    if db.query(Subnet).filter(Subnet.cidr == payload.cidr).first():
        raise HTTPException(status_code=409, detail=f"Subnet {payload.cidr} already exists")

    overlapping = [s for s in db.query(Subnet).all() if _networks_overlap(payload.cidr, s.cidr)]
    if overlapping:
        raise HTTPException(
            status_code=409,
            detail=f"{payload.cidr} overlaps with existing subnet {overlapping[0].cidr}",
        )

    subnet = Subnet(
        cidr=payload.cidr,
        name=payload.name,
        vlan_id=payload.vlan_id,
        site=payload.site,
        description=payload.description,
        tags=_tags_to_json(payload.tags),
        auto_rescan_enabled=payload.auto_rescan_enabled,
        rescan_interval_hours=payload.rescan_interval_hours,
    )
    db.add(subnet)
    db.commit()
    db.refresh(subnet)
    return _to_read(db, subnet)


def _networks_overlap(cidr_a: str, cidr_b: str) -> bool:
    import ipaddress

    a = ipaddress.ip_network(cidr_a, strict=False)
    b = ipaddress.ip_network(cidr_b, strict=False)
    return a.overlaps(b)


@router.get("/conflicts", response_model=ConflictReport)
def get_conflicts(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Fleet-wide: every IP address currently claimed by more than one
    device, regardless of whether a subnet has been defined for it.
    """
    return ConflictReport(conflicts=[IPConflict(**c) for c in ipam_service.fleet_conflicts(db)])


@router.get("/lookup")
def lookup_ip(ip_address: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Which managed subnet (if any) an address belongs to, plus whether
    it's already in use -- handy for "is 10.20.30.55 free before I hand
    it out" without knowing the subnet id up front.
    """
    subnet = ipam_service.find_subnet_for_ip(db, ip_address)
    conflict_devices = ipam_service.check_conflict(db, ip_address)
    return {
        "ip_address": ip_address,
        "subnet_id": subnet.id if subnet else None,
        "subnet_cidr": subnet.cidr if subnet else None,
        "in_use_by": [{"device_id": d.id, "hostname": d.hostname} for d in conflict_devices],
    }


@router.get("/{subnet_id}", response_model=SubnetRead)
def get_subnet(subnet_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    subnet = db.get(Subnet, subnet_id)
    if not subnet:
        raise HTTPException(status_code=404, detail="Subnet not found")
    return _to_read(db, subnet)


@router.patch("/{subnet_id}", response_model=SubnetRead)
def update_subnet(
    subnet_id: uuid.UUID, payload: SubnetUpdate, db: Session = Depends(get_db), _=Depends(IPAM_MANAGER_ROLES)
):
    subnet = db.get(Subnet, subnet_id)
    if not subnet:
        raise HTTPException(status_code=404, detail="Subnet not found")

    updates = payload.model_dump(exclude_unset=True)
    if "tags" in updates:
        updates["tags"] = _tags_to_json(payload.tags)
    for field, value in updates.items():
        setattr(subnet, field, value)

    db.commit()
    db.refresh(subnet)
    return _to_read(db, subnet)


@router.delete("/{subnet_id}", status_code=204)
def delete_subnet(subnet_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(IPAM_MANAGER_ROLES)):
    subnet = db.get(Subnet, subnet_id)
    if not subnet:
        raise HTTPException(status_code=404, detail="Subnet not found")
    db.delete(subnet)  # cascades to ip_reservations
    db.commit()


@router.get("/{subnet_id}/addresses", response_model=list[SubnetAddressEntry])
def list_addresses(subnet_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    subnet = db.get(Subnet, subnet_id)
    if not subnet:
        raise HTTPException(status_code=404, detail="Subnet not found")
    try:
        rows = ipam_service.list_addresses(db, subnet)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return [SubnetAddressEntry(**r) for r in rows]


@router.post("/{subnet_id}/scan", response_model=SubnetScanResult)
def scan_subnet(subnet_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(IPAM_MANAGER_ROLES)):
    """Runs a live nmap ping-sweep over this subnet and replaces its
    stored scan results, so utilization/the address table reflect who's
    actually on the wire right now -- not just switches/routers NetGuard
    manages. See app.services.ipam_service.scan_subnet's docstring for
    why this is the only one of IPAM's four "used" signals that can see
    an unmanaged endpoint (PC, printer, phone, IoT device).

    Gated behind the same Network Admin/Engineer roles as other
    IPAM-mutating actions, and behind an explicit button click in the UI
    -- this touches the actual network (albeit just a ping sweep), so it
    should never fire silently on page load.
    """
    subnet = db.get(Subnet, subnet_id)
    if not subnet:
        raise HTTPException(status_code=404, detail="Subnet not found")
    try:
        result = ipam_service.scan_subnet(db, subnet)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return SubnetScanResult(subnet_id=subnet.id, **result)


@router.post("/{subnet_id}/fingerprint", response_model=SubnetFingerprintResult)
def fingerprint_subnet(subnet_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(IPAM_MANAGER_ROLES)):
    """Runs a live `nmap -O` OS/device-type fingerprint pass over this
    subnet's live hosts. Separate endpoint (and separate button) from
    /scan on purpose -- this needs a raw socket (root, or
    CAP_NET_RAW+CAP_NET_ADMIN) on the NetGuard backend host, which a
    plain ping-sweep never does, so it may simply 503 in deployments
    that haven't granted that capability. See
    app.services.ipam_service.fingerprint_subnet's docstring for the
    deployment options.
    """
    subnet = db.get(Subnet, subnet_id)
    if not subnet:
        raise HTTPException(status_code=404, detail="Subnet not found")
    try:
        result = ipam_service.fingerprint_subnet(db, subnet)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return SubnetFingerprintResult(subnet_id=subnet.id, **result)


@router.get("/{subnet_id}/free-ip", response_model=FreeIPResult)
def free_ip(subnet_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    subnet = db.get(Subnet, subnet_id)
    if not subnet:
        raise HTTPException(status_code=404, detail="Subnet not found")
    try:
        ip = ipam_service.find_free_ip(db, subnet)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return FreeIPResult(
        subnet_id=subnet.id,
        cidr=subnet.cidr,
        free_ip=ip,
        message=None if ip else "No free addresses remain in this subnet",
    )


@router.get("/{subnet_id}/reservations", response_model=list[IPReservationRead])
def list_reservations(subnet_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    subnet = db.get(Subnet, subnet_id)
    if not subnet:
        raise HTTPException(status_code=404, detail="Subnet not found")
    return (
        db.query(IPReservation)
        .filter(IPReservation.subnet_id == subnet_id)
        .order_by(IPReservation.ip_address.asc())
        .all()
    )


@router.post("/{subnet_id}/reservations", response_model=IPReservationRead, status_code=201)
def create_reservation(
    subnet_id: uuid.UUID,
    payload: IPReservationCreate,
    db: Session = Depends(get_db),
    _=Depends(IPAM_MANAGER_ROLES),
):
    subnet = db.get(Subnet, subnet_id)
    if not subnet:
        raise HTTPException(status_code=404, detail="Subnet not found")

    try:
        state = IPAddressState(payload.state)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="state must be one of: reserved, gateway, broadcast, network",
        )
    if state == IPAddressState.ASSIGNED:
        raise HTTPException(
            status_code=400, detail="assigned is derived from device inventory, not settable directly"
        )

    import ipaddress

    net = ipam_service.network_for(subnet)
    try:
        addr = ipaddress.ip_address(payload.ip_address)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid IP address")
    if addr not in net:
        raise HTTPException(status_code=400, detail=f"{payload.ip_address} is not inside {subnet.cidr}")

    existing_devices = ipam_service.check_conflict(db, payload.ip_address)
    if existing_devices:
        raise HTTPException(
            status_code=409,
            detail=f"{payload.ip_address} is already assigned to device {existing_devices[0].hostname}",
        )
    if db.query(IPReservation).filter(
        IPReservation.subnet_id == subnet_id, IPReservation.ip_address == payload.ip_address
    ).first():
        raise HTTPException(status_code=409, detail=f"{payload.ip_address} is already reserved in this subnet")

    reservation = IPReservation(subnet_id=subnet_id, ip_address=payload.ip_address, state=state, note=payload.note)
    db.add(reservation)
    db.commit()
    db.refresh(reservation)
    return reservation


@router.delete("/{subnet_id}/reservations/{reservation_id}", status_code=204)
def delete_reservation(
    subnet_id: uuid.UUID, reservation_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(IPAM_MANAGER_ROLES)
):
    reservation = db.get(IPReservation, reservation_id)
    if not reservation or reservation.subnet_id != subnet_id:
        raise HTTPException(status_code=404, detail="Reservation not found")
    db.delete(reservation)
    db.commit()
