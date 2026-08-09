import enum
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class DeviceVendor(str, enum.Enum):
    CISCO = "cisco"
    JUNIPER = "juniper"
    ARISTA = "arista"
    LINUX = "linux"


class DeviceStatus(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class DeviceLifecycleState(str, enum.Enum):
    """Where a device sits in its operational lifecycle, independent of
    live reachability (DeviceStatus above). STAGING is a device that's
    been added/discovered but not yet cut over into real traffic-serving
    production use (e.g. still being burned in, awaiting a maintenance
    window to go live). PRODUCTION is the default -- a live, in-service
    device, same as every device before this field existed. DECOMMISSIONED
    is a device kept in inventory for historical/audit purposes (past
    deployments, config history) after being pulled from service, rather
    than deleted outright.
    """

    STAGING = "staging"
    PRODUCTION = "production"
    DECOMMISSIONED = "decommissioned"


class SnmpVersion(str, enum.Enum):
    V1 = "v1"
    V2C = "v2c"
    V3 = "v3"


class SnmpSecurityLevel(str, enum.Enum):
    NO_AUTH_NO_PRIV = "noAuthNoPriv"
    AUTH_NO_PRIV = "authNoPriv"
    AUTH_PRIV = "authPriv"


class SnmpAuthProtocol(str, enum.Enum):
    MD5 = "MD5"
    SHA = "SHA"
    SHA224 = "SHA224"
    SHA256 = "SHA256"
    SHA384 = "SHA384"
    SHA512 = "SHA512"


class SnmpPrivProtocol(str, enum.Enum):
    DES = "DES"
    DES3 = "3DES"
    AES128 = "AES128"
    AES192 = "AES192"
    AES256 = "AES256"


class Device(Base):
    __tablename__ = "devices"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hostname = Column(String, unique=True, index=True, nullable=False)
    ip_address = Column(String, nullable=False)  # management IP
    vendor = Column(Enum(DeviceVendor), nullable=False, default=DeviceVendor.CISCO)
    site = Column(String, nullable=True)
    device_type = Column(String, nullable=True)  # e.g. router, switch, firewall
    # Compliance/topology role -- e.g. "core", "distribution", "access",
    # "edge-firewall", "wan-edge". Free-text (not an enum) like device_type,
    # since orgs name roles differently, but distinct from device_type: a
    # "switch" (device_type) can be a "core" or "access" switch (device_role)
    # -- those need different compliance baselines even though they're the
    # same device_type. See app.models.compliance_baseline.ComplianceBaseline
    # and drift_service's DriftBaseline.ROLE_BASELINE.
    device_role = Column(String, nullable=True)
    status = Column(Enum(DeviceStatus), nullable=False, default=DeviceStatus.UNKNOWN)

    # Lifecycle state (staging -> production -> decommissioned), separate
    # from the live-reachability `status` above -- a device can be
    # `status=offline` while `lifecycle_state=staging` (not cabled up
    # yet) or `status=online` while `lifecycle_state=decommissioned`
    # (still powered/reachable during a wind-down window). Defaults to
    # PRODUCTION so every pre-existing device (and any device created
    # without specifying this) behaves exactly as before this field
    # existed.
    lifecycle_state = Column(
        Enum(DeviceLifecycleState, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        nullable=False,
        default=DeviceLifecycleState.PRODUCTION,
        server_default=DeviceLifecycleState.PRODUCTION.value,
    )

    # JSON-encoded list of free-text labels (e.g. ["pci-scope",
    # "east-region", "auto-migrated"]) -- used for ad-hoc bulk filtering/
    # bulk-tagging from the Inventory page, and as a match target for
    # DeviceGroup dynamic membership rules (see
    # app.services.group_membership_service). Distinct from device_role/
    # device_type, which are single-value classification fields; a
    # device can carry any number of tags.
    tags = Column(Text, nullable=True)

    # JSON-encoded object of org-defined custom fields (e.g.
    # {"asset_tag": "AT-4471", "owner_team": "NetEng", "cost_center":
    # "CC-1029"}). Deliberately schemaless (Text, not real columns) so
    # an org can define whatever fields it needs without a migration
    # per field; the Inventory UI reads/writes this as a flat string ->
    # string map. See schemas.device.DeviceRead.from_device for the
    # decode side.
    custom_fields = Column(Text, nullable=True)

    # --- Physical/logical grouping (Device Grouping: rack + data center) ---
    # Free-text, same convention as `site`/`device_type`/`device_role` above
    # -- orgs name data centers and racks however they already do (site
    # codes, building names, "DC1", "Rack A12", ...) so this stays a plain
    # string rather than a rigid enum. data_center is the top grouping
    # level, rack is nested one level under it; a device with a rack but
    # no data_center still groups fine (falls under an "Unassigned" DC
    # bucket in the UI), same as devices with no site today.
    data_center = Column(String, nullable=True, index=True)
    rack = Column(String, nullable=True, index=True)
    # Optional 1-based slot/U position within the rack, purely cosmetic
    # (sorts devices top-to-bottom in the rack-elevation view) -- not
    # validated against rack height since we don't model rack capacity.
    rack_position = Column(Integer, nullable=True)

    # --- Named/logical device group (see app.models.device_group.DeviceGroup) ---
    # Distinct from data_center/rack above -- this is the explicit,
    # user-managed grouping ("Edge Firewalls", "Q3 Migration Batch"), not
    # physical placement. NULL means "not in any named group", same
    # ON DELETE SET NULL semantics as data_center/rack going empty.
    group_id = Column(UUID(as_uuid=True), ForeignKey("device_groups.id"), nullable=True, index=True)
    ssh_username = Column(String, nullable=True)
    ssh_credential_ref = Column(String, nullable=True)  # legacy: pointer to secret store, not raw secret
    # Fernet-encrypted (app.core.crypto) SSH password, set via
    # POST /devices/{id}/ssh-credentials -- same pattern as the SNMP
    # *_encrypted columns below, so an operator can enter the real SSH
    # password from the UI instead of hand-editing a NETGUARD_CRED_<REF>
    # env var on the server. Never returned by any GET endpoint. Takes
    # priority over ssh_credential_ref (env-var lookup) when present; see
    # credential_service.get_ssh_password.
    ssh_password_encrypted = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # --- Inventory detail (Device Inventory page columns) ---
    platform = Column(String, nullable=True)  # e.g. "IOS-XE", "Junos", "EOS"
    model = Column(String, nullable=True)  # e.g. "ISR4331"
    serial_number = Column(String, nullable=True)
    os_version = Column(String, nullable=True)

    # --- Protocol support flags (NETCONF / RESTCONF / SNMP) ---
    supports_netconf = Column(Boolean, nullable=False, default=False, server_default="false")
    supports_restconf = Column(Boolean, nullable=False, default=False, server_default="false")
    supports_snmp = Column(Boolean, nullable=False, default=False, server_default="false")
    # Set/cleared by every poll attempt (scheduled or on-demand), success
    # or failure -- see metrics_service.poll_device. Lets the UI show
    # *why* a device has no metrics (never polled / unreachable /
    # credentials incomplete) instead of just an empty Health tab.
    last_snmp_poll_at = Column(DateTime(timezone=True), nullable=True)
    last_snmp_poll_error = Column(Text, nullable=True)

    # --- Per-device polling cadence override (Discovery at Scale) ---
    # The fleet-wide defaults (settings.SNMP_POLL_INTERVAL_SECONDS /
    # REACHABILITY_POLL_INTERVAL_SECONDS) are fine at small counts, but a
    # real fleet needs some devices polled tighter (a core router) and
    # others looser (a rarely-changing branch access switch, or one on a
    # metered/low-bandwidth WAN link where frequent SNMP walks are
    # actually costly). NULL means "use the fleet default" -- see
    # app.tasks.run_snmp_poll_sweep_task / run_reachability_sweep_task,
    # which read these to decide whether a device is due yet, rather than
    # every device sharing one global cadence tied to beat's own tick.
    snmp_poll_interval_seconds = Column(Integer, nullable=True)
    reachability_poll_interval_seconds = Column(Integer, nullable=True)
    # Reachability's equivalent of last_snmp_poll_at above -- didn't
    # previously exist because nothing needed to know "was this device's
    # due time reached yet" until the sweep became interval-aware here.
    last_reachability_poll_at = Column(DateTime(timezone=True), nullable=True)

    # --- Per-metric "last successful read" timestamps ---
    # last_snmp_poll_at above only tells you when a poll *attempt* last
    # ran, not which individual OIDs in that attempt actually resolved.
    # A device can have a perfectly healthy, fresh CPU/memory reading
    # while its interface table has been failing to walk (ACL change,
    # agent restart mid-table, etc.) for days -- previously that showed
    # up nowhere: the device still looked fully green because CPU/mem
    # alone are enough to compute a health_score. Each column here is
    # stamped independently in metrics_service.poll_device, one per poll,
    # only when that specific reading resolved to a non-None value on
    # that poll -- so a metric that stops resolving simply stops
    # advancing its timestamp while the others keep moving, and the API/
    # UI can flag it as stale instead of silently folding it into an
    # overall "green" score derived from whatever did come back.
    last_cpu_success_at = Column(DateTime(timezone=True), nullable=True)
    last_memory_success_at = Column(DateTime(timezone=True), nullable=True)
    last_interface_success_at = Column(DateTime(timezone=True), nullable=True)
    last_temperature_success_at = Column(DateTime(timezone=True), nullable=True)
    last_fan_success_at = Column(DateTime(timezone=True), nullable=True)
    last_power_success_at = Column(DateTime(timezone=True), nullable=True)

    # NetBox device object ID (see app.services.netbox_service), if this
    # device was created or is kept in sync by a NetBox pull-sync. NULL
    # for manually-added/GNS3-discovered devices. Used as the match key
    # on re-sync instead of hostname, so a hostname rename in NetBox
    # updates the existing device instead of creating a duplicate.
    netbox_id = Column(Integer, nullable=True, unique=True, index=True)
    netbox_last_synced_at = Column(DateTime(timezone=True), nullable=True)

    # JSON-encoded list of health_monitor check_name values (e.g.
    # ["ping","packet_loss_latency","http"]) that the post-deployment
    # verification suite (FR-9) should actually run against this device.
    # NULL/empty means "run everything" (the historical, still-default
    # behavior). Lets an operator turn off checks that don't apply to a
    # given device (e.g. no BGP/OSPF configured, no NAPALM driver
    # installed) instead of every deploy failing verification -- and
    # triggering a rollback -- over a check that was never going to pass.
    enabled_health_checks = Column(Text, nullable=True)

    # --- NETCONF connection settings (ncclient) ---
    netconf_port = Column(Integer, nullable=True, default=830)
    # Whether netconf_service.push_config should <lock>/<unlock> the target
    # datastore around edit-config. Defaults on (the safe behavior for
    # devices that support it), but some NETCONF agents either don't
    # implement <lock> at all or reject it from a session that already
    # holds an implicit lock, which makes every deploy/restore to that
    # device fail at the lock step even though the edit-config itself
    # would have succeeded. Per-device escape hatch: turn this off for a
    # device that's confirmed to have that problem instead of losing
    # locking (and the safety it gives every other device) globally.
    netconf_use_lock = Column(Boolean, nullable=False, default=True, server_default="true")

    # --- RESTCONF connection settings ---
    restconf_url = Column(String, nullable=True)  # e.g. https://10.0.0.1/restconf

    # --- SNMP connection settings ---
    snmp_version = Column(Enum(SnmpVersion), nullable=True)
    snmp_port = Column(Integer, nullable=True, default=161)
    # Legacy env-var-ref pointers (see credential_service module docstring for
    # the ref-> NETGUARD_CRED_<REF> env var pattern). Still supported as a
    # fallback, but superseded by the *_encrypted columns below, which are
    # populated by POST /devices/{id}/snmp-credentials and let an operator
    # enter real SNMP secrets from the UI instead of hand-editing .env per
    # device.
    snmp_community_ref = Column(String, nullable=True)  # pointer to secret store (v1/v2c)
    snmp_username = Column(String, nullable=True)  # v3 (not secret, stored plain like ssh_username)
    snmp_auth_credential_ref = Column(String, nullable=True)  # v3 auth passphrase (secret store ref)
    snmp_privacy_credential_ref = Column(String, nullable=True)  # v3 priv passphrase (secret store ref)
    # SNMPv3 USM parameters (not secret; auth/priv *keys* are the secrets,
    # stored encrypted below -- protocol choice and security level are just
    # configuration, same sensitivity as snmp_version itself).
    #
    # values_callable is required here: SQLAlchemy's Enum(PythonEnum) sends
    # the member *name* (e.g. "AUTH_PRIV") to the DB by default, but
    # alembic/versions/0013_snmpv3_device_columns.py created the Postgres
    # enum types using the member *values* ("noAuthNoPriv", "authPriv",
    # "3DES", ...). Without values_callable that mismatch makes every save
    # with security_level=authPriv (or priv_protocol=3DES) fail with
    # `psycopg2.errors.InvalidTextRepresentation: invalid input value for
    # enum snmpsecuritylevel: "AUTH_PRIV"`. DeviceVendor/DeviceStatus don't
    # need this because their DB enum types were auto-created by
    # create_all() using the same name-based convention SQLAlchemy uses by
    # default, so both sides already agree there.
    snmp_security_level = Column(
        Enum(SnmpSecurityLevel, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        nullable=True,
    )
    snmp_auth_protocol = Column(
        Enum(SnmpAuthProtocol, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        nullable=True,
    )
    snmp_priv_protocol = Column(
        Enum(SnmpPrivProtocol, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        nullable=True,
    )
    # Fernet-encrypted (app.core.crypto) secrets stored directly on the
    # device row -- set via credential_service.set_snmp_credentials, called
    # from POST /devices/{id}/snmp-credentials. Never returned by any GET
    # endpoint (excluded from DeviceRead). Takes priority over the
    # snmp_*_ref env-var-based fallbacks above when present.
    snmp_community_encrypted = Column(Text, nullable=True)  # v1/v2c community string
    snmp_auth_key_encrypted = Column(Text, nullable=True)  # v3 auth passphrase
    snmp_priv_key_encrypted = Column(Text, nullable=True)  # v3 priv passphrase

    # --- Discovered capabilities, e.g. NETCONF <hello> capability list, stored as JSON text ---
    capabilities = Column(Text, nullable=True)

    # --- Lab / simulation backing (GNS3 integration) ---
    # A device backed by a GNS3 node is a real virtual router/switch instance
    # (IOSv, vIOS-L2, Arista vEOS, Juniper vMX, ...) running inside GNS3, not a
    # mock. Once `bootstrapped` is true it has a real management IP + SSH
    # reachable from this app, so every other service (deployment_engine,
    # protocol_manager, health_monitor, rollback_service) treats it exactly
    # like a physical device via the normal ip_address/ssh_username columns
    # above -- no separate code path needed for day-to-day deploy/validate/
    # rollback. These columns exist only for: (a) telling lab devices apart
    # in inventory/UI, (b) remembering which GNS3 project/node backs a
    # device so it can be started/stopped/torn down, and (c) reaching the
    # node's console over telnet for the one-time bootstrap before it has an
    # SSH-reachable management IP of its own.
    is_simulated = Column(Boolean, nullable=False, default=False, server_default="false")
    lab_provider = Column(String, nullable=True)  # e.g. "gns3"
    gns3_project_id = Column(String, nullable=True)
    gns3_node_id = Column(String, nullable=True)
    console_host = Column(String, nullable=True)  # GNS3 server address for console access
    console_port = Column(Integer, nullable=True)  # per-node telnet console port assigned by GNS3
    console_type = Column(String, nullable=True, default="telnet")  # telnet | vnc | none
    bootstrapped = Column(Boolean, nullable=False, default=False, server_default="false")

    # --- Deployment pipeline circuit breaker ---
    # Set by app.services.pipeline_service._check_circuit_breaker when this
    # device fails deployment (FAILED or ROLLED_BACK) settings.
    # DEPLOYMENT_CIRCUIT_BREAKER_FAILURE_THRESHOLD times in a row across
    # distinct ChangeRequests. While true, run_deployment_for_device
    # refuses to attempt further automated deploys against this device --
    # protects against a flapping device silently eating retries/rollbacks
    # forever. Cleared only by a Network Administrator via POST
    # /devices/{id}/clear-unstable-flag (manual review), never
    # automatically by a later success.
    flagged_unstable = Column(Boolean, nullable=False, default=False, server_default="false")
    unstable_since = Column(DateTime(timezone=True), nullable=True)
