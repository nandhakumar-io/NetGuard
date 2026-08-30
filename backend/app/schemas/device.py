import datetime
import enum
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.models.device import (
    DeviceLifecycleState,
    DeviceStatus,
    DeviceVendor,
    SnmpAuthProtocol,
    SnmpPrivProtocol,
    SnmpSecurityLevel,
    SnmpVersion,
)


class DeviceBase(BaseModel):
    hostname: str
    ip_address: str
    vendor: DeviceVendor = DeviceVendor.CISCO
    site: str | None = None
    device_type: str | None = None
    # Structured group assignment (rack/datacenter/site/custom) -- see
    # app.models.device_group.DeviceGroup. Distinct from the free-text
    # `site` field above.
    group_id: uuid.UUID | None = None
    # Compliance/topology role (e.g. "core", "distribution", "access",
    # "edge-firewall") -- selects which ComplianceBaseline a
    # DriftBaseline.ROLE_BASELINE scan compares this device against.
    # Distinct from device_type: two devices can share a device_type
    # ("switch") but need different baselines because they're different
    # device_roles ("core" vs "access").
    device_role: str | None = None
    # Explicit WAN/uplink flag -- see Device.is_uplink's docstring.
    # Independent of device_role: an operator can mark a device as an
    # uplink without needing to know (or fit into) the role-keyword
    # convention the dashboard widget used to rely on exclusively.
    is_uplink: bool = False
    # Explicit "on the Core & Critical Devices shortlist" pin -- see
    # Device.is_pinned_critical's docstring.
    is_pinned_critical: bool = False
    # Physical placement (Groups page: Data Center -> Rack -> device).
    # Free-text on purpose -- see Device.data_center/rack in
    # app/models/device.py. rack_position is a cosmetic 1-based U slot,
    # not validated against rack height.
    data_center: str | None = None
    rack: str | None = None
    rack_position: int | None = None
    lifecycle_state: DeviceLifecycleState = DeviceLifecycleState.PRODUCTION
    tags: list[str] = Field(default_factory=list)
    custom_fields: dict[str, str] = Field(default_factory=dict)
    ssh_username: str | None = None
    ssh_credential_ref: str | None = None

    # --- SNMP Monitoring (Health Dashboard) ---
    supports_snmp: bool = False
    # See Device.snmp_stack_aware docstring -- take the worst stack
    # member's CPU/memory instead of the lowest table-index row.
    snmp_stack_aware: bool = False
    snmp_version: SnmpVersion | None = None
    snmp_port: int | None = 161
    snmp_community_ref: str | None = None  # v1/v2c (legacy env-var fallback)
    snmp_username: str | None = None  # v3
    snmp_auth_credential_ref: str | None = None  # v3 auth passphrase (legacy env-var fallback)
    snmp_privacy_credential_ref: str | None = None  # v3 priv passphrase (legacy env-var fallback)
    # v3 USM parameters -- not secrets, just protocol choice (the actual
    # auth/priv passphrases are set via POST /devices/{id}/snmp-credentials
    # and are never exposed through this schema; see DeviceRead below).
    snmp_security_level: SnmpSecurityLevel | None = None
    snmp_auth_protocol: SnmpAuthProtocol | None = None
    snmp_priv_protocol: SnmpPrivProtocol | None = None

    # --- NETCONF / RESTCONF (Protocol Manager) ---
    supports_netconf: bool = False
    netconf_port: int | None = 830
    # Whether push_config/restore_config should <lock>/<unlock> the
    # datastore around edit-config. Off for devices whose NETCONF agent
    # doesn't implement (or rejects) <lock> -- otherwise every deploy to
    # that device fails at the lock step. See Device.netconf_use_lock.
    netconf_use_lock: bool = True
    supports_restconf: bool = False
    restconf_url: str | None = None

    # --- Discovered / manually-entered inventory detail ---
    platform: str | None = None
    model: str | None = None
    serial_number: str | None = None
    os_version: str | None = None
    capabilities: str | None = None

    # --- Lab / simulation backing (GNS3) ---
    is_simulated: bool = False
    lab_provider: str | None = None
    gns3_project_id: str | None = None
    gns3_node_id: str | None = None
    console_host: str | None = None
    console_port: int | None = None
    console_type: str | None = None
    bootstrapped: bool = False

    # Which post-deployment health checks (health_monitor.ALL_CHECKS keys)
    # to actually run for this device. None/omitted means "run everything".
    enabled_health_checks: list[str] | None = None


class DeviceCreate(DeviceBase):
    pass


class DeviceUpdate(BaseModel):
    hostname: str | None = None
    ip_address: str | None = None
    vendor: DeviceVendor | None = None
    site: str | None = None
    device_type: str | None = None
    device_role: str | None = None
    is_uplink: bool | None = None
    is_pinned_critical: bool | None = None
    data_center: str | None = None
    rack: str | None = None
    rack_position: int | None = None
    lifecycle_state: DeviceLifecycleState | None = None
    tags: list[str] | None = None
    custom_fields: dict[str, str] | None = None
    group_id: uuid.UUID | None = None
    ssh_username: str | None = None
    ssh_credential_ref: str | None = None
    supports_snmp: bool | None = None
    snmp_stack_aware: bool | None = None
    snmp_version: SnmpVersion | None = None
    snmp_port: int | None = None
    snmp_community_ref: str | None = None
    snmp_username: str | None = None
    snmp_auth_credential_ref: str | None = None
    snmp_privacy_credential_ref: str | None = None
    snmp_security_level: SnmpSecurityLevel | None = None
    snmp_auth_protocol: SnmpAuthProtocol | None = None
    snmp_priv_protocol: SnmpPrivProtocol | None = None
    supports_netconf: bool | None = None
    netconf_port: int | None = None
    netconf_use_lock: bool | None = None
    supports_restconf: bool | None = None
    restconf_url: str | None = None
    platform: str | None = None
    model: str | None = None
    serial_number: str | None = None
    os_version: str | None = None
    capabilities: str | None = None
    is_simulated: bool | None = None
    lab_provider: str | None = None
    gns3_project_id: str | None = None
    gns3_node_id: str | None = None
    console_host: str | None = None
    console_port: int | None = None
    console_type: str | None = None
    bootstrapped: bool | None = None
    enabled_health_checks: list[str] | None = None
    # Discovery at Scale: per-device override of the fleet-wide poll
    # cadence (app.core.config.settings.SNMP_POLL_INTERVAL_SECONDS /
    # REACHABILITY_POLL_INTERVAL_SECONDS). None/omitted leaves the device
    # on the fleet default; set explicitly to poll a device tighter
    # (core router) or looser (metered WAN link, rarely-changing access
    # switch) than the rest of the fleet.
    snmp_poll_interval_seconds: int | None = None
    reachability_poll_interval_seconds: int | None = None


class DeviceRead(DeviceBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: DeviceStatus
    flagged_unstable: bool = False
    unstable_since: datetime.datetime | None = None
    # Derived, not stored: whether *some* SNMP secret is on file for this
    # device (community for v1/v2c, or a v3 auth/priv key), without ever
    # exposing the encrypted columns themselves through this schema. Lets
    # the UI show "credentials configured" vs. "not configured" honestly.
    snmp_credentials_configured: bool = False
    # Same idea for the DB-encrypted SSH password (POST
    # /devices/{id}/ssh-credentials). ssh_credential_ref itself is already
    # plain on DeviceBase (it's just a pointer, not a secret) -- this flag
    # is specifically about whether a real password is on file.
    ssh_credentials_configured: bool = False
    # Whether an SSH private key is on file (POST /devices/{id}/ssh-credentials
    # private_key field), independent of ssh_credentials_configured (password).
    ssh_key_configured: bool = False
    # "password" (default) or "key" -- which credential the terminal
    # currently presents for this device. Plain field (not derived), safe
    # to expose since it's not a secret.
    ssh_auth_method: str = "password"
    # Set by every SNMP poll attempt (success or failure) -- see
    # metrics_service.poll_device. None until the first poll ever runs.
    last_snmp_poll_at: datetime.datetime | None = None
    last_snmp_poll_error: str | None = None
    # See DeviceUpdate for what these override; last_reachability_poll_at
    # is reachability's equivalent of last_snmp_poll_at above, stamped by
    # app.tasks.reachability_task on every attempt.
    snmp_poll_interval_seconds: int | None = None
    reachability_poll_interval_seconds: int | None = None
    last_reachability_poll_at: datetime.datetime | None = None
    # Derived from eol_service.check_device_eol against device.vendor/
    # model/os_version -- never stored, always computed fresh so it stays
    # correct if EOL_DATABASE gets updated without touching this device
    # row. None fields mean "not in the EOL database" (unknown), not
    # "confirmed supported" -- see eol_service module docstring.
    eol_matched: bool = False
    eol_platform_label: str | None = None
    is_eos: bool = False
    is_eol: bool = False
    eos_date: datetime.date | None = None
    eol_date: datetime.date | None = None
    eol_note: str | None = None
    # Independent of the EOS/EOL fields above -- a device can be fully
    # supported today and still be behind the platform's recommended
    # build. None means no target has been curated for this platform.
    recommended_target_version: str | None = None
    needs_upgrade: bool = False
    # Fleet-list "at a glance" fields -- lets the Devices table show
    # health and open-alert state inline without the user clicking into
    # each row individually. Sourced from a single bulk VictoriaMetrics
    # query + a single grouped alert count query in list_devices (not a
    # per-device call), same cost-avoidance pattern as
    # metrics_service.fleet_health_summary. None means "no SNMP health
    # sample yet" (matches DeviceHealthSummary's per-device shape), not
    # "confirmed healthy".
    health_score: int | None = None
    health_color: str | None = None
    open_alert_count: int = 0
    critical_alert_count: int = 0
    # Same bulk-fetch pattern as health_score/health_color above (see
    # list_devices) -- CPU/memory/uptime shown directly in the fleet-list
    # row so "which of these is under load / how long has it been up"
    # doesn't require clicking into every device individually either.
    cpu_utilization_pct: float | None = None
    memory_utilization_pct: float | None = None
    uptime_seconds: int | None = None
    last_polled_at: datetime.datetime | None = None

    @classmethod
    def from_device(cls, device, *, health: dict | None = None, open_alert_count: int = 0, critical_alert_count: int = 0) -> "DeviceRead":
        import json as _json

        # enabled_health_checks is stored as a JSON-encoded string
        # (Device.enabled_health_checks : Text) but exposed as a list --
        # model_validate would otherwise choke trying to coerce a raw
        # string into list[str].
        raw = getattr(device, "enabled_health_checks", None)
        checks: list[str] | None = None
        if raw:
            try:
                parsed = _json.loads(raw)
                if isinstance(parsed, list):
                    checks = parsed
            except (ValueError, TypeError):
                checks = None

        raw_tags = getattr(device, "tags", None)
        tags: list[str] = []
        if raw_tags:
            try:
                parsed_tags = _json.loads(raw_tags)
                if isinstance(parsed_tags, list):
                    tags = [str(t) for t in parsed_tags]
            except (ValueError, TypeError):
                tags = []

        raw_custom = getattr(device, "custom_fields", None)
        custom_fields: dict[str, str] = {}
        if raw_custom:
            try:
                parsed_custom = _json.loads(raw_custom)
                if isinstance(parsed_custom, dict):
                    custom_fields = {str(k): str(v) for k, v in parsed_custom.items()}
            except (ValueError, TypeError):
                custom_fields = {}

        obj = cls.model_construct(
            **{
                k: getattr(device, k, None)
                for k in cls.model_fields
                if k not in ("enabled_health_checks", "tags", "custom_fields")
            },
            enabled_health_checks=checks,
            tags=tags,
            custom_fields=custom_fields,
        )
        obj.snmp_credentials_configured = bool(
            device.snmp_community_encrypted or device.snmp_auth_key_encrypted or device.snmp_priv_key_encrypted
        )
        obj.ssh_credentials_configured = bool(device.ssh_password_encrypted)
        obj.ssh_key_configured = bool(device.ssh_private_key_encrypted)
        obj.ssh_auth_method = getattr(device, "ssh_auth_method", None) or "password"

        from app.services import eol_service

        eol = eol_service.check_device_eol(
            vendor=getattr(device, "vendor", None).value if getattr(device, "vendor", None) else None,
            model=getattr(device, "model", None),
            os_version=getattr(device, "os_version", None),
        )
        obj.eol_matched = eol.matched
        obj.eol_platform_label = eol.platform_label
        obj.is_eos = eol.is_eos
        obj.is_eol = eol.is_eol
        obj.eos_date = eol.eos_date
        obj.eol_date = eol.eol_date
        obj.eol_note = eol.note
        obj.recommended_target_version = eol.recommended_target_version
        obj.needs_upgrade = eol.needs_upgrade

        obj.health_score = int(health["health_score"]) if health and health.get("health_score") is not None else None
        obj.health_color = health.get("health_color") if health else None
        obj.open_alert_count = open_alert_count
        obj.critical_alert_count = critical_alert_count
        obj.cpu_utilization_pct = health.get("cpu_utilization_pct") if health else None
        obj.memory_utilization_pct = health.get("memory_utilization_pct") if health else None
        obj.uptime_seconds = int(health["uptime_seconds"]) if health and health.get("uptime_seconds") is not None else None
        obj.last_polled_at = health.get("polled_at") if health else None
        return obj


class SnmpCredentialsUpdate(BaseModel):
    """Body for POST /devices/{id}/snmp-credentials. Only fields actually
    provided are touched/encrypted -- omit a field (or send it as null)
    to leave the currently-stored value untouched; send "" to explicitly
    clear it. Never round-tripped back out via DeviceRead.
    """

    community: str | None = None  # v1/v2c
    v3_auth_key: str | None = None  # v3 auth passphrase (authNoPriv/authPriv)
    v3_priv_key: str | None = None  # v3 privacy passphrase (authPriv)


class SnmpTestResult(BaseModel):
    success: bool
    message: str
    sys_descr: str | None = None
    sys_uptime_seconds: int | None = None


class ConnectionDiagnosticStep(BaseModel):
    name: str
    success: bool
    detail: str


class ConnectionTestAndFixResult(BaseModel):
    """Response for POST /devices/{id}/connection/test-and-fix -- see that
    endpoint's docstring for what each step checks and why the fix step
    exists at all (the classic "SNMP works but the device still shows
    offline" complaint, caused by the ping/TCP reachability sweep and SNMP
    polling disagreeing about whether the device is up)."""

    steps: list[ConnectionDiagnosticStep]
    overall_success: bool
    status_before: str
    status_after: str
    fix_applied: bool
    fix_detail: str | None = None


class SshCredentialsUpdate(BaseModel):
    """Body for POST /devices/{id}/ssh-credentials. Mirrors
    SnmpCredentialsUpdate: only fields actually provided are touched.
    username is stored plain (it's not a secret, same as ssh_username on
    DeviceBase always was); password is encrypted at rest and never
    returned by any GET endpoint. Send password="" to explicitly clear it.
    """

    username: str | None = None
    password: str | None = None
    # Key-based auth alternative to `password` -- PEM/OpenSSH-format
    # private key text, encrypted at rest (see credential_service.
    # set_ssh_private_key). Send private_key="" to explicitly clear it.
    private_key: str | None = None
    private_key_passphrase: str | None = None
    # "password" or "key" -- which credential the terminal should present.
    # Omit to leave the device's current setting unchanged.
    auth_method: str | None = None


class SshTestResult(BaseModel):
    success: bool
    message: str
    protocol: str | None = None  # which transport actually answered: netconf / restconf / ssh / telnet


class ArpEntry(BaseModel):
    if_index: str
    ip_address: str
    mac_address: str


class RouteEntry(BaseModel):
    destination: str
    mask: str | None = None
    next_hop: str
    if_index: str | None = None


class LldpNeighbor(BaseModel):
    local_port_index: str
    # Real local interface name (e.g. "ge-0/0/0"), resolved from
    # lldpLocPortTable in snmp_service._resolve_lldp_local_port_names.
    # Falls back to local_port_index itself when that lookup couldn't
    # resolve a name -- see that function's docstring.
    local_port: str | None = None
    neighbor_name: str | None = None
    neighbor_port: str | None = None
    # Raw lldpRemChassisId -- always populated when a neighbor row exists
    # at all (mandatory TLV, unlike System Name). Useful on its own when
    # neighbor_name had to fall back to this same value because the
    # neighbor didn't send an optional System Name TLV (common on Junos).
    neighbor_chassis_id: str | None = None
    # Best-effort switchport mode/VLAN for local_port, via the same
    # Q-BRIDGE-MIB walk the device Interfaces tab uses (see
    # app.services.snmp_service.walk_switchport_vlans) -- filled in by
    # discover_device after the base LLDP walk, matched by local_port
    # name. None if unresolved (no SNMP switchport data for that port).
    port_mode: str | None = None
    vlan: str | None = None
    trunk_vlans: list[str] | None = None


class CdpNeighbor(BaseModel):
    local_if_index: str
    # Real local interface name, resolved from ifTable's ifDescr in
    # snmp_service._discover_cdp_neighbors. Falls back to local_if_index
    # itself when that lookup couldn't resolve a name.
    local_port: str | None = None
    neighbor_id: str | None = None
    neighbor_port: str | None = None
    neighbor_platform: str | None = None
    # Same best-effort switchport enrichment as LldpNeighbor above.
    port_mode: str | None = None
    vlan: str | None = None
    trunk_vlans: list[str] | None = None


class InventoryItem(BaseModel):
    index: str
    name: str | None = None
    description: str | None = None
    model: str | None = None
    serial_number: str | None = None


class DeviceCsvImportError(BaseModel):
    row: int
    hostname: str | None = None
    error: str


class DeviceCsvImportResult(BaseModel):
    """Result of POST /devices/import. `created`/`updated` list the
    hostnames actually written; `errors` covers per-row problems (bad
    vendor value, missing required column, ...) -- a row with an error
    doesn't block the rest of the file from importing.
    """

    created: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    errors: list[DeviceCsvImportError] = Field(default_factory=list)
    total_rows: int = 0


class BulkDeviceAction(str, enum.Enum):
    MOVE_GROUP = "move_group"
    ASSIGN_TAGS = "assign_tags"
    SET_LIFECYCLE_STATE = "set_lifecycle_state"
    APPLY_CONFIG_TEMPLATE = "apply_config_template"
    ADD_MAINTENANCE_WINDOW = "add_maintenance_window"
    ROTATE_CREDENTIALS = "rotate_credentials"


class BulkDeviceActionRequest(BaseModel):
    """Body for POST /devices/bulk. `params` is interpreted based on
    `action`:

      - move_group: {"group_id": "<uuid>" | None}
      - assign_tags: {"tags": ["a","b"], "mode": "add" | "replace"}
        (mode defaults to "add" -- union with each device's existing tags)
      - set_lifecycle_state: {"lifecycle_state": "staging"|"production"|"decommissioned"}
      - apply_config_template: {"template_id": "<uuid>", "variables": {...},
        "description": "...", "priority": "medium"} -- creates a single
        multi-device ChangeRequest (see app.api.change_requests) covering
        all selected devices, it does not push config directly.
      - add_maintenance_window: {"name": "...", "reason": "...",
        "start": "<iso datetime>", "end": "<iso datetime>"} -- creates one
        DEVICE-scoped MaintenanceWindow per selected device.
      - rotate_credentials: {"ssh_password": "...", "ssh_username": "...",
        "snmp_community": "...", "snmp_v3_auth_key": "...",
        "snmp_v3_priv_key": "..."} -- sets the same new secret(s) on every
        selected device (Fernet-encrypted at rest via credential_service,
        same as the single-device ssh-credentials/snmp-credentials
        endpoints). Only the fields present are rotated; omit a field to
        leave that credential untouched on every device. Pass "" for a
        field to explicitly clear it fleet-wide instead of setting it.
    """

    device_ids: list[uuid.UUID]
    action: BulkDeviceAction
    params: dict = Field(default_factory=dict)


class BulkDeviceActionResult(BaseModel):
    action: BulkDeviceAction
    affected_device_ids: list[uuid.UUID] = Field(default_factory=list)
    failed: dict[str, str] = Field(default_factory=dict)  # device_id (str) -> error message
    detail: str | None = None
    change_request_id: uuid.UUID | None = None


class DeviceDiscoveryResult(BaseModel):
    """SNMP-based discovery for Cisco devices: hostname, ARP table,
    routing table, LLDP/CDP neighbors, and chassis/module inventory.
    Every sub-list is independently best-effort -- an empty list means
    that table wasn't available/populated on the device, not that the
    whole discovery call failed (see snmp_service.discover_inventory)."""

    device_id: uuid.UUID
    hostname: str | None = None
    reported_hostname: str | None = None  # device's own sysName, if it answered
    arp_table: list[ArpEntry] = Field(default_factory=list)
    routing_table: list[RouteEntry] = Field(default_factory=list)
    lldp_neighbors: list[LldpNeighbor] = Field(default_factory=list)
    cdp_neighbors: list[CdpNeighbor] = Field(default_factory=list)
    inventory: list[InventoryItem] = Field(default_factory=list)
    # Best-effort single values derived from sysDescr + ENTITY-MIB
    # (entPhysicalTable's chassis row) -- see
    # snmp_service._detect_platform_from_sysdescr / _detect_chassis_summary.
    # These were being computed all along but discarded: never declared on
    # this response model (silently dropped by pydantic) and never written
    # back to Device.platform/model/serial_number. Now surfaced here *and*
    # persisted by the discovery endpoint so Overview page fields actually
    # populate instead of staying blank forever.
    detected_platform: str | None = None
    detected_model: str | None = None
    detected_serial_number: str | None = None
    detected_os_version: str | None = None
    retrieved_at: datetime.datetime
