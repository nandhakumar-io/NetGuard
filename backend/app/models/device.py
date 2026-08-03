import enum
import uuid

from sqlalchemy import Boolean, Column, String, Enum, DateTime, Integer, Text, func
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
    status = Column(Enum(DeviceStatus), nullable=False, default=DeviceStatus.UNKNOWN)
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