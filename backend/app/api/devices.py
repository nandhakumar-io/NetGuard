import datetime
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core import vm_client
from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.alert import Alert
from app.models.alert_snooze import AlertSnooze
from app.models.audit_log import AuditLog
from app.models.change_request import ChangeRequest
from app.models.config_drift import ConfigDrift
from app.models.deployment import Deployment, DeploymentLog, HealthCheckResult
from app.models.device import Device
from app.models.device_metric import DeviceMetric
from app.models.device_status_history import DeviceStatusHistory
from app.models.discovered_neighbor import DiscoveredNeighbor
from app.models.firmware_upgrade import FirmwareUpgrade
from app.models.flow_record import FlowRecord
from app.models.golden_config import GoldenConfig
from app.models.interface_alert_config import InterfaceAlertConfig
from app.models.interface_metric import InterfaceMetric
from app.models.interface_status import InterfaceStatus
from app.models.maintenance_window import MaintenanceWindow
from app.models.path_trace import PathHop, PathTrace
from app.models.protocol_operation import ProtocolOperation
from app.models.recurring_maintenance_schedule import RecurringMaintenanceSchedule
from app.models.snapshot import ConfigSnapshot
from app.models.syslog_message import SyslogMessage
from app.models.user import User, UserRole
from app.schemas.device import (
    BulkDeviceAction,
    BulkDeviceActionRequest,
    BulkDeviceActionResult,
    DeviceCreate,
    DeviceCsvImportResult,
    DeviceDiscoveryResult,
    DeviceRead,
    DeviceUpdate,
    SnmpCredentialsUpdate,
    SnmpTestResult,
    SshCredentialsUpdate,
    SshTestResult,
)
from app.schemas.interface_status import InterfaceCurrentStatus, InterfaceStatusRead
from app.schemas.rollback import (
    PartialRollbackPreviewResponse,
    PartialRollbackRequest,
    RollbackPreviewResponse,
    RollbackRequest,
    RollbackResponse,
    RollbackSection,
    SnapshotSummary,
)
from app.services import (
    audit_service,
    credential_service,
    device_csv_service,
    device_overview_service,
    eol_service,
    event_bus,
    metrics_service,
    netbox_service,
    protocol_manager,
    reachability_service,
    rollback_service,
    snmp_service,
)
from app.services.health_monitor import ALL_CHECKS
from app.tasks import run_deployment_pipeline_task

router = APIRouter(prefix="/devices", tags=["devices"])

# Only Network Administrators manage inventory (FR-2 + RBAC); everyone authenticated can read it.
INVENTORY_MANAGER_ROLES = require_roles(UserRole.NETWORK_ADMIN)


def _poll_snmp_best_effort(db: Session, device: Device) -> None:
    """Fire an immediate SNMP poll via the Celery worker (instead of
    blocking the API thread). This ensures that dashboard telemetry is
    updated smoothly without delaying the device create/update response.
    """
    try:
        from app.tasks import snmp_poll_task
        snmp_poll_task.delay(str(device.id))
    except Exception:
        pass

# Rollback carries the same authority as approving a change (both bypass
# the normal validation/approval queue), so it's gated the same way.
ROLLBACK_ROLES = require_roles(UserRole.NETWORK_ADMIN)


@router.get("", response_model=list[DeviceRead])
def list_devices(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return [DeviceRead.from_device(d) for d in db.query(Device).all()]


@router.post("", response_model=DeviceRead, status_code=201)
def create_device(payload: DeviceCreate, db: Session = Depends(get_db), _=Depends(INVENTORY_MANAGER_ROLES)):
    if db.query(Device).filter(Device.hostname == payload.hostname).first():
        raise HTTPException(status_code=400, detail="Device with this hostname already exists")
    data = payload.model_dump()
    data["tags"] = json.dumps(data["tags"]) if data.get("tags") else None
    data["custom_fields"] = json.dumps(data["custom_fields"]) if data.get("custom_fields") else None
    device = Device(**data)
    db.add(device)
    db.commit()
    db.refresh(device)

    # Don't make the operator wait for the next SNMP_POLL_INTERVAL_SECONDS
    # sweep -- if the device was added with SNMP already configured, poll
    # it right away so the dashboard / device detail page has telemetry as
    # soon as possible.
    if device.supports_snmp:
        _poll_snmp_best_effort(db, device)

    # Same idea for reachability: don't leave a freshly-added device
    # showing UNKNOWN until the next REACHABILITY_POLL_INTERVAL_SECONDS
    # sweep picks it up.
    try:
        reachability_service.check_device(db, device)
    except Exception:  # noqa: BLE001 - best-effort, same policy as the SNMP poll above
        pass

    # New node for every open Topology tab -- reachability_service already
    # pushes a status event if it changed device.status, but a brand new
    # device needs its node to appear at all, which that event alone
    # wouldn't trigger (nothing "changed" from Topology's point of view
    # until this fires).
    event_bus.publish_event(
        "device_added", channel=event_bus.TOPOLOGY_CHANNEL, device_id=str(device.id), hostname=device.hostname
    )

    return DeviceRead.from_device(device)


@router.get("/health-checks/catalog")
def get_health_checks_catalog(_=Depends(get_current_user)):
    """Available post-deployment verification tests (SRS 6.9)."""
    return [
        {"name": k, "description": v["label"]}
        for k, v in ALL_CHECKS.items()
    ]


@router.get("/eol-summary")
def get_eol_summary(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Fleet-wide firmware/hardware EOL rollup for the dashboard badge and
    a dedicated audit view -- how many devices are past vendor End-of-
    Support (the operationally meaningful date -- no more fixes/support
    contracts) or End-of-Life, plus the actual per-device list so an
    operator doesn't have to click through the whole inventory to build
    that list by hand.
    """
    devices = db.query(Device).all()
    eos_devices, eol_devices, unknown_hostnames = [], [], []
    for device in devices:
        status = eol_service.check_device_eol(
            vendor=device.vendor.value if device.vendor else None,
            model=device.model,
            os_version=device.os_version,
        )
        if not status.matched:
            unknown_hostnames.append(device.hostname)
            continue
        entry = {
            "device_id": str(device.id),
            "hostname": device.hostname,
            "platform_label": status.platform_label,
            "eos_date": status.eos_date.isoformat() if status.eos_date else None,
            "eol_date": status.eol_date.isoformat() if status.eol_date else None,
            "days_since_eos": status.days_since_eos,
            "note": status.note,
        }
        if status.is_eol:
            eol_devices.append(entry)
        elif status.is_eos:
            eos_devices.append(entry)

    return {
        "total_devices": len(devices),
        "eos_count": len(eos_devices),
        "eol_count": len(eol_devices),
        "unknown_count": len(unknown_hostnames),
        "eos_devices": eos_devices,
        "eol_devices": eol_devices,
        "unknown_hostnames": unknown_hostnames,
    }


@router.get("/upgrade-plan")
def get_upgrade_plan(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Fleet-wide firmware *upgrade path* view -- distinct from
    /eol-summary above. That endpoint answers "what's already past
    vendor support"; this one answers the earlier, more useful question
    for planning purposes: "what's not on the platform's recommended
    target version yet", regardless of whether it's EOS/EOL. A device
    can be fully supported and still show up here (it's a few releases
    behind), and a device already past EOS is *also* included here if a
    target is known, since it still needs somewhere to go.

    Grouped by (vendor, platform_label, recommended_target_version) so
    an operator sees "23 Catalyst 2960s need to go to 17.9" as one line
    rather than scrolling 23 individual devices.
    """
    devices = db.query(Device).all()
    groups: dict[tuple[str, str, str], dict] = {}
    up_to_date_count = 0
    no_target_hostnames: list[str] = []

    for device in devices:
        status = eol_service.check_device_eol(
            vendor=device.vendor.value if device.vendor else None,
            model=device.model,
            os_version=device.os_version,
        )
        if not status.matched or not status.recommended_target_version:
            no_target_hostnames.append(device.hostname)
            continue
        if not status.needs_upgrade:
            up_to_date_count += 1
            continue

        key = (device.vendor.value if device.vendor else "unknown", status.platform_label, status.recommended_target_version)
        group = groups.setdefault(key, {
            "vendor": key[0],
            "platform_label": key[1],
            "recommended_target_version": key[2],
            "devices": [],
        })
        group["devices"].append({
            "device_id": str(device.id),
            "hostname": device.hostname,
            "current_os_version": device.os_version,
            "is_eos": status.is_eos,
            "is_eol": status.is_eol,
        })

    upgrade_groups = sorted(groups.values(), key=lambda g: len(g["devices"]), reverse=True)
    needs_upgrade_count = sum(len(g["devices"]) for g in upgrade_groups)

    return {
        "total_devices": len(devices),
        "needs_upgrade_count": needs_upgrade_count,
        "up_to_date_count": up_to_date_count,
        "no_target_count": len(no_target_hostnames),
        "upgrade_groups": upgrade_groups,
        "no_target_hostnames": no_target_hostnames,
    }


@router.post("/netbox-sync")
def sync_from_netbox(
    dry_run: bool = Query(False, description="Preview the sync without writing any changes"),
    db: Session = Depends(get_db),
    current_user: User = Depends(INVENTORY_MANAGER_ROLES),
):
    """Pull-syncs the device inventory from NetBox (see
    app.services.netbox_service) -- devices are matched/updated by
    netbox_id (falling back to hostname for a device that predates this
    sync), created if new, and never deleted here (a device removed from
    NetBox stays in NetGuard until someone explicitly removes it, since
    it may still have deployment/drift/audit history worth keeping).
    """
    try:
        result = netbox_service.sync_devices(db, dry_run=dry_run)
    except netbox_service.NetBoxSyncError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if not dry_run and (result["created"] or result["updated"]):
        audit_service.record_event(
            db, actor=current_user.email, action="NetBox Sync", result="Success",
            detail=(
                f"Created {len(result['created'])}, updated {len(result['updated'])}, "
                f"skipped {len(result['skipped'])} (of {result['netbox_devices_seen']} seen)."
            ),
        )
    return result


@router.get("/export")
def export_devices_csv(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Plain-CSV export of the device inventory -- for orgs that don't
    run NetBox (see /netbox-sync above), or just want a spreadsheet to
    bulk-edit and re-import via POST /devices/import.
    """
    devices = db.query(Device).order_by(Device.hostname).all()
    csv_text = device_csv_service.export_devices_csv(devices)
    return StreamingResponse(
        iter([csv_text]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=devices_export.csv"},
    )


@router.post("/import", response_model=DeviceCsvImportResult)
def import_devices_csv(
    file: UploadFile,
    db: Session = Depends(get_db),
    current_user: User = Depends(INVENTORY_MANAGER_ROLES),
):
    """Bulk CSV import -- creates devices whose hostname doesn't already
    exist, updates ones that do (matched by hostname, same convention as
    /netbox-sync). See app.services.device_csv_service.CSV_FIELDS for the
    accepted columns; only `hostname` (and `ip_address` for new rows) are
    required, everything else is optional and left unchanged on update
    if left blank.
    """
    raw = file.file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded CSV")

    try:
        result = device_csv_service.import_devices_csv(db, text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if result["created"] or result["updated"]:
        audit_service.record_event(
            db,
            actor=current_user.email,
            action="CSV Import",
            result="Success",
            detail=(
                f"Created {len(result['created'])}, updated {len(result['updated'])}, "
                f"{len(result['errors'])} row error(s) (of {result['total_rows']} rows)."
            ),
        )
    return result


@router.post("/bulk", response_model=BulkDeviceActionResult)
def bulk_device_action(
    payload: BulkDeviceActionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(INVENTORY_MANAGER_ROLES),
):
    """Applies one action to many devices at once from the Inventory
    page's multi-select toolbar. See BulkDeviceActionRequest docstring
    for the `params` shape each action expects.
    """
    devices = db.query(Device).filter(Device.id.in_(payload.device_ids)).all()
    found = {d.id: d for d in devices}
    missing = set(payload.device_ids) - set(found.keys())
    failed: dict[str, str] = {str(m): "Device not found" for m in missing}
    affected: list[uuid.UUID] = []
    detail: str | None = None
    change_request_id: uuid.UUID | None = None

    if payload.action == BulkDeviceAction.MOVE_GROUP:
        group_id = payload.params.get("group_id")
        if group_id:
            from app.models.device_group import DeviceGroup

            if not db.get(DeviceGroup, group_id):
                raise HTTPException(status_code=400, detail="Target group not found")
        for device in devices:
            device.group_id = group_id
            affected.append(device.id)
        db.commit()
        detail = f"Moved {len(affected)} device(s) to group {group_id or '(none)'}"

    elif payload.action == BulkDeviceAction.ASSIGN_TAGS:
        new_tags = [str(t) for t in payload.params.get("tags", [])]
        mode = payload.params.get("mode", "add")
        for device in devices:
            existing: list[str] = []
            if device.tags:
                try:
                    parsed = json.loads(device.tags)
                    if isinstance(parsed, list):
                        existing = parsed
                except (ValueError, TypeError):
                    existing = []
            merged = sorted(set(new_tags)) if mode == "replace" else sorted(set(existing) | set(new_tags))
            device.tags = json.dumps(merged) if merged else None
            affected.append(device.id)
        db.commit()
        detail = f"{'Replaced' if mode == 'replace' else 'Added'} tags on {len(affected)} device(s)"

    elif payload.action == BulkDeviceAction.SET_LIFECYCLE_STATE:
        from app.models.device import DeviceLifecycleState

        raw_state = payload.params.get("lifecycle_state")
        try:
            state = DeviceLifecycleState(raw_state)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid lifecycle_state: {raw_state}")
        for device in devices:
            device.lifecycle_state = state
            affected.append(device.id)
        db.commit()
        detail = f"Set lifecycle_state={state.value} on {len(affected)} device(s)"

    elif payload.action == BulkDeviceAction.ADD_MAINTENANCE_WINDOW:
        from app.models.maintenance_window import MaintenanceScope, MaintenanceWindow

        name = payload.params.get("name") or "Bulk maintenance window"
        reason = payload.params.get("reason")
        starts_at_raw = payload.params.get("start") or payload.params.get("starts_at")
        ends_at_raw = payload.params.get("end") or payload.params.get("ends_at")
        if not starts_at_raw or not ends_at_raw:
            raise HTTPException(status_code=400, detail="start and end are required")
        try:
            starts_at = datetime.datetime.fromisoformat(str(starts_at_raw))
            ends_at = datetime.datetime.fromisoformat(str(ends_at_raw))
        except ValueError:
            raise HTTPException(status_code=400, detail="start/end must be ISO 8601 datetimes")
        for device in devices:
            window = MaintenanceWindow(
                name=name,
                reason=reason,
                scope=MaintenanceScope.DEVICE,
                device_id=device.id,
                starts_at=starts_at,
                ends_at=ends_at,
                created_by=current_user.email,
            )
            db.add(window)
            affected.append(device.id)
        db.commit()
        detail = f"Created {len(affected)} maintenance window(s)"

    elif payload.action == BulkDeviceAction.APPLY_CONFIG_TEMPLATE:
        if not devices:
            raise HTTPException(status_code=400, detail="No valid devices selected")
        template_id = payload.params.get("template_id")
        if not template_id:
            raise HTTPException(status_code=400, detail="template_id is required")

        from app.api.change_requests import create_change_request
        from app.api.config_templates import _get_template, _render
        from app.models.change_request import ChangePriority
        from app.schemas.change_request import ChangeRequestCreate

        template = _get_template(db, uuid.UUID(str(template_id)))
        rendered, _version = _render(db, template, payload.params.get("variables", {}), None)

        device_id_list = list(found.keys())
        cr_payload = ChangeRequestCreate(
            device_id=device_id_list[0],
            additional_device_ids=device_id_list[1:],
            priority=ChangePriority(payload.params.get("priority", "medium")),
            description=payload.params.get("description") or f"Bulk apply template '{template.name}'",
            business_justification=payload.params.get("business_justification"),
            proposed_config=rendered,
        )
        cr = create_change_request(cr_payload, db=db, current_user=current_user)
        change_request_id = cr.id
        affected = device_id_list
        detail = f"Created change request {cr.id} covering {len(affected)} device(s)"

    elif payload.action == BulkDeviceAction.ROTATE_CREDENTIALS:
        if not devices:
            raise HTTPException(status_code=400, detail="No valid devices selected")

        ssh_username = payload.params.get("ssh_username")
        ssh_password = payload.params.get("ssh_password")
        snmp_community = payload.params.get("snmp_community")
        snmp_v3_auth_key = payload.params.get("snmp_v3_auth_key")
        snmp_v3_priv_key = payload.params.get("snmp_v3_priv_key")

        if (
            ssh_username is None
            and ssh_password is None
            and snmp_community is None
            and snmp_v3_auth_key is None
            and snmp_v3_priv_key is None
        ):
            raise HTTPException(
                status_code=400,
                detail="At least one of ssh_username, ssh_password, snmp_community, "
                "snmp_v3_auth_key, snmp_v3_priv_key is required",
            )

        rotated_ssh = ssh_username is not None or ssh_password is not None
        rotated_snmp = snmp_community is not None or snmp_v3_auth_key is not None or snmp_v3_priv_key is not None

        for device in devices:
            if ssh_username is not None:
                device.ssh_username = ssh_username
            if ssh_password is not None:
                credential_service.set_ssh_password(device, ssh_password)
            if rotated_snmp:
                credential_service.set_snmp_credentials(
                    device,
                    community=snmp_community,
                    v3_auth_key=snmp_v3_auth_key,
                    v3_priv_key=snmp_v3_priv_key,
                )
            if not rotated_snmp and ssh_password is None and ssh_username is not None:
                # Username-only change still counts as a credential
                # rotation for countdown purposes.
                device.credentials_rotated_at = datetime.datetime.now(datetime.timezone.utc)
            affected.append(device.id)
        db.commit()

        rotated_what = ", ".join(
            filter(None, [rotated_ssh and "SSH", rotated_snmp and "SNMP"])
        )
        detail = f"Rotated {rotated_what} credential(s) on {len(affected)} device(s)"

    else:
        raise HTTPException(status_code=400, detail=f"Unsupported action: {payload.action}")

    if detail:
        audit_service.record_event(
            db, actor=current_user.email, action=f"Bulk {payload.action.value}", result="Success", detail=detail
        )

    return BulkDeviceActionResult(
        action=payload.action,
        affected_device_ids=affected,
        failed=failed,
        detail=detail,
        change_request_id=change_request_id,
    )


@router.get("/credentials/expiry")
def list_credential_expiry(
    status: str | None = Query(
        None, description="Filter: ok | due_soon | overdue | unknown"
    ),
    policy_days: int = Query(credential_service.DEFAULT_ROTATION_POLICY_DAYS, ge=1, le=3650),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Fleet-wide credential-expiry countdown -- backs the badge shown on
    the device list and a dedicated "credentials due for rotation" view,
    so rotation happens proactively ahead of the policy deadline instead
    of reactively after a lockout. See
    app.services.credential_service.credential_expiry for the per-device
    calculation and status thresholds.
    """
    devices = db.query(Device).order_by(Device.hostname).all()
    results = []
    for device in devices:
        badge = credential_service.credential_expiry(device, policy_days=policy_days)
        if status and badge["status"] != status:
            continue
        results.append(
            {
                "device_id": device.id,
                "hostname": device.hostname,
                **badge,
            }
        )
    return results


@router.get("/{device_id}", response_model=DeviceRead)
def get_device(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return DeviceRead.from_device(device)


@router.get("/{device_id}/overview")
def get_device_overview(
    device_id: uuid.UUID,
    hours: int = Query(72, ge=1, le=24 * 30, description="Lookback window for counts + timeline"),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Unified "why is this device unhealthy" payload for the device
    detail panel: live health, recent per-source counts (alerts, drift,
    notable syslog, deployments), and a single merged timeline built by
    app.services.device_overview_service. Exists so the frontend doesn't
    have to make four separate calls (Health, Drift, Syslog Viewer,
    Deployments) and stitch them together client-side just to answer
    "what happened to this device recently".
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    overview = device_overview_service.build_device_overview(db, device, hours=hours)
    overview["credential_expiry"] = credential_service.credential_expiry(device)
    return overview


@router.patch("/{device_id}", response_model=DeviceRead)
def update_device(
    device_id: uuid.UUID,
    payload: DeviceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(INVENTORY_MANAGER_ROLES),
):
    """Partial update -- e.g. enabling SNMP monitoring on a device that was
    added before its community string / SNMPv3 credentials were set up, or
    moving a device to a new data center/rack from the Groups page (drag-drop
    or bulk move). Only fields present in the request body are changed.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    updates = payload.model_dump(exclude_unset=True)
    if "hostname" in updates and updates["hostname"] != device.hostname:
        if db.query(Device).filter(Device.hostname == updates["hostname"]).first():
            raise HTTPException(status_code=400, detail="Device with this hostname already exists")

    was_snmp_enabled = device.supports_snmp
    # Snapshot before-values for every field actually being changed, so the
    # audit entry can say what moved rather than just that "something" did.
    before = {field: getattr(device, field, None) for field in updates}
    hostname_for_log = device.hostname

    for field, value in updates.items():
        if field in ("tags", "custom_fields"):
            # Same reasoning as enabled_health_checks below: these are
            # Text columns storing JSON, but DeviceUpdate exposes them
            # deserialized (list[str] / dict[str,str]) for a sane API
            # shape.
            setattr(device, field, json.dumps(value) if value else None)
        elif field == "enabled_health_checks":
            # Device.enabled_health_checks is a Text column storing a
            # JSON-encoded list (see DeviceRead.from_device / schemas/device.py).
            # `value` here is the deserialized list[str] | None from
            # DeviceUpdate -- it must be re-serialized before it's written,
            # otherwise a raw list gets shoved into the Text column and the
            # very next read (`json.loads` on a non-string) silently fails,
            # falls back to None, and the operator's selection appears to
            # have vanished/reverted to "run everything" the moment they
            # navigate away and back.
            import json as _json

            setattr(device, field, _json.dumps(value) if value else None)
        else:
            setattr(device, field, value)

    try:
        db.commit()
        db.refresh(device)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Failed to update device: {exc}")

    if device.supports_snmp and not was_snmp_enabled:
        _poll_snmp_best_effort(db, device)

    if "data_center" in updates or "rack" in updates:
        # This is the write path the Groups page uses (drag-drop onto a
        # rack, or the bulk "Move to rack" action) -- log it as a
        # placement change specifically, since "what moved where" is what
        # a NOC admin actually wants out of the audit trail here.
        old_place = f"{before.get('data_center') or 'Unassigned'} / {before.get('rack') or 'Unassigned'}"
        new_place = f"{device.data_center or 'Unassigned'} / {device.rack or 'Unassigned'}"
        audit_service.record_event(
            db,
            actor=current_user.email,
            action="Moved device",
            result="Success",
            device_hostname=hostname_for_log,
            detail=f"{old_place} → {new_place}",
        )
    elif updates:
        changed_fields = ", ".join(sorted(updates.keys()))
        audit_service.record_event(
            db,
            actor=current_user.email,
            action="Updated device",
            result="Success",
            device_hostname=hostname_for_log,
            detail=f"Fields changed: {changed_fields}",
        )

    if updates:
        event_bus.publish_event(
            "device_updated", channel=event_bus.TOPOLOGY_CHANNEL, device_id=str(device.id), fields=sorted(updates.keys())
        )

    return DeviceRead.from_device(device)


@router.delete("/{device_id}", status_code=204)
def delete_device(
    device_id: uuid.UUID,
    force: bool = Query(False, description="Also permanently delete this device's change/deployment/config history"),
    db: Session = Depends(get_db),
    current_user: User = Depends(INVENTORY_MANAGER_ROLES),
):
    """Delete a device and ALL its related records.

    If the device has compliance-relevant history (change requests,
    deployments, config snapshots, golden configs) the caller must pass
    ``?force=true`` — otherwise a 409 is raised so the UI can warn the
    operator. Pure telemetry (alerts, drift, metrics, protocol ops) is
    always purged since it has no standalone value once the device is gone.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    blocking_counts = {
        "change_requests": db.query(func.count(ChangeRequest.id)).filter(ChangeRequest.device_id == device_id).scalar(),
        "deployments": db.query(func.count(Deployment.id)).filter(Deployment.device_id == device_id).scalar(),
        "config_snapshots": db.query(func.count(ConfigSnapshot.id)).filter(ConfigSnapshot.device_id == device_id).scalar(),
        "golden_config": db.query(func.count(GoldenConfig.id)).filter(GoldenConfig.device_id == device_id).scalar(),
    }
    blocking_counts = {k: v for k, v in blocking_counts.items() if v}

    if blocking_counts and not force:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"'{device.hostname}' has change/deployment history and cannot be deleted without confirmation. "
                    "Retry with ?force=true to permanently delete the device along with this history."
                ),
                "counts": blocking_counts,
            },
        )

    try:
        # ------- Purge ALL child rows before deleting the device -------
        # Pure telemetry: no compliance value — always purged.
        db.query(Alert).filter(Alert.device_id == device_id).delete(synchronize_session=False)
        db.query(AlertSnooze).filter(AlertSnooze.device_id == device_id).delete(synchronize_session=False)
        db.query(ConfigDrift).filter(ConfigDrift.device_id == device_id).delete(synchronize_session=False)
        vm_client.delete_device_series(device_id)
        db.query(ProtocolOperation).filter(ProtocolOperation.device_id == device_id).delete(synchronize_session=False)
        db.query(DeviceMetric).filter(DeviceMetric.device_id == device_id).delete(synchronize_session=False)
        db.query(DeviceStatusHistory).filter(DeviceStatusHistory.device_id == device_id).delete(synchronize_session=False)
        db.query(InterfaceMetric).filter(InterfaceMetric.device_id == device_id).delete(synchronize_session=False)
        db.query(InterfaceStatus).filter(InterfaceStatus.device_id == device_id).delete(synchronize_session=False)
        db.query(InterfaceAlertConfig).filter(InterfaceAlertConfig.device_id == device_id).delete(synchronize_session=False)
        db.query(SyslogMessage).filter(SyslogMessage.device_id == device_id).delete(synchronize_session=False)
        db.query(FlowRecord).filter(FlowRecord.device_id == device_id).delete(synchronize_session=False)
        db.query(MaintenanceWindow).filter(MaintenanceWindow.device_id == device_id).delete(synchronize_session=False)
        db.query(RecurringMaintenanceSchedule).filter(RecurringMaintenanceSchedule.device_id == device_id).delete(synchronize_session=False)
        db.query(FirmwareUpgrade).filter(FirmwareUpgrade.device_id == device_id).delete(synchronize_session=False)
        # PathTrace: device can be source or target; purge dependent hops first
        path_trace_ids = [
            pt.id for pt in db.query(PathTrace.id).filter(
                (PathTrace.source_device_id == device_id) | (PathTrace.target_device_id == device_id)
            ).all()
        ]
        if path_trace_ids:
            db.query(PathHop).filter(PathHop.path_trace_id.in_(path_trace_ids)).delete(synchronize_session=False)
            db.query(PathTrace).filter(PathTrace.id.in_(path_trace_ids)).delete(synchronize_session=False)
        # DiscoveredNeighbor: device_id OR neighbor_device_id
        db.query(DiscoveredNeighbor).filter(
            (DiscoveredNeighbor.device_id == device_id) | (DiscoveredNeighbor.neighbor_device_id == device_id)
        ).delete(synchronize_session=False)

        # Compliance-relevant records (only reached when force=true or counts are 0).
        # Deletion order: children before parents.
        # deployment_logs / health_check_results -> deployments -> change_requests
        deployment_ids = [
            d.id for d in db.query(Deployment.id).filter(Deployment.device_id == device_id).all()
        ]
        if deployment_ids:
            db.query(DeploymentLog).filter(DeploymentLog.deployment_id.in_(deployment_ids)).delete(synchronize_session=False)
            db.query(HealthCheckResult).filter(HealthCheckResult.deployment_id.in_(deployment_ids)).delete(synchronize_session=False)
        db.query(Deployment).filter(Deployment.device_id == device_id).delete(synchronize_session=False)

        change_request_ids = [
            cr.id for cr in db.query(ChangeRequest.id).filter(ChangeRequest.device_id == device_id).all()
        ]
        if change_request_ids:
            # Audit history is immutable — detach the FK rather than deleting.
            db.query(AuditLog).filter(AuditLog.change_request_id.in_(change_request_ids)).update(
                {"change_request_id": None}, synchronize_session=False
            )
            # ChangeRequest.rollback_snapshot_id -> config_snapshots.id and
            # ConfigSnapshot.change_request_id -> change_requests.id form a
            # circular FK (see the note in alembic/versions/0001_baseline.py).
            # Any change request for this device that went through a
            # rollback has rollback_snapshot_id pointing at one of the
            # ConfigSnapshot rows deleted right below -- detach it first or
            # that delete throws an IntegrityError that (unlike the one
            # guarded further down) used to propagate unhandled, producing
            # a raw 500 that skips CORSMiddleware entirely and shows up in
            # the browser as an opaque "Network Error" instead of a real
            # message.
            db.query(ChangeRequest).filter(ChangeRequest.id.in_(change_request_ids)).update(
                {"rollback_snapshot_id": None}, synchronize_session=False
            )
        db.query(ConfigSnapshot).filter(ConfigSnapshot.device_id == device_id).delete(synchronize_session=False)
        db.query(GoldenConfig).filter(GoldenConfig.device_id == device_id).delete(synchronize_session=False)
        db.query(ChangeRequest).filter(ChangeRequest.device_id == device_id).delete(synchronize_session=False)

        if blocking_counts:
            audit_service.record_event(
                db, actor=current_user.email, action="Device Force-Deleted", result="Deleted",
                device_hostname=device.hostname,
                detail=f"Purged history: {blocking_counts}",
            )

        deleted_hostname = device.hostname
        db.delete(device)
        db.commit()
        event_bus.publish_event(
            "device_deleted", channel=event_bus.TOPOLOGY_CHANNEL, device_id=str(device_id), hostname=deleted_hostname
        )
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"'{device.hostname}' still has related records that prevent deletion. "
                    "Retry with ?force=true to permanently delete the device along with all its history."
                ),
                "counts": {"related_records": 1},
            },
        )
    except SQLAlchemyError:
        # Belt-and-suspenders alongside the IntegrityError branch above:
        # any *other* unexpected DB error partway through this multi-table
        # purge (a future FK this function doesn't know about yet, a
        # constraint added later, etc.) should still roll back and surface
        # as a real, catchable 409 -- not propagate as a raw exception.
        # (Even if it did propagate, app.main's global exception handler
        # now converts it to a proper CORS-safe 500 instead of the
        # unreadable "Network Error" this used to produce -- but failing
        # cleanly here gives a much more actionable message than that.)
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"'{device.hostname}' could not be deleted due to an unexpected database error. "
                    "Retry with ?force=true, or check the server logs for the underlying cause."
                ),
                "counts": {"related_records": 1},
            },
        )


@router.get("/{device_id}/snapshots", response_model=list[SnapshotSummary])
def list_device_snapshots(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Config version history for a device (SRS 10: git-style config
    version control), newest first. Pick a `version`'s `id` to pass to
    POST /devices/{device_id}/rollback.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return rollback_service.list_snapshots(db, device_id)


@router.get("/{device_id}/rollback/preview", response_model=RollbackPreviewResponse)
def preview_device_rollback(
    device_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(ROLLBACK_ROLES),
):
    """Shows the diff a rollback to `snapshot_id` would apply -- before
    anything is confirmed. Read-only: no ChangeRequest is created, no
    config is pushed. Pair with POST /devices/{device_id}/rollback once
    the user has reviewed this and confirms.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    snapshot = db.get(ConfigSnapshot, snapshot_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    try:
        preview = rollback_service.preview_rollback(db, device, snapshot)
    except rollback_service.RollbackError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return RollbackPreviewResponse(**preview)


@router.post("/{device_id}/rollback", response_model=RollbackResponse, status_code=202)
def rollback_device(
    device_id: uuid.UUID,
    payload: RollbackRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(ROLLBACK_ROLES),
):
    """Manually roll a device back to a prior configuration snapshot.

    Builds and auto-approves a change request that redeploys the chosen
    snapshot, then queues it on the same deployment pipeline used for
    ordinary approved changes (Snapshot -> Deploy -> Health Monitor ->
    Success / Automatic Rollback) -- so the restore itself is snapshotted
    first and its health is verified just like any other deployment.

    Returns immediately (202) with the change_request_id; poll
    GET /change-requests/{id} or GET /deployments?change_request_id={id}
    for progress, same as approving a normal change request.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    snapshot = db.get(ConfigSnapshot, payload.snapshot_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    try:
        cr = rollback_service.initiate_rollback(db, device, snapshot, current_user, reason=payload.reason)
    except rollback_service.RollbackError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    run_deployment_pipeline_task.delay(str(cr.id), current_user.email)

    return RollbackResponse(
        change_request_id=cr.id,
        status=cr.status.value,
        message=f"Rollback queued for {device.hostname}. Track progress via the change request or deployments feed.",
    )


@router.get("/{device_id}/rollback/sections", response_model=list[RollbackSection])
def list_device_rollback_sections(
    device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)
):
    """Independently revertible sections (ACLs, VLANs, interface
    stanzas, ...) found in the device's current configuration -- the
    picklist for a section-level rollback. Pair a `key` from this list
    with a `snapshot_id` on the partial-rollback preview/confirm
    endpoints below to revert just that one section.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return rollback_service.list_rollback_sections(db, device)


@router.get("/{device_id}/rollback/partial/preview", response_model=PartialRollbackPreviewResponse)
def preview_partial_device_rollback(
    device_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    section_key: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(ROLLBACK_ROLES),
):
    """Shows the diff a section-level rollback would apply -- reverting
    only `section_key` to its version in `snapshot_id`, leaving every
    other line of the device's current config untouched. Read-only.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    snapshot = db.get(ConfigSnapshot, snapshot_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    try:
        preview = rollback_service.preview_partial_rollback(db, device, snapshot, section_key)
    except rollback_service.RollbackError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return PartialRollbackPreviewResponse(**preview)


@router.post("/{device_id}/rollback/partial", response_model=RollbackResponse, status_code=202)
def rollback_device_partial(
    device_id: uuid.UUID,
    payload: PartialRollbackRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(ROLLBACK_ROLES),
):
    """Section-level (partial) rollback: reverts only `section_key`
    (e.g. `"ACL:BLOCK_TELNET"`, from GET .../rollback/sections) to its
    version in `snapshot_id`, instead of restoring the entire device
    config. Reduces the blast radius of the rollback itself -- anything
    changed elsewhere on the box since that snapshot is left alone.

    Runs through the same Snapshot -> Deploy -> Health Monitor pipeline
    as a full rollback, including automatic full-config rollback if this
    smaller change still fails its health checks.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    snapshot = db.get(ConfigSnapshot, payload.snapshot_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    try:
        cr = rollback_service.initiate_partial_rollback(
            db, device, snapshot, payload.section_key, current_user, reason=payload.reason
        )
    except rollback_service.RollbackError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    run_deployment_pipeline_task.delay(str(cr.id), current_user.email)

    return RollbackResponse(
        change_request_id=cr.id,
        status=cr.status.value,
        message=(
            f"Partial rollback of {payload.section_key} queued for {device.hostname}. "
            "Track progress via the change request or deployments feed."
        ),
    )


@router.post("/{device_id}/clear-unstable-flag", response_model=DeviceRead)
def clear_unstable_flag(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(INVENTORY_MANAGER_ROLES),
):
    """Manual review sign-off for the deployment pipeline circuit breaker:
    clears `flagged_unstable` so automated deploys against this device are
    allowed again. Only Network Administrators may clear it (same RBAC as
    inventory management / rollback) -- this is a deliberate "I've looked
    at what's wrong with this device" action, never automatic.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not device.flagged_unstable:
        raise HTTPException(status_code=400, detail="Device is not currently flagged unstable")

    device.flagged_unstable = False
    device.unstable_since = None
    db.commit()
    db.refresh(device)

    audit_service.record_event(
        db, actor=current_user.email, action="Unstable Flag Cleared", result="Success",
        device_hostname=device.hostname,
        detail="Manual review completed; automated deploys re-enabled.",
    )
    return DeviceRead.from_device(device)


@router.post("/{device_id}/snmp-credentials", response_model=DeviceRead)
def set_snmp_credentials(
    device_id: uuid.UUID,
    payload: SnmpCredentialsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(INVENTORY_MANAGER_ROLES),
):
    """Sets/updates SNMP secrets for a device: the v1/v2c community string,
    or the SNMPv3 auth/privacy passphrases. Encrypted at rest (Fernet, see
    app.core.crypto) and never returned by any GET endpoint -- DeviceRead
    only exposes a derived `snmp_credentials_configured` boolean. Only
    fields present in the request are touched; omit a field to leave it
    unchanged, or send "" to explicitly clear it.

    This does not itself verify the credentials work -- use
    POST /devices/{id}/snmp-credentials/test for that, either before or
    after saving.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    credential_service.set_snmp_credentials(
        device,
        community=payload.community,
        v3_auth_key=payload.v3_auth_key,
        v3_priv_key=payload.v3_priv_key,
    )
    db.commit()
    db.refresh(device)

    # Don't make the operator wait for the next scheduled sweep to see
    # whether the credentials they just entered actually work -- poll
    # immediately (best-effort; failures here don't block the response,
    # same as the create/enable-SNMP paths above).
    if device.supports_snmp:
        _poll_snmp_best_effort(db, device)
        db.refresh(device)

    audit_service.record_event(
        db, actor=current_user.email, action="SNMP Credentials Updated", result="Success",
        device_hostname=device.hostname,
        detail="community" if payload.community is not None else "v3 auth/priv keys",
    )

    return DeviceRead.from_device(device)


@router.post("/{device_id}/metrics/poll")
def poll_device_metrics(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(INVENTORY_MANAGER_ROLES),
):
    """On-demand SNMP poll for a device's telemetry."""
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not device.supports_snmp:
        raise HTTPException(status_code=400, detail="SNMP monitoring is not enabled for this device")

    try:
        metrics_service.poll_device(db, device)
    except metrics_service.credential_service.CredentialNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"On-demand poll failed: {exc}")

    return {"status": "success"}


@router.post("/{device_id}/snmp-credentials/test", response_model=SnmpTestResult)
def test_snmp_credentials(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Verifies SNMP connectivity using whatever credentials are currently
    on file for this device (DB-encrypted first, legacy env-var ref as
    fallback -- see credential_service), without doing a full health poll.
    Lets an operator confirm a community string / SNMPv3 credential set
    actually works right after saving it, instead of waiting for the next
    scheduled poll to find out it doesn't.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not device.snmp_version:
        raise HTTPException(status_code=400, detail="Device has no SNMP version configured (snmp_version is unset)")

    try:
        auth = metrics_service.build_snmp_auth(device)
    except credential_service.CredentialNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    from app.core.config import settings

    result = snmp_service.test_connection(device.ip_address, auth, timeout=settings.SNMP_TIMEOUT_SECONDS)
    return SnmpTestResult(**result)


@router.get("/{device_id}/discovery", response_model=DeviceDiscoveryResult)
def discover_device(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """On-demand SNMP discovery: hostname, ARP table, routing table, LLDP/
    CDP neighbors, and chassis/module inventory (snmp_service.discover_inventory).
    Heavier than a routine health poll (several full table walks), so this
    runs only when requested, not on the scheduled polling cadence.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not device.supports_snmp or not device.snmp_version:
        raise HTTPException(status_code=400, detail="Device has no SNMP configured (supports_snmp/snmp_version unset)")

    try:
        auth = metrics_service.build_snmp_auth(device)
    except credential_service.CredentialNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    from app.core.config import settings

    result = snmp_service.discover_inventory(
        device.ip_address, auth, timeout=settings.SNMP_TIMEOUT_SECONDS, vendor=device.vendor.value
    )

    # Backfill Device.platform/model/serial_number/os_version from
    # discovery whenever they're still blank -- these were being computed
    # by snmp_service all along but the values had nowhere to land
    # (dropped by the response model, never written to the Device row).
    # Only fills gaps, never overwrites an operator-entered value, so a
    # manually-corrected platform/model/serial/os_version isn't clobbered
    # by a later re-discovery. Same detection path for Cisco and Juniper
    # -- see snmp_service._detect_os_version_from_sysdescr.
    if not device.platform and result.get("detected_platform"):
        device.platform = result["detected_platform"]
    if not device.model and result.get("detected_model"):
        device.model = result["detected_model"]
    if not device.serial_number and result.get("detected_serial_number"):
        device.serial_number = result["detected_serial_number"]
    if not device.os_version and result.get("detected_os_version"):
        device.os_version = result["detected_os_version"]
    db.commit()

    return DeviceDiscoveryResult(
        device_id=device.id,
        hostname=device.hostname,
        reported_hostname=result["hostname"],
        arp_table=result["arp_table"],
        routing_table=result["routing_table"],
        lldp_neighbors=result["lldp_neighbors"],
        cdp_neighbors=result["cdp_neighbors"],
        inventory=result["inventory"],
        detected_platform=result.get("detected_platform"),
        detected_model=result.get("detected_model"),
        detected_serial_number=result.get("detected_serial_number"),
        detected_os_version=result.get("detected_os_version"),
        retrieved_at=datetime.datetime.utcnow(),
    )


@router.post("/{device_id}/ssh-credentials", response_model=DeviceRead)
def set_ssh_credentials(
    device_id: uuid.UUID,
    payload: SshCredentialsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(INVENTORY_MANAGER_ROLES),
):
    """Sets/updates the SSH login for a device: username (plain) and
    password (Fernet-encrypted at rest, see app.core.crypto). Never
    returned by any GET endpoint -- DeviceRead only exposes a derived
    `ssh_credentials_configured` boolean.

    This is the actual credential entry point for NETCONF/RESTCONF/SSH
    deployments, config backups, and topology link inference (all of
    which need a real running-config read to succeed) -- ssh_credential_ref
    alone is only a *pointer* to a NETGUARD_CRED_<REF> env var and was
    never something an operator could set from the UI.

    Only fields present in the request are touched; omit a field to leave
    it unchanged, or send password="" to explicitly clear it. This does
    not itself verify the credential works -- use
    POST /devices/{id}/ssh-credentials/test for that, either before or
    after saving.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    touched: list[str] = []
    if payload.username is not None:
        device.ssh_username = payload.username
        touched.append("username")
    if payload.password is not None:
        credential_service.set_ssh_password(device, payload.password)
        touched.append("password")
    if payload.private_key is not None:
        credential_service.set_ssh_private_key(device, payload.private_key, payload.private_key_passphrase)
        touched.append("private_key")
    if payload.auth_method is not None:
        if payload.auth_method not in ("password", "key"):
            raise HTTPException(status_code=400, detail="auth_method must be 'password' or 'key'")
        device.ssh_auth_method = payload.auth_method
        touched.append("auth_method")

    db.commit()
    db.refresh(device)

    audit_service.record_event(
        db, actor=current_user.email, action="SSH Credentials Updated", result="Success",
        device_hostname=device.hostname,
        detail=", ".join(touched) if touched else "no-op",
    )

    return DeviceRead.from_device(device)


@router.post("/{device_id}/ssh-credentials/test", response_model=SshTestResult)
def test_ssh_credentials(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Verifies SSH/NETCONF/RESTCONF connectivity using whatever
    credentials are currently on file for this device (DB-encrypted
    password first, legacy env-var ref as fallback -- see
    credential_service), by reusing the exact same read that backups,
    drift detection, and topology link inference depend on
    (ProtocolManager.get_running_config) rather than opening a separate
    test-only connection. Lets an operator confirm a password actually
    works right after saving it, instead of waiting for the next
    scheduled backup/poll to find out it doesn't.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    used_protocol = protocol_manager.select_protocol(device)
    result = protocol_manager.ProtocolManager(db, device, operator=current_user.email).get_running_config()
    return SshTestResult(
        success=result.success,
        message=result.error or f"Connected via {used_protocol} and read the running config.",
        protocol=used_protocol if result.success else None,
    )


@router.get("/{device_id}/protocol-operations")
def list_device_protocol_operations(
    device_id: uuid.UUID,
    limit: int = 50,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Recent NETCONF/RESTCONF/SNMP operations recorded against this device
    (config reads/pushes, health checks, SNMP polls) -- backs the Protocol
    Operations tab on the device detail page. Complements the coarser
    AuditLog with the raw request/response payloads captured by
    ProtocolManager for every operation it performs.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    ops = (
        db.query(ProtocolOperation)
        .filter(ProtocolOperation.device_id == device_id)
        .order_by(ProtocolOperation.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": str(op.id),
            "protocol": op.protocol.value if hasattr(op.protocol, "value") else str(op.protocol),
            "operation": op.operation,
            "operator": op.operator,
            "success": op.success,
            "error_message": op.error_message,
            "http_status": op.http_status,
            "execution_time_ms": op.execution_time_ms,
            "created_at": op.created_at,
        }
        for op in ops
    ]


# ------------------------------------------------------------------
# Device grouping (rack + data center) and interface status
# ------------------------------------------------------------------


@router.get("/groups/summary")
def get_device_groups(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Devices rolled up into a data-center -> rack -> device hierarchy,
    for the Topology page's grouping view and any other "group by
    location" UI. Devices with no data_center/rack set are bucketed
    under "Unassigned" rather than dropped, so the fleet always fully
    accounts for every device even before anyone's filled in placement.
    """
    devices = db.query(Device).order_by(Device.hostname).all()

    data_centers: dict[str, dict] = {}
    for d in devices:
        dc_name = d.data_center or "Unassigned"
        rack_name = d.rack or "Unassigned"
        dc = data_centers.setdefault(dc_name, {"name": dc_name, "racks": {}, "device_count": 0})
        rack = dc["racks"].setdefault(rack_name, {"name": rack_name, "devices": []})
        rack["devices"].append(
            {
                "id": str(d.id),
                "hostname": d.hostname,
                "status": d.status.value if hasattr(d.status, "value") else str(d.status),
                "device_type": d.device_type,
                "rack_position": d.rack_position,
            }
        )
        dc["device_count"] += 1

    result = []
    for dc in data_centers.values():
        # Sort devices within a rack by rack_position when set (unset
        # sorts last), then hostname -- gives a stable, sensible
        # top-to-bottom rack-elevation order.
        racks = []
        for rack in dc["racks"].values():
            rack["devices"].sort(key=lambda dv: (dv["rack_position"] is None, dv["rack_position"] or 0, dv["hostname"]))
            racks.append(rack)
        racks.sort(key=lambda r: r["name"])
        result.append({"name": dc["name"], "device_count": dc["device_count"], "racks": racks})

    result.sort(key=lambda dc: dc["name"])
    return result


@router.get("/{device_id}/interfaces", response_model=list[InterfaceCurrentStatus])
def get_device_interfaces(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Current status of every interface last seen on this device --
    the most recent InterfaceStatus row per if_index, i.e. what the
    device detail / topology drawer's "Interfaces" panel and the NOC
    dashboard's down-port list draw from.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    from app.models.interface_status import InterfaceStatus

    subq = (
        db.query(
            InterfaceStatus.if_index,
            func.max(InterfaceStatus.changed_at).label("max_changed_at"),
        )
        .filter(InterfaceStatus.device_id == device_id)
        .group_by(InterfaceStatus.if_index)
        .subquery()
    )
    rows = (
        db.query(InterfaceStatus)
        .join(
            subq,
            (InterfaceStatus.if_index == subq.c.if_index) & (InterfaceStatus.changed_at == subq.c.max_changed_at),
        )
        .filter(InterfaceStatus.device_id == device_id)
        .order_by(InterfaceStatus.if_descr)
        .all()
    )

    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for r in rows:
        changed_at = r.changed_at
        seconds_in_status = None
        if changed_at is not None:
            ref = changed_at if changed_at.tzinfo else changed_at.replace(tzinfo=datetime.timezone.utc)
            seconds_in_status = (now - ref).total_seconds()
        out.append(
            InterfaceCurrentStatus(
                if_index=r.if_index,
                if_descr=r.if_descr,
                status=r.status.value if hasattr(r.status, "value") else str(r.status),
                changed_at=r.changed_at,
                seconds_in_status=seconds_in_status,
            )
        )
    return out


@router.get("/{device_id}/interfaces/history", response_model=list[InterfaceStatusRead])
def get_device_interface_history(
    device_id: uuid.UUID,
    if_index: str | None = Query(None, description="Scope to one interface's history by ifIndex"),
    limit: int = Query(200, ge=1, le=2000),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Full up/down transition log for this device (optionally scoped to
    one interface), newest first -- Interface Status History panel.
    """
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    from app.models.interface_status import InterfaceStatus

    q = db.query(InterfaceStatus).filter(InterfaceStatus.device_id == device_id)
    if if_index:
        q = q.filter(InterfaceStatus.if_index == if_index)
    rows = q.order_by(InterfaceStatus.changed_at.desc()).limit(limit).all()
    return [
        InterfaceStatusRead(
            id=r.id,
            device_id=r.device_id,
            if_index=r.if_index,
            if_descr=r.if_descr,
            status=r.status.value if hasattr(r.status, "value") else str(r.status),
            previous_status=(r.previous_status.value if r.previous_status and hasattr(r.previous_status, "value") else r.previous_status),
            changed_at=r.changed_at,
        )
        for r in rows
    ]
