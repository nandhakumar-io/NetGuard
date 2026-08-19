import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field


class RunningConfigResponse(BaseModel):
    """Live read of a device's running configuration (FR: View Running Config)."""

    device_id: uuid.UUID
    hostname: str
    protocol: str
    config: str
    # `config` is left byte-for-byte as returned by the device (needed for
    # diffing/restore) -- these two are display-only additions so the UI
    # can show something readable without touching the value used for
    # comparisons. config_pretty is None when `config` isn't XML (already
    # CLI text off an SSH/NAPALM read, or unparseable) -- fall back to
    # `config` in that case.
    config_pretty: str | None = None
    is_xml: bool = False
    retrieved_at: datetime.datetime


class StartupConfigResponse(BaseModel):
    """Startup configuration for a device (FR: View Startup Config).

    Startup config isn't polled live the way running config is -- it's
    carried on the most recent ConfigSnapshot taken for the device (every
    backup captures both). `source` tells the caller whether this came
    from a snapshot or is unavailable.
    """

    device_id: uuid.UUID
    hostname: str
    config: str | None
    config_pretty: str | None = None
    is_xml: bool = False
    source: str  # "snapshot" | "unavailable"
    snapshot_id: uuid.UUID | None = None
    retrieved_at: datetime.datetime


class InterfaceStatusOut(BaseModel):
    """One interface's operational status, normalized across NETCONF/
    RESTCONF/SSH-NAPALM backends -- see app.services.config_format_service."""

    name: str
    description: str | None = None
    admin_status: str | None = None
    oper_status: str | None = None
    ip_addresses: list[str] = Field(default_factory=list)
    mtu: int | None = None
    speed: str | None = None
    mac_address: str | None = None
    # Best-effort switchport enrichment (see snmp_service.walk_switchport_vlans)
    # -- None/unknown on platforms or protocols that don't expose it rather
    # than a guess, so the UI shows "--" instead of a wrong value.
    port_mode: str | None = None  # "access" | "trunk" | "routed" | None
    vlan: str | None = None  # access/native VLAN ID
    trunk_vlans: list[str] | None = None  # allowed/tagged VLAN IDs, only set when port_mode == "trunk"
    edge_port: bool | None = None  # STP edge/portfast state (Cisco CISCO-STP-EXTENSIONS-MIB); None where unsupported
    # Whether the automatic "Interface Down" critical alert is armed for
    # this port -- see InterfaceAlertConfig. True unless an operator has
    # explicitly muted it from the Interfaces tab.
    alerts_enabled: bool = True


class InterfacesResponse(BaseModel):
    device_id: uuid.UUID
    hostname: str
    protocol: str
    interfaces: list[InterfaceStatusOut]
    retrieved_at: datetime.datetime
    # Populated (interfaces == []) when the read itself failed, so the UI
    # can tell "device has no interfaces" apart from "couldn't reach it".
    error: str | None = None


class InterfaceAlertConfigUpdate(BaseModel):
    enabled: bool


class InterfaceAlertConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    device_id: uuid.UUID
    if_descr: str
    enabled: bool


class BackupHistoryEntry(BaseModel):
    """One entry in a device's configuration backup history (git-style log).

    Mirrors app.schemas.rollback.SnapshotSummary intentionally -- both
    represent a ConfigSnapshot row -- kept as a separate type here so the
    Configuration Management API's contract can evolve independently of
    the Rollback API's.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    device_id: uuid.UUID
    change_request_id: uuid.UUID | None = None
    version: str
    checksum: str
    has_startup_config: bool = False
    created_at: datetime.datetime


class BackupConfigRequest(BaseModel):
    label: str | None = Field(default=None, description="Optional human-readable note for this backup.")


class BackupConfigResponse(BaseModel):
    snapshot: BackupHistoryEntry
    protocol: str | None = None
    message: str


class RestoreConfigRequest(BaseModel):
    snapshot_id: uuid.UUID
    reason: str | None = None


class RestoreConfigResponse(BaseModel):
    device_id: uuid.UUID
    hostname: str
    restored_from_snapshot_id: uuid.UUID
    post_restore_snapshot_id: uuid.UUID | None = None
    protocol: str
    success: bool
    message: str


class CompareConfigRequest(BaseModel):
    """Compare two configurations for a device.

    Either side may be a snapshot_id (a prior backup) or, if omitted /
    set to "live", the device's current live running configuration.
    Omitting both `base_snapshot_id` and `target_snapshot_id` compares
    the most recent backup against the live running config -- the
    common "has this device drifted since its last backup?" check.
    """

    base_snapshot_id: uuid.UUID | None = None
    target_snapshot_id: uuid.UUID | None = None


class CompareConfigResponse(BaseModel):
    device_id: uuid.UUID
    base_label: str
    target_label: str
    identical: bool
    diff: str


# --- Golden Config (approved baseline) -------------------------------
class GoldenConfigRead(BaseModel):
    device_id: uuid.UUID
    config: str
    config_pretty: str | None = None
    is_xml: bool = False
    checksum: str
    set_by: str
    created_at: datetime.datetime
    updated_at: datetime.datetime


class GoldenConfigSet(BaseModel):
    """Body for PUT /devices/{id}/golden-config. `config` is required --
    there's no partial update, since a golden config is meant to be
    reviewed and set as a whole (typically from a known-good backup),
    not patched piecemeal.
    """

    config: str


class GoldenConfigCompareResponse(BaseModel):
    device_id: uuid.UUID
    identical: bool
    diff: str


# --- Snapshot retention policy (visible housekeeping, not just enforced) --
class RetentionPolicy(BaseModel):
    retention_days: int
    min_snapshots_per_device: int
    sweep_hour_utc: int
    description: str


class DeviceRetentionStatus(BaseModel):
    device_id: uuid.UUID
    total_snapshots: int
    protected_snapshots: int
    eligible_for_purge: int
    oldest_snapshot_at: datetime.datetime | None = None
    newest_snapshot_at: datetime.datetime | None = None


class RetentionPolicyResponse(BaseModel):
    policy: RetentionPolicy
    device: DeviceRetentionStatus | None = None
