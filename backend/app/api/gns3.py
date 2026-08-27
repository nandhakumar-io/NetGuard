"""GNS3 Lab Integration API.

Surfaces the already-implemented controller client (app.services.gns3_service)
and one-time console bootstrap (app.services.lab_bootstrap_service) so a
GNS3 topology can be treated as a disposable test network: start/stop nodes,
bootstrap management IP + SSH, and sync them into Device inventory. After
bootstrap every other NetGuard service (deploy, health monitor, rollback,
drift) talks to the node over normal SSH exactly like physical hardware.

All endpoints are gated on settings.GNS3_ENABLED -- when disabled they
return 503 so the frontend can hide the Lab section cleanly.
"""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.device import Device, DeviceStatus, DeviceVendor
from app.models.user import User, UserRole
from app.schemas.gns3 import (
    GNS3BootstrapRequest,
    GNS3BootstrapResponse,
    GNS3NodeActionResponse,
    GNS3NodeSummary,
    GNS3ProjectSummary,
    GNS3StatusResponse,
    GNS3SyncNodeResult,
    GNS3SyncRequest,
    GNS3SyncResponse,
)
from app.services import audit_service, gns3_service, lab_bootstrap_service
from app.services.gns3_service import GNS3Error

router = APIRouter(prefix="/gns3", tags=["gns3"])

LAB_ADMIN_ROLES = require_roles(UserRole.NETWORK_ADMIN)


def _ensure_enabled() -> None:
    if not settings.GNS3_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GNS3 integration is disabled. Set GNS3_ENABLED=true in the environment to use the lab.",
        )


def _raise_gns3(exc: GNS3Error) -> None:
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


def _vendor_enum(guess: str) -> DeviceVendor:
    try:
        return DeviceVendor(guess)
    except ValueError:
        return DeviceVendor.CISCO


def _slug_cred_ref(node_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", node_name.strip()).strip("-").lower() or "node"
    return f"gns3-{slug}"


def _node_to_summary(node: dict, device: Device | None = None) -> GNS3NodeSummary:
    host, port, ctype = gns3_service.node_console_info(node)
    return GNS3NodeSummary(
        node_id=str(node.get("node_id") or node.get("id") or ""),
        name=node.get("name") or "unknown",
        node_type=node.get("node_type"),
        status=node.get("status"),
        console_host=host,
        console_port=port,
        console_type=ctype,
        vendor_guess=gns3_service.guess_vendor(node),
        synced=device is not None,
        device_id=device.id if device else None,
        bootstrapped=bool(device.bootstrapped) if device else False,
        management_ip=device.ip_address if device and device.bootstrapped else None,
    )


def _find_linked_device(db: Session, project_id: str, node_id: str) -> Device | None:
    return (
        db.query(Device)
        .filter(
            Device.gns3_project_id == project_id,
            Device.gns3_node_id == node_id,
        )
        .first()
    )


@router.get("/status", response_model=GNS3StatusResponse)
def gns3_status(_=Depends(get_current_user)):
    """Whether the lab integration is enabled and the controller is reachable."""
    if not settings.GNS3_ENABLED:
        return GNS3StatusResponse(
            enabled=False,
            reachable=False,
            controller_url=settings.GNS3_BASE_URL,
            detail="GNS3 integration is disabled (GNS3_ENABLED=false).",
        )
    try:
        version_payload = gns3_service.check_status()
        version = (
            version_payload.get("version")
            or version_payload.get("local")
            or str(version_payload)
        )
        return GNS3StatusResponse(
            enabled=True,
            reachable=True,
            version=str(version),
            controller_url=settings.GNS3_BASE_URL,
        )
    except GNS3Error as exc:
        return GNS3StatusResponse(
            enabled=True,
            reachable=False,
            controller_url=settings.GNS3_BASE_URL,
            detail=str(exc),
        )


@router.get("/projects", response_model=list[GNS3ProjectSummary])
def list_projects(_=Depends(get_current_user)):
    _ensure_enabled()
    try:
        projects = gns3_service.list_projects()
    except GNS3Error as exc:
        _raise_gns3(exc)
    return [
        GNS3ProjectSummary(
            project_id=str(p.get("project_id") or p.get("id") or ""),
            name=p.get("name") or "unnamed",
            status=p.get("status"),
            filename=p.get("filename"),
        )
        for p in projects
    ]


@router.post("/projects/{project_id}/open", response_model=GNS3ProjectSummary)
def open_project(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(LAB_ADMIN_ROLES),
):
    """Open (load) a GNS3 project so its nodes can be started."""
    _ensure_enabled()
    try:
        project = gns3_service.open_project(project_id)
    except GNS3Error as exc:
        _raise_gns3(exc)
    audit_service.record_event(
        db,
        actor=user.email, tenant_id=user.tenant_id,
        action="GNS3 Open Project",
        result="Success",
        detail=f"project_id={project_id} name={project.get('name')}",
    )
    return GNS3ProjectSummary(
        project_id=str(project.get("project_id") or project_id),
        name=project.get("name") or "unnamed",
        status=project.get("status"),
        filename=project.get("filename"),
    )


@router.get("/projects/{project_id}/nodes", response_model=list[GNS3NodeSummary])
def list_nodes(project_id: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    _ensure_enabled()
    try:
        nodes = gns3_service.list_nodes(project_id)
    except GNS3Error as exc:
        _raise_gns3(exc)

    summaries: list[GNS3NodeSummary] = []
    for node in nodes:
        node_id = str(node.get("node_id") or node.get("id") or "")
        device = _find_linked_device(db, project_id, node_id) if node_id else None
        summaries.append(_node_to_summary(node, device))
    return summaries


@router.post(
    "/projects/{project_id}/nodes/{node_id}/start",
    response_model=GNS3NodeActionResponse,
)
def start_node(
    project_id: str,
    node_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(LAB_ADMIN_ROLES),
):
    _ensure_enabled()
    try:
        node = gns3_service.start_node(project_id, node_id)
    except GNS3Error as exc:
        _raise_gns3(exc)
    audit_service.record_event(
        db,
        actor=user.email, tenant_id=user.tenant_id,
        action="GNS3 Start Node",
        result="Success",
        detail=f"project={project_id} node={node_id}",
    )
    return GNS3NodeActionResponse(
        project_id=project_id,
        node_id=node_id,
        action="start",
        status=node.get("status") if isinstance(node, dict) else None,
    )


@router.post(
    "/projects/{project_id}/nodes/{node_id}/stop",
    response_model=GNS3NodeActionResponse,
)
def stop_node(
    project_id: str,
    node_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(LAB_ADMIN_ROLES),
):
    _ensure_enabled()
    try:
        node = gns3_service.stop_node(project_id, node_id)
    except GNS3Error as exc:
        _raise_gns3(exc)
    audit_service.record_event(
        db,
        actor=user.email, tenant_id=user.tenant_id,
        action="GNS3 Stop Node",
        result="Success",
        detail=f"project={project_id} node={node_id}",
    )
    return GNS3NodeActionResponse(
        project_id=project_id,
        node_id=node_id,
        action="stop",
        status=node.get("status") if isinstance(node, dict) else None,
    )


@router.post(
    "/projects/{project_id}/nodes/{node_id}/bootstrap",
    response_model=GNS3BootstrapResponse,
)
def bootstrap_node(
    project_id: str,
    node_id: str,
    payload: GNS3BootstrapRequest,
    db: Session = Depends(get_db),
    user: User = Depends(LAB_ADMIN_ROLES),
):
    """Push management IP + SSH onto a bare Cisco IOS/IOSv node via its
    GNS3 console, then optionally upsert a Device inventory row so the rest
    of NetGuard can deploy against it like any other device.
    """
    _ensure_enabled()
    try:
        node = gns3_service.get_node(project_id, node_id)
    except GNS3Error as exc:
        _raise_gns3(exc)

    console_host, console_port, console_type = gns3_service.node_console_info(node)
    if not console_host or not console_port:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Node '{node.get('name')}' has no console port assigned "
                f"(console_type={console_type}). Only nodes with a telnet console can be bootstrapped."
            ),
        )
    if (console_type or "").lower() == "vnc":
        raise HTTPException(
            status_code=400,
            detail="Node console type is VNC; bootstrap only supports telnet consoles (Cisco IOS).",
        )

    hostname = payload.hostname or node.get("name") or f"gns3-{node_id[:8]}"
    result = lab_bootstrap_service.bootstrap_cisco_ios(
        console_host=console_host,
        console_port=console_port,
        mgmt_interface=payload.mgmt_interface,
        mgmt_ip=payload.mgmt_ip,
        mgmt_subnet_mask=payload.mgmt_subnet_mask,
        ssh_username=payload.ssh_username,
        ssh_password=payload.ssh_password,
        enable_password=payload.enable_password,
        hostname=hostname,
        ready_timeout_seconds=settings.GNS3_CONSOLE_READY_TIMEOUT_SECONDS,
    )

    if not result.success:
        audit_service.record_event(
            db,
            actor=user.email, tenant_id=user.tenant_id,
            action="GNS3 Bootstrap",
            result="Failed",
            device_hostname=hostname,
            detail=result.error,
        )
        return GNS3BootstrapResponse(
            success=False,
            output=result.output,
            error=result.error,
            message=result.error or "Bootstrap failed",
        )

    device_id: uuid.UUID | None = None
    cred_ref = payload.ssh_credential_ref or _slug_cred_ref(hostname)
    device = _find_linked_device(db, project_id, node_id)

    if payload.create_device and device is None:
        device = db.query(Device).filter(Device.hostname == hostname).first()

    vendor = _vendor_enum(gns3_service.guess_vendor(node))
    if device is None and payload.create_device:
        device = Device(
            hostname=hostname,
            ip_address=payload.mgmt_ip,
            vendor=vendor,
            site=payload.site,
            device_type=node.get("node_type") or "router",
            status=DeviceStatus.ONLINE,
            ssh_username=payload.ssh_username,
            ssh_credential_ref=cred_ref,
            is_simulated=True,
            lab_provider="gns3",
            gns3_project_id=project_id,
            gns3_node_id=node_id,
            console_host=console_host,
            console_port=console_port,
            console_type=console_type or "telnet",
            bootstrapped=True,
        )
        db.add(device)
    elif device is not None:
        device.ip_address = payload.mgmt_ip
        device.ssh_username = payload.ssh_username
        device.ssh_credential_ref = cred_ref
        device.is_simulated = True
        device.lab_provider = "gns3"
        device.gns3_project_id = project_id
        device.gns3_node_id = node_id
        device.console_host = console_host
        device.console_port = console_port
        device.console_type = console_type or "telnet"
        device.bootstrapped = True
        device.status = DeviceStatus.ONLINE
        if payload.site:
            device.site = payload.site

    if device is not None:
        db.commit()
        db.refresh(device)
        device_id = device.id

    audit_service.record_event(
        db,
        actor=user.email, tenant_id=user.tenant_id,
        action="GNS3 Bootstrap",
        result="Success",
        device_hostname=hostname,
        detail=f"mgmt_ip={payload.mgmt_ip} project={project_id} node={node_id}",
    )

    cred_key = re.sub(
        r"[^A-Za-z0-9]",
        "_",
        (payload.ssh_credential_ref or _slug_cred_ref(hostname)).upper(),
    )
    return GNS3BootstrapResponse(
        success=True,
        output=result.output,
        device_id=device_id,
        hostname=hostname,
        management_ip=payload.mgmt_ip,
        message=(
            f"Node '{hostname}' is SSH-reachable at {payload.mgmt_ip}. "
            f"Ensure NETGUARD_CRED_{cred_key} is set in the environment "
            f"so deployments can authenticate."
        ),
    )


@router.post("/projects/{project_id}/sync", response_model=GNS3SyncResponse)
def sync_project(
    project_id: str,
    payload: GNS3SyncRequest,
    db: Session = Depends(get_db),
    user: User = Depends(LAB_ADMIN_ROLES),
):
    """Create/update Device rows for every node in a GNS3 project."""
    _ensure_enabled()
    try:
        gns3_service.open_project(project_id)
        nodes = gns3_service.list_nodes(project_id)
    except GNS3Error as exc:
        _raise_gns3(exc)

    created = updated = skipped = 0
    results: list[GNS3SyncNodeResult] = []

    for node in nodes:
        node_id = str(node.get("node_id") or node.get("id") or "")
        name = node.get("name") or f"gns3-{node_id[:8]}"
        if not node_id:
            skipped += 1
            results.append(
                GNS3SyncNodeResult(
                    node_id="",
                    name=name,
                    action="skipped",
                    detail="Node missing id",
                )
            )
            continue

        console_host, console_port, console_type = gns3_service.node_console_info(node)
        vendor = _vendor_enum(gns3_service.guess_vendor(node))
        device = _find_linked_device(db, project_id, node_id)

        if device is None:
            existing = db.query(Device).filter(Device.hostname == name).first()
            if existing and not existing.is_simulated:
                skipped += 1
                results.append(
                    GNS3SyncNodeResult(
                        node_id=node_id,
                        name=name,
                        action="skipped",
                        device_id=existing.id,
                        detail="Hostname already used by a non-lab device",
                    )
                )
                continue

            if existing and existing.is_simulated:
                device = existing
            else:
                cred_ref = payload.default_ssh_credential_ref or _slug_cred_ref(name)
                device = Device(
                    hostname=name,
                    ip_address=payload.placeholder_ip,
                    vendor=vendor,
                    site=payload.site,
                    device_type=node.get("node_type") or "router",
                    status=DeviceStatus.UNKNOWN,
                    ssh_username=payload.default_ssh_username,
                    ssh_credential_ref=cred_ref,
                    is_simulated=True,
                    lab_provider="gns3",
                    gns3_project_id=project_id,
                    gns3_node_id=node_id,
                    console_host=console_host,
                    console_port=console_port,
                    console_type=console_type or "telnet",
                    bootstrapped=False,
                )
                db.add(device)
                db.flush()
                created += 1
                results.append(
                    GNS3SyncNodeResult(
                        node_id=node_id,
                        name=name,
                        action="created",
                        device_id=device.id,
                    )
                )
                continue

        device.lab_provider = "gns3"
        device.is_simulated = True
        device.gns3_project_id = project_id
        device.gns3_node_id = node_id
        device.console_host = console_host
        device.console_port = console_port
        device.console_type = console_type or "telnet"
        device.vendor = vendor
        if payload.site:
            device.site = payload.site
        if payload.default_ssh_username and not device.bootstrapped:
            device.ssh_username = payload.default_ssh_username
        if payload.default_ssh_credential_ref and not device.bootstrapped:
            device.ssh_credential_ref = payload.default_ssh_credential_ref
        updated += 1
        results.append(
            GNS3SyncNodeResult(
                node_id=node_id,
                name=name,
                action="updated",
                device_id=device.id,
            )
        )

    db.commit()
    audit_service.record_event(
        db,
        actor=user.email, tenant_id=user.tenant_id,
        action="GNS3 Sync Project",
        result="Success",
        detail=f"project={project_id} created={created} updated={updated} skipped={skipped}",
    )
    return GNS3SyncResponse(
        project_id=project_id,
        created=created,
        updated=updated,
        skipped=skipped,
        results=results,
    )
