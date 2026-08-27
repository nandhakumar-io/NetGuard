import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.device import Device
from app.models.golden_config import GoldenConfig
from app.models.interface_alert_config import InterfaceAlertConfig
from app.models.snapshot import ConfigSnapshot
from app.models.user import User, UserRole
from app.schemas.config_management import (
    BackupConfigRequest,
    BackupConfigResponse,
    BackupHistoryEntry,
    CompareConfigRequest,
    CompareConfigResponse,
    DeviceRetentionStatus,
    GoldenConfigCompareResponse,
    GoldenConfigRead,
    GoldenConfigSet,
    InterfaceAlertConfigOut,
    InterfaceAlertConfigUpdate,
    InterfacesResponse,
    InterfaceStatusOut,
    RestoreConfigRequest,
    RestoreConfigResponse,
    RetentionPolicy,
    RetentionPolicyResponse,
    RunningConfigResponse,
    StartupConfigResponse,
)
from app.services import (
    audit_service,
    config_format_service,
    diff_engine,
    metrics_service,
    snapshot_service,
    snmp_service,
)
from app.services.protocol_manager import ProtocolManager, select_protocol
from app.services.rollback_service import list_snapshots

router = APIRouter(prefix="/devices/{device_id}/config", tags=["configuration-management"])

# Global (not per-device) snapshot retention endpoint -- the policy itself
# applies fleet-wide, so it lives outside the /devices/{device_id}/config
# prefix; ?device_id= optionally layers a specific device's counts on top
# of the same policy payload (see retention_policy_endpoint below).
snapshot_policy_router = APIRouter(prefix="/snapshots", tags=["configuration-management"])

# Backups and restores are config-changing/authority-bearing operations
# (a restore is a live config push), so they're gated the same way
# rollback is in app.api.devices: Network Administrators only. Viewing
# (running/startup/backup history/compare) is available to any
# authenticated user, matching read access elsewhere in the app.
CONFIG_WRITE_ROLES = require_roles(UserRole.NETWORK_ADMIN)


def _get_device(db: Session, device_id: uuid.UUID) -> Device:
    device = db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


def _latest_snapshot(db: Session, device_id: uuid.UUID) -> ConfigSnapshot | None:
    return (
        db.query(ConfigSnapshot)
        .filter(ConfigSnapshot.device_id == device_id)
        .order_by(ConfigSnapshot.created_at.desc())
        .first()
    )


def _get_snapshot_for_device(db: Session, device_id: uuid.UUID, snapshot_id: uuid.UUID) -> ConfigSnapshot:
    snapshot = db.get(ConfigSnapshot, snapshot_id)
    if not snapshot or snapshot.device_id != device_id:
        raise HTTPException(status_code=404, detail="Snapshot not found for this device")
    return snapshot


# ---------------------------------------------------------------------------
# View Running Config
# ---------------------------------------------------------------------------
@router.get("/running", response_model=RunningConfigResponse)
def view_running_config(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    device = _get_device(db, device_id)
    pm = ProtocolManager(db, device)
    result = pm.get_running_config()
    if not result.success:
        raise HTTPException(status_code=502, detail=result.error or "Failed to read running configuration")
    is_xml = config_format_service.looks_like_xml(result.output)
    return RunningConfigResponse(
        device_id=device.id,
        hostname=device.hostname,
        protocol=result.protocol.value if hasattr(result.protocol, "value") else str(result.protocol),
        config=result.output,
        config_pretty=config_format_service.pretty_xml(result.output) if is_xml else None,
        is_xml=is_xml,
        retrieved_at=datetime.datetime.utcnow(),
    )


# ---------------------------------------------------------------------------
# View Startup Config
# ---------------------------------------------------------------------------
@router.get("/startup", response_model=StartupConfigResponse)
def view_startup_config(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    device = _get_device(db, device_id)
    snapshot = _latest_snapshot(db, device_id)
    if snapshot is None or not snapshot.startup_config_encrypted:
        return StartupConfigResponse(
            device_id=device.id,
            hostname=device.hostname,
            config=None,
            source="unavailable",
            snapshot_id=None,
            retrieved_at=datetime.datetime.utcnow(),
        )
    startup_text = snapshot_service.decrypt_config(snapshot.startup_config_encrypted)
    is_xml = config_format_service.looks_like_xml(startup_text)
    return StartupConfigResponse(
        device_id=device.id,
        hostname=device.hostname,
        config=startup_text,
        config_pretty=config_format_service.pretty_xml(startup_text) if is_xml else None,
        is_xml=is_xml,
        source="snapshot",
        snapshot_id=snapshot.id,
        retrieved_at=datetime.datetime.utcnow(),
    )


def _snmp_interface_fallback(device) -> list[InterfaceStatusOut]:
    """Best-effort per-interface admin/oper status straight from
    ifTable/ifXTable (snmp_service.walk_interface_stats), used by
    view_interfaces when the primary NETCONF/RESTCONF/SSH read fails but
    the device has SNMP configured. Deliberately thin compared to the
    NETCONF/RESTCONF path: SNMP's ifTable has no notion of IP addresses,
    MTU, or MAC beyond what's already covered by other tabs, so those
    stay unset here rather than guessed -- oper/admin status, description,
    and speed (what actually matters for "is this port up") are what
    ifTable reliably gives us. Never raises: any SNMP failure here should
    fall through to the caller's existing "no data" handling, not replace
    one empty-page failure mode with a 500.
    """
    try:
        auth = metrics_service.build_snmp_auth(device)
        stats = snmp_service.walk_interface_stats(device.ip_address, auth)
    except Exception:
        return []

    out: list[InterfaceStatusOut] = []
    for row in stats.get("per_interface", []):
        status = row.get("status")  # "up" | "down"
        speed_bps = row.get("speed_bps")
        out.append(
            InterfaceStatusOut(
                name=row.get("if_descr") or f"if{row.get('if_index')}",
                description=None,
                # ifTable's ifOperStatus is the only state SNMP directly
                # gives us here -- admin_status isn't separately walked
                # (ifAdminStatus isn't in IFTABLE_OIDS), so it's left
                # unset rather than assumed equal to oper_status.
                admin_status=None,
                oper_status=status,
                ip_addresses=[],
                mtu=None,
                speed=f"{speed_bps // 1_000_000} Mbps" if speed_bps else None,
                mac_address=None,
                alerts_enabled=True,
            )
        )
    return out


# ---------------------------------------------------------------------------
# View Interface Status
# ---------------------------------------------------------------------------
@router.get("/interfaces", response_model=InterfacesResponse)
def view_interfaces(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Live per-interface admin/oper status + IPs (the device Interfaces
    tab). ProtocolManager.get_interfaces() and config_format_service's
    parser already did the real work of fetching and normalizing this
    across NETCONF/RESTCONF/SSH -- this endpoint just never existed to
    call them, which is why the frontend's GET .../config/interfaces
    always 404'd and the tab could only show the fleet-aggregate SNMP
    chart, never live per-interface data.
    """
    device = _get_device(db, device_id)
    pm = ProtocolManager(db, device)
    result = pm.get_interfaces()
    # NOT result.protocol: ProtocolResult.protocol mislabels the SSH/NAPALM
    # path as ProtocolName.NETCONF (see the note in protocol_manager._record --
    # ProtocolOperation's protocol enum has no SSH member, so SSH ops get
    # stored under NETCONF's value with the real protocol tagged into the
    # operation name instead). Trusting that here would make
    # config_format_service.parse_interfaces try to parse a NAPALM repr()
    # string as XML for every device that isn't NETCONF/RESTCONF-enabled --
    # the common case for lab/SSH-only devices -- and silently return no
    # interfaces. select_protocol() is the real, unambiguous choice.
    protocol = select_protocol(device)
    parse_protocol = protocol
    if protocol == "netconf" and (
        (device.vendor.value if hasattr(device.vendor, "value") else str(device.vendor)).lower() == "juniper"
    ):
        # ProtocolManager.get_interfaces() fetches Junos operational state
        # (get-interface-information) rather than get-config for this
        # vendor -- see the note there -- so it needs the matching parser,
        # not the generic ietf-interfaces one "netconf" would otherwise
        # select.
        parse_protocol = "netconf-junos-opstate"

    if not result.success:
        # SNMP fallback for the Interfaces tab specifically. Some
        # NETCONF-capable devices (EX3400 in particular -- its NETCONF
        # server responds to <get-config> fine but has been observed
        # timing out or resetting the session on the operational
        # <get-interface-information> RPC this tab needs, see
        # netconf_service.get_junos_interface_information) leave
        # get_interfaces() failing even though the device is otherwise
        # reachable and NETCONF-managed for everything else (config
        # view/push, drift, snapshots). Previously that meant this tab
        # came back empty with just an error message no matter how many
        # times you reloaded it. If the device also has SNMP configured,
        # fall back to the same ifTable/ifXTable walk the Health
        # Dashboard's Interface Statistics panel already uses
        # (snmp_service.walk_interface_stats) so the tab can at least
        # show admin/oper status, speed, and error counts instead of
        # nothing. This is a fallback, not a silent success: `error`
        # stays populated so the UI can still surface that the primary
        # (NETCONF/RESTCONF/SSH) read failed, just with usable data
        # alongside it rather than an empty table.
        snmp_interfaces = _snmp_interface_fallback(device) if device.snmp_version else []
        return InterfacesResponse(
            device_id=device.id,
            hostname=device.hostname,
            protocol=protocol if not snmp_interfaces else "snmp (fallback)",
            interfaces=snmp_interfaces,
            retrieved_at=datetime.datetime.utcnow(),
            error=(
                f"{result.error or 'Failed to read interface status'} -- showing SNMP data instead"
                if snmp_interfaces
                else (result.error or "Failed to read interface status")
            ),
        )

    parsed = config_format_service.parse_interfaces(result.output, parse_protocol)

    # Best-effort switchport (VLAN/mode/edge) enrichment.
    #
    # Juniper: read straight from the device's own config (family
    # ethernet-switching / protocols rstp) over the NETCONF session we're
    # already using for everything else on this tab -- see
    # protocol_manager.get_junos_switchport_config's docstring. This is
    # the primary source for Juniper devices, not a fallback: SNMP's
    # edge-port MIB (CISCO-STP-EXTENSIONS-MIB) is Cisco-only and never
    # returns anything for Juniper, and Q-BRIDGE-MIB port-mode/VLAN over
    # SNMP additionally requires SNMP to be configured on the device at
    # all and depends on its ifDescr lining up with the NETCONF interface
    # name -- both of which made this tab come back empty for Juniper
    # devices that had NETCONF working fine.
    #
    # Cisco (and anything else): unchanged SNMP-based enrichment.
    vlan_info: dict = {}
    edge_port_info: dict = {}
    vendor_lower = (device.vendor.value if hasattr(device.vendor, "value") else str(device.vendor)).lower()
    if parsed and vendor_lower == "juniper" and protocol == "netconf":
        try:
            switchport_result = pm.get_junos_switchport_config()
            if switchport_result.success:
                parsed_switchport = config_format_service.parse_junos_switchport_config(switchport_result.output)
                vlan_info = parsed_switchport
                edge_port_info = {name: info.get("edge_port") for name, info in parsed_switchport.items() if "edge_port" in info}
        except Exception:
            vlan_info = {}
            edge_port_info = {}
    if parsed and device.snmp_version and (vendor_lower != "juniper" or not vlan_info):
        try:
            auth = metrics_service.build_snmp_auth(device)
            snmp_vlan_info = snmp_service.walk_switchport_vlans(device.ip_address, auth)
            snmp_edge_port_info = snmp_service.walk_stp_edge_ports(device.ip_address, auth)
            # Fill gaps only -- never let a partial/failed SNMP walk
            # overwrite data already resolved straight from Junos config.
            for name, info in snmp_vlan_info.items():
                vlan_info.setdefault(name, info)
            for name, edge in snmp_edge_port_info.items():
                edge_port_info.setdefault(name, edge)
        except Exception:
            pass

    alert_config_by_if = {
        row.if_descr: row.enabled
        for row in db.query(InterfaceAlertConfig).filter(InterfaceAlertConfig.device_id == device.id).all()
    }

    interfaces_out: list[InterfaceStatusOut] = []
    for i in parsed:
        extra = vlan_info.get(i.name, {})
        mode = extra.get("mode")
        if not mode and i.ip_addresses:
            # No switchport/PVID data, but it's got an IP -- almost
            # certainly an L3/routed port rather than an access port with
            # unreadable VLAN info.
            mode = "routed"
        interfaces_out.append(
            InterfaceStatusOut(
                **vars(i),
                port_mode=mode,
                vlan=extra.get("vlan"),
                trunk_vlans=extra.get("trunk_vlans"),
                edge_port=edge_port_info.get(i.name),
                alerts_enabled=alert_config_by_if.get(i.name, True),
            )
        )

    return InterfacesResponse(
        device_id=device.id,
        hostname=device.hostname,
        protocol=protocol,
        interfaces=interfaces_out,
        retrieved_at=datetime.datetime.utcnow(),
        error=None if parsed else "Device returned no parsable interface data",
    )


# ---------------------------------------------------------------------------
# Per-interface "alert me if this port goes down" configuration
# ---------------------------------------------------------------------------
@router.get("/interfaces/alert-config", response_model=list[InterfaceAlertConfigOut])
def list_interface_alert_config(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    device = _get_device(db, device_id)
    return [
        InterfaceAlertConfigOut(device_id=device.id, if_descr=row.if_descr, enabled=row.enabled)
        for row in db.query(InterfaceAlertConfig).filter(InterfaceAlertConfig.device_id == device.id).all()
    ]


@router.put("/interfaces/{if_descr}/alert-config", response_model=InterfaceAlertConfigOut)
def set_interface_alert_config(
    device_id: uuid.UUID,
    if_descr: str,
    payload: InterfaceAlertConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Arms/mutes the automatic "Interface Down" critical alert for one
    interface (see InterfaceAlertConfig and metrics_service._sync_interface_status).
    Any authenticated user can tune this -- it only affects alert noise,
    not device state -- same bar as acking/resolving an alert.
    """
    device = _get_device(db, device_id)
    row = (
        db.query(InterfaceAlertConfig)
        .filter(InterfaceAlertConfig.device_id == device.id, InterfaceAlertConfig.if_descr == if_descr)
        .first()
    )
    if row is None:
        row = InterfaceAlertConfig(device_id=device.id, if_descr=if_descr, enabled=payload.enabled)
        db.add(row)
    else:
        row.enabled = payload.enabled
    row.updated_by = current_user.email
    db.commit()
    db.refresh(row)
    return InterfaceAlertConfigOut(device_id=device.id, if_descr=row.if_descr, enabled=row.enabled)


# ---------------------------------------------------------------------------
# Backup Config  (+ Backup History)
# ---------------------------------------------------------------------------
def _to_history_entry(snapshot: ConfigSnapshot) -> BackupHistoryEntry:
    return BackupHistoryEntry(
        id=snapshot.id,
        device_id=snapshot.device_id,
        change_request_id=snapshot.change_request_id,
        version=snapshot.version,
        checksum=snapshot.checksum,
        has_startup_config=bool(snapshot.startup_config_encrypted),
        created_at=snapshot.created_at,
    )


@router.post("/backup", response_model=BackupConfigResponse, status_code=201)
def backup_config(
    device_id: uuid.UUID,
    payload: BackupConfigRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(CONFIG_WRITE_ROLES),
):
    """On-demand configuration backup (FR: Backup Config).

    Reads the device's live running config via the Protocol Manager and
    persists it as an immutable ConfigSnapshot -- the same storage used by
    the automatic pre-deployment snapshot and the rollback history, so a
    manual backup shows up in Backup History / snapshot history either way.
    """
    device = _get_device(db, device_id)
    label = payload.label if payload else None
    try:
        from app.services import backup_service

        snapshot = backup_service.run_device_config_backup(db, device, current_user.email, label=label)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc) or "Failed to read device configuration for backup")

    return BackupConfigResponse(
        snapshot=_to_history_entry(snapshot),
        protocol=None,
        message=f"Backed up {device.hostname} configuration (v{snapshot.version}).",
    )


@router.get("/backups", response_model=list[BackupHistoryEntry])
def backup_history(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Full backup / configuration version history for a device, newest first."""
    device = _get_device(db, device_id)
    snapshots = list_snapshots(db, device.id)
    return [_to_history_entry(s) for s in snapshots]


# ---------------------------------------------------------------------------
# Download Config
# ---------------------------------------------------------------------------
@router.get("/backups/{snapshot_id}/download")
def download_backup(
    device_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    device = _get_device(db, device_id)
    snapshot = _get_snapshot_for_device(db, device_id, snapshot_id)
    config_text = snapshot_service.decrypt_config(snapshot.running_config_encrypted)
    filename = f"{device.hostname}_v{snapshot.version}_{snapshot.checksum[:8]}.cfg"
    return Response(
        content=config_text,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Restore Config
# ---------------------------------------------------------------------------
@router.post("/restore", response_model=RestoreConfigResponse)
def restore_config(
    device_id: uuid.UUID,
    payload: RestoreConfigRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(CONFIG_WRITE_ROLES),
):
    """Restore a device directly to a prior backup (FR: Restore Config).

    This is a direct, immediate config push via the Protocol Manager --
    for a governed restore that goes through the full
    snapshot -> deploy -> health-monitor -> auto-rollback pipeline as a
    tracked Change Request instead, use POST /devices/{id}/rollback.
    A pre-restore snapshot of the device's current state is still taken
    first so the restore itself is never undoable, and a post-restore
    snapshot is captured on success to keep the backup history accurate.
    """
    device = _get_device(db, device_id)
    snapshot = _get_snapshot_for_device(db, device_id, payload.snapshot_id)
    restored_config = snapshot_service.decrypt_config(snapshot.running_config_encrypted)

    pm = ProtocolManager(db, device, operator=current_user.email)

    # Pre-restore safety snapshot of whatever is live right now.
    pre_restore = pm.backup_config()
    if pre_restore.success:
        pre_version = str(int(datetime.datetime.utcnow().timestamp()))
        pre_payload = snapshot_service.build_snapshot_payload(pre_restore.output, pre_restore.startup_config, pre_version)
        db.add(ConfigSnapshot(device_id=device.id, seq=snapshot_service.next_seq(db), **pre_payload))
        db.commit()

    result = pm.restore_config(restored_config)

    post_restore_id = None
    if result.success:
        post = pm.backup_config()
        if post.success:
            post_version = str(int(datetime.datetime.utcnow().timestamp()))
            post_payload = snapshot_service.build_snapshot_payload(post.output, post.startup_config, post_version)
            post_snapshot = ConfigSnapshot(device_id=device.id, seq=snapshot_service.next_seq(db), **post_payload)
            db.add(post_snapshot)
            db.commit()
            db.refresh(post_snapshot)
            post_restore_id = post_snapshot.id

    audit_service.record_event(
        db,
        actor=current_user.email, tenant_id=current_user.tenant_id,
        action="Configuration Restore",
        result="Success" if result.success else "Failed",
        device_hostname=device.hostname,
        detail=(
            f"restored_from={snapshot.id} version={snapshot.version}"
            + (f" reason={payload.reason}" if payload.reason else "")
            + ("" if result.success else f" error={result.error}")
        ),
    )

    if not result.success:
        raise HTTPException(status_code=502, detail=result.error or "Failed to restore configuration to device")

    return RestoreConfigResponse(
        device_id=device.id,
        hostname=device.hostname,
        restored_from_snapshot_id=snapshot.id,
        post_restore_snapshot_id=post_restore_id,
        protocol=result.protocol.value if hasattr(result.protocol, "value") else str(result.protocol),
        success=True,
        message=f"Restored {device.hostname} to configuration v{snapshot.version}.",
    )


# ---------------------------------------------------------------------------
# Compare Configurations
# ---------------------------------------------------------------------------
@router.post("/compare", response_model=CompareConfigResponse)
def compare_config(
    device_id: uuid.UUID,
    payload: CompareConfigRequest,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    device = _get_device(db, device_id)

    def _resolve(snapshot_id: uuid.UUID | None) -> tuple[str, str]:
        """Returns (label, config_text). None => live running config."""
        if snapshot_id is None:
            pm = ProtocolManager(db, device)
            result = pm.get_running_config()
            if not result.success:
                raise HTTPException(status_code=502, detail=result.error or "Failed to read live running config")
            return "live running config", result.output
        snap = _get_snapshot_for_device(db, device_id, snapshot_id)
        return f"backup v{snap.version}", snapshot_service.decrypt_config(snap.running_config_encrypted)

    if payload.base_snapshot_id is None and payload.target_snapshot_id is None:
        latest = _latest_snapshot(db, device_id)
        if latest is None:
            raise HTTPException(status_code=404, detail="No backups exist yet for this device to compare against.")
        base_label, base_config = f"backup v{latest.version}", snapshot_service.decrypt_config(
            latest.running_config_encrypted
        )
        target_label, target_config = _resolve(None)
    else:
        base_label, base_config = _resolve(payload.base_snapshot_id)
        target_label, target_config = _resolve(payload.target_snapshot_id)

    identical = base_config == target_config

    # Diff on pretty-printed text when either side is XML (NETCONF
    # configs) -- diffing raw, unindented XML produces a single-line
    # "everything changed" diff that's useless to a human. The equality
    # check above still compares the untouched raw text so "identical"
    # reflects the device's actual bytes, not a formatting artifact.
    diff_base = config_format_service.pretty_xml(base_config) or base_config
    diff_target = config_format_service.pretty_xml(target_config) or target_config
    diff = diff_engine.generate_diff(diff_base, diff_target)

    return CompareConfigResponse(
        device_id=device.id,
        base_label=base_label,
        target_label=target_label,
        identical=identical,
        diff=diff,
    )


# ---------------------------------------------------------------------------
# Golden Config (approved baseline) -- SRS: one authoritative, approved
# configuration per device, used as the comparison target here and as the
# baseline for Drift Detection (see app.services.drift_service) when a
# device's ConfigDrift.baseline is GOLDEN_CONFIG rather than the previous
# backup. Previously the GoldenConfig model existed (and drift_service
# already read from it) but nothing ever let anyone create/update one --
# there was no "add golden config" option anywhere.
# ---------------------------------------------------------------------------
def _golden_config_to_read(golden: GoldenConfig) -> GoldenConfigRead:
    config_text = snapshot_service.decrypt_config(golden.config_encrypted)
    is_xml = config_format_service.looks_like_xml(config_text)
    return GoldenConfigRead(
        device_id=golden.device_id,
        config=config_text,
        config_pretty=config_format_service.pretty_xml(config_text) if is_xml else None,
        is_xml=is_xml,
        checksum=golden.checksum,
        set_by=golden.set_by,
        created_at=golden.created_at,
        updated_at=golden.updated_at,
    )


@router.get("/golden-config", response_model=GoldenConfigRead)
def get_golden_config(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    device = _get_device(db, device_id)
    golden = db.query(GoldenConfig).filter(GoldenConfig.device_id == device.id).first()
    if not golden:
        raise HTTPException(
            status_code=404,
            detail="No golden config set for this device yet. PUT this endpoint (or use a backup as the "
                   "source) to approve one.",
        )
    return _golden_config_to_read(golden)


@router.put("/golden-config", response_model=GoldenConfigRead)
def set_golden_config(
    device_id: uuid.UUID,
    payload: GoldenConfigSet,
    db: Session = Depends(get_db),
    current_user: User = Depends(CONFIG_WRITE_ROLES),
):
    """Sets (or replaces) the device's golden config -- the one
    authoritative, approved baseline used for manual comparison here and
    for Drift Detection when configured to compare against
    GOLDEN_CONFIG rather than the previous backup. One row per device
    (upsert: creates it if missing, overwrites if one already exists) --
    a golden config is a single current approved state, not a history.
    """
    device = _get_device(db, device_id)
    if not payload.config or not payload.config.strip():
        raise HTTPException(status_code=400, detail="Golden config cannot be empty")

    golden = db.query(GoldenConfig).filter(GoldenConfig.device_id == device.id).first()
    if golden is None:
        golden = GoldenConfig(device_id=device.id)
        db.add(golden)

    golden.config_encrypted = snapshot_service.encrypt_config(payload.config)
    golden.checksum = snapshot_service.compute_checksum(payload.config)
    golden.set_by = current_user.email
    db.commit()
    db.refresh(golden)

    audit_service.record_event(
        db, actor=current_user.email, tenant_id=current_user.tenant_id, action="Golden Config Set", result="Success",
        device_hostname=device.hostname, detail=f"checksum={golden.checksum}",
    )

    return _golden_config_to_read(golden)


@router.delete("/golden-config", status_code=204)
def clear_golden_config(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(CONFIG_WRITE_ROLES),
):
    device = _get_device(db, device_id)
    golden = db.query(GoldenConfig).filter(GoldenConfig.device_id == device.id).first()
    if golden:
        db.delete(golden)
        db.commit()
        audit_service.record_event(
            db, actor=current_user.email, tenant_id=current_user.tenant_id, action="Golden Config Cleared", result="Success",
            device_hostname=device.hostname,
        )
    return Response(status_code=204)


@router.post("/golden-config/from-backup/{snapshot_id}", response_model=GoldenConfigRead)
def set_golden_config_from_backup(
    device_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(CONFIG_WRITE_ROLES),
):
    """Convenience path: approve an existing backup as the golden config,
    instead of pasting the config text in by hand -- the common real
    workflow is "this backup is known-good, make it the baseline."
    """
    device = _get_device(db, device_id)
    snapshot = db.get(ConfigSnapshot, snapshot_id)
    if not snapshot or snapshot.device_id != device_id:
        raise HTTPException(status_code=404, detail="Snapshot not found for this device")

    config_text = snapshot_service.decrypt_config(snapshot.running_config_encrypted)

    golden = db.query(GoldenConfig).filter(GoldenConfig.device_id == device.id).first()
    if golden is None:
        golden = GoldenConfig(device_id=device.id)
        db.add(golden)

    golden.config_encrypted = snapshot_service.encrypt_config(config_text)
    golden.checksum = snapshot_service.compute_checksum(config_text)
    golden.set_by = current_user.email
    db.commit()
    db.refresh(golden)

    audit_service.record_event(
        db, actor=current_user.email, tenant_id=current_user.tenant_id, action="Golden Config Set From Backup", result="Success",
        device_hostname=device.hostname, detail=f"snapshot={snapshot.id} checksum={golden.checksum}",
    )

    return _golden_config_to_read(golden)


@router.post("/golden-config/compare", response_model=GoldenConfigCompareResponse)
def compare_golden_config(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Compares the device's live running config against its golden
    config -- the manual, on-demand equivalent of what Drift Detection
    checks on a schedule when baseline=GOLDEN_CONFIG."""
    device = _get_device(db, device_id)
    golden = db.query(GoldenConfig).filter(GoldenConfig.device_id == device.id).first()
    if not golden:
        raise HTTPException(status_code=404, detail="No golden config set for this device yet.")

    pm = ProtocolManager(db, device)
    result = pm.get_running_config()
    if not result.success:
        raise HTTPException(status_code=502, detail=result.error or "Failed to read live running config")

    golden_text = snapshot_service.decrypt_config(golden.config_encrypted)
    identical = golden_text == result.output

    diff_base = config_format_service.pretty_xml(golden_text) or golden_text
    diff_target = config_format_service.pretty_xml(result.output) or result.output
    diff = diff_engine.generate_diff(diff_base, diff_target)

    return GoldenConfigCompareResponse(device_id=device.id, identical=identical, diff=diff)


# ---------------------------------------------------------------------------
# Snapshot Retention (visible policy, not just background enforcement)
# ---------------------------------------------------------------------------
@router.get("/retention", response_model=RetentionPolicyResponse)
def device_retention_status(device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """The retention policy plus this device's current snapshot counts
    against it (kept / protected / eligible for the next nightly purge).
    Powers the "Snapshot Retention" note on the Backups tab.
    """
    device = _get_device(db, device_id)
    status = snapshot_service.retention_status_for_device(db, device.id)
    return RetentionPolicyResponse(
        policy=RetentionPolicy(**snapshot_service.retention_policy()),
        device=DeviceRetentionStatus(**status),
    )


@snapshot_policy_router.get("/retention-policy", response_model=RetentionPolicyResponse)
def fleet_retention_policy(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Fleet-wide retention policy, with no device context -- the plain
    "what's our retention policy" answer for anywhere in the app that
    isn't already looking at one device (e.g. a settings/admin page).
    """
    return RetentionPolicyResponse(policy=RetentionPolicy(**snapshot_service.retention_policy()), device=None)
