"""Protocol Manager.

Single entry point the rest of the app (config management APIs, drift
detection, the deployment pipeline) should go through instead of calling
netconf_service / restconf_service / deployment_engine directly. It:

  1. Picks a protocol for a given device (NETCONF > RESTCONF > SSH/Netmiko,
     based on which the device is configured/capable for).
  2. Runs the requested operation against the already-COMPLETE protocol
     services (app.services.netconf_service, restconf_service,
     deployment_engine's Netmiko wrapper) -- this module does not talk to
     devices itself, it only dispatches to those.
  3. Records a ProtocolOperation row, an AuditLog entry (with a
     correlation ID tying the two together), and -- on failure -- an
     Alert, for every single execution. Callers never need to do this
     bookkeeping themselves.

Credentials: Device only has one credential pair today
(ssh_username / ssh_credential_ref via credential_service.get_ssh_password)
-- there's no separate netconf/restconf credential ref on the model, so
NETCONF and RESTCONF operations authenticate with that same credential.
Extending Device with protocol-specific credential refs is a schema change
outside this integration's scope (Device is one of the "already
implemented, do not regenerate" models).
"""
import logging
import time
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.alert import AlertSeverity, AlertSource
from app.models.device import Device
from app.models.protocol_operation import ProtocolName, ProtocolOperation
from app.services import alert_service, audit_service, credential_service, deployment_engine, netconf_service, restconf_service
from app.services.config_format_service import looks_like_json, looks_like_xml
from app.services.credential_service import CredentialNotFoundError

logger = logging.getLogger("netguard.protocol_manager")


class ProtocolUnavailableError(Exception):
    """Raised when a device has no usable protocol configured at all."""


@dataclass
class ProtocolResult:
    success: bool
    protocol: ProtocolName
    operation: str
    output: str
    error: str | None
    execution_time_ms: float
    correlation_id: str
    protocol_operation_id: uuid.UUID | None = None
    # Populated only by backup_config() for NETCONF-capable devices --
    # NETCONF's <get-config><source><startup/></source> gives a real
    # startup-config read, unlike RESTCONF/SSH which have no equivalent
    # standardized primitive. See backup_config() below and
    # app.api.config_management.backup_config, which persists this into
    # ConfigSnapshot.startup_config_encrypted when present.
    startup_config: str | None = None


def select_protocol(device: Device) -> str:
    """NETCONF first, then RESTCONF, then plain SSH -- SSH is the
    universal fallback since deployment_engine/Netmiko only needs
    ssh_username + a credential ref, which every device already has.

    Returns a plain string ("netconf" / "restconf" / "ssh"), deliberately
    kept separate from ProtocolOperation's ProtocolName enum (which only
    has NETCONF/RESTCONF/SNMP -- no SSH member; see the note in _record()
    on why we don't widen that enum). Using strings here means the
    `if protocol == "netconf"` dispatch in every method below can't
    accidentally alias "no NETCONF support" onto ProtocolName.NETCONF.
    """
    if device.supports_netconf and device.netconf_port:
        return "netconf"
    if device.supports_restconf and device.restconf_url:
        return "restconf"
    return "ssh"


class ProtocolManager:
    def __init__(self, db: Session, device: Device, operator: str = "system"):
        self.db = db
        self.device = device
        self.operator = operator

    # -- internal helpers ---------------------------------------------

    def _safe_credentials(self, *, protocol: str, operation: str) -> tuple[str, str] | ProtocolResult:
        """Same as _credentials(), but a CredentialNotFoundError (device has
        no ssh_credential_ref, or that ref has nothing in the secret store)
        is turned into a normal failed ProtocolResult -- recorded and
        alerted like any other protocol failure -- instead of propagating
        out of the endpoint as an unhandled 500. Callers should check
        `isinstance(result, ProtocolResult)` and return it immediately if so.
        """
        try:
            return self._credentials()
        except CredentialNotFoundError as exc:
            return self._record(
                protocol=protocol, operation=operation, success=False,
                request=None, response=None, http_status=None,
                error=str(exc), execution_time_ms=0.0,
            )

    def _credentials(self) -> tuple[str, str]:
        username = self.device.ssh_username or "admin"
        password = credential_service.get_ssh_password(self.device)
        return username, password

    def _record(
        self,
        *,
        protocol: str,
        operation: str,
        success: bool,
        request: str | None,
        response: str | None,
        http_status: int | None,
        error: str | None,
        execution_time_ms: float,
    ) -> ProtocolResult:
        correlation_id = str(uuid.uuid4())

        po_protocol = protocol if protocol in (ProtocolName.NETCONF.value, ProtocolName.RESTCONF.value, ProtocolName.SNMP.value) else ProtocolName.NETCONF
        # SSH/Netmiko operations are stored with protocol=NETCONF's sibling
        # is misleading, so instead of mislabeling them we tag the real
        # protocol into `operation` (e.g. "ssh:deploy_config") and keep
        # ProtocolOperation.protocol pointed at the closest schema-valid
        # value only when the real protocol genuinely is netconf/restconf/
        # snmp. This keeps the existing enum untouched per "do not
        # regenerate ProtocolOperation".
        recorded_operation = operation if protocol in (ProtocolName.NETCONF.value, ProtocolName.RESTCONF.value, ProtocolName.SNMP.value) else f"{protocol}:{operation}"

        protocol_op = ProtocolOperation(
            device_id=self.device.id,
            protocol=ProtocolName(po_protocol),
            operation=recorded_operation,
            operator=self.operator,
            request_payload=request,
            response_payload=response,
            http_status=http_status,
            success=success,
            error_message=error,
            execution_time_ms=execution_time_ms,
        )
        self.db.add(protocol_op)
        self.db.commit()
        self.db.refresh(protocol_op)

        audit_service.record_event(
            self.db,
            actor=self.operator,
            action=f"Protocol {operation}",
            result="Success" if success else "Failed",
            device_hostname=self.device.hostname,
            detail=(
                f"protocol={protocol} correlation_id={correlation_id} "
                f"duration_ms={execution_time_ms:.1f}" + (f" error={error}" if error else "")
            ),
        )

        if not success:
            # Dedup-aware: a device stuck failing the same operation on
            # every retry/poll updates one standing alert instead of
            # piling up a new row per attempt (see alert_service.raise_alert).
            alert_service.raise_alert(
                self.db,
                device_id=self.device.id,
                severity=AlertSeverity.WARNING,
                source=AlertSource.PROTOCOL_FAILURE,
                category=f"Protocol {operation.replace('_', ' ').title()} Failed",
                message=f"{protocol.upper()} {operation} failed on {self.device.hostname}: {error or 'unknown error'}",
            )

        return ProtocolResult(
            success=success,
            protocol=ProtocolName(po_protocol),
            operation=operation,
            output=response or "",
            error=error,
            execution_time_ms=execution_time_ms,
            correlation_id=correlation_id,
            protocol_operation_id=protocol_op.id,
        )

    # -- public API ------------------------------------------------------

    def _get_running_config_ssh(self, username: str, password: str) -> ProtocolResult:
        """SSH first, Netmiko-over-Telnet fallback (deployment_engine.read_running_config)."""
        start = time.perf_counter()
        device_type = _netmiko_device_type(self.device)
        config, used_protocol = deployment_engine.read_running_config(device_type, self.device.ip_address, username, password)
        elapsed = (time.perf_counter() - start) * 1000
        if used_protocol == "telnet":
            error = None  # success, but flag it as an unencrypted-fallback read, not a failure
        else:
            error = None if config is not None else "SSH/NAPALM read failed or unsupported platform (Telnet fallback also unavailable)"
        return self._record(
            protocol=used_protocol if config is not None else "ssh",
            operation="get_running_config", success=config is not None,
            request=None, response=config, http_status=None,
            error=error, execution_time_ms=elapsed,
        )

    def get_running_config(self) -> ProtocolResult:
        protocol = select_protocol(self.device)
        creds = self._safe_credentials(protocol=protocol, operation="get_running_config")
        if isinstance(creds, ProtocolResult):
            return creds
        username, password = creds

        if protocol == "netconf":
            result = netconf_service.get_config(
                self.device.ip_address, self.device.netconf_port, username, password, source="running"
            )
            if not result.success and self.device.ssh_username:
                # NETCONF is marked supported on the device record but the
                # live session failed (wrong port, feature not actually
                # enabled, auth rejected over NETCONF specifically, etc.).
                # Previously this was a hard failure -- "Backup" would
                # error out even though the same device is perfectly
                # reachable over SSH. Fall back to SSH/NAPALM instead of
                # giving up, same tolerant "best available protocol"
                # philosophy this class already uses for startup-config
                # and everywhere else in the app.
                logger.debug(
                    "NETCONF get_running_config failed for %s (%s); falling back to SSH",
                    self.device.hostname, result.error,
                )
                fallback = self._get_running_config_ssh(username, password)
                if fallback.success:
                    return fallback
            return self._record(
                protocol="netconf", operation="get_running_config", success=result.success,
                request=result.request_xml, response=result.response_xml, http_status=None,
                error=result.error, execution_time_ms=result.execution_time_ms,
            )

        if protocol == "restconf":
            result = restconf_service.get(self.device.restconf_url, "data", username, password)
            if not result.success and self.device.ssh_username:
                logger.debug(
                    "RESTCONF get_running_config failed for %s (%s); falling back to SSH",
                    self.device.hostname, result.error,
                )
                fallback = self._get_running_config_ssh(username, password)
                if fallback.success:
                    return fallback
            return self._record(
                protocol="restconf", operation="get_running_config", success=result.success,
                request=None, response=result.response_body, http_status=result.http_status,
                error=result.error, execution_time_ms=result.execution_time_ms,
            )

        return self._get_running_config_ssh(username, password)

    def deploy_config(self, config_text: str) -> ProtocolResult:
        protocol = select_protocol(self.device)
        creds = self._safe_credentials(protocol=protocol, operation="deploy_config")
        if isinstance(creds, ProtocolResult):
            return creds
        username, password = creds

        if protocol == "netconf":
            if looks_like_xml(config_text):
                vendor = self.device.vendor.value if hasattr(self.device.vendor, "value") else str(self.device.vendor)
                result = netconf_service.push_config(
                    self.device.ip_address, self.device.netconf_port, username, password, config_text, vendor=vendor,
                )
                return self._record(
                    protocol="netconf", operation="deploy_config", success=result.success,
                    request=result.request_xml, response=result.response_xml, http_status=None,
                    error=result.error, execution_time_ms=result.execution_time_ms,
                )
            else:
                logger.debug("Config specifies NETCONF, but the payload is raw CLI (not XML). Falling back to SSH.")

        elif protocol == "restconf":
            if looks_like_json(config_text):
                result = restconf_service.patch(self.device.restconf_url, "data", username, password, {"raw": config_text})
                return self._record(
                    protocol="restconf", operation="deploy_config", success=result.success,
                    request=result.request_body, response=result.response_body, http_status=result.http_status,
                    error=result.error, execution_time_ms=result.execution_time_ms,
                )
            else:
                logger.debug("Config specifies RESTCONF, but the payload is raw CLI (not JSON). Falling back to SSH.")

        device_type = _netmiko_device_type(self.device)
        commands = [line for line in config_text.splitlines() if line.strip()]
        result = deployment_engine.deploy_config(
            self.device.hostname, self.device.ip_address, device_type, username, password, commands
        )
        return self._record(
            protocol="ssh", operation="deploy_config", success=result.success,
            request="\n".join(commands), response=result.output, http_status=None,
            error=result.error, execution_time_ms=0.0,
        )

    def backup_config(self) -> ProtocolResult:
        """Reads the running config (via get_running_config) and returns it
        as a ProtocolResult; the caller (config management API) is
        responsible for turning that into a ConfigSnapshot via
        snapshot_service, same as the deployment pipeline already does --
        ProtocolManager doesn't duplicate that persistence logic.

        Also attempts a genuine startup-config read and attaches it as
        `result.startup_config`, so backups actually capture both
        datastores instead of running-config only:
          - NETCONF-capable devices: <get-config><source><startup/>.
          - SSH-managed devices: NAPALM's get_config()["startup"] (the
            same call get_running_config's SSH path already makes for
            "running", just reading the other key).
        RESTCONF has no equivalent standardized "read startup config"
        primitive, so it stays running-config only. Either read failing
        (unsupported platform, device rejects it, no startup datastore)
        is swallowed here -- it's a best-effort addition to the backup,
        never a reason to fail the backup itself.
        """
        result = self.get_running_config()
        result.operation = "backup_config"

        if not result.success:
            return result

        protocol = select_protocol(self.device)
        creds = self._safe_credentials(protocol=protocol, operation="backup_config")
        if isinstance(creds, ProtocolResult):
            return result
        username, password = creds

        if protocol == "netconf":
            startup_result = netconf_service.get_config(
                self.device.ip_address, self.device.netconf_port, username, password,
                source="startup", vendor=self.device.vendor.value if hasattr(self.device.vendor, "value") else str(self.device.vendor),
            )
            if startup_result.success and startup_result.response_xml:
                result.startup_config = startup_result.response_xml
        elif protocol != "restconf":
            device_type = _netmiko_device_type(self.device)
            result.startup_config = deployment_engine.read_startup_config(
                device_type, self.device.ip_address, username, password
            )

        return result

    def restore_config(self, config_text: str) -> ProtocolResult:
        result = self.deploy_config(config_text)
        result.operation = "restore_config"
        return result

    def get_interfaces(self) -> ProtocolResult:
        protocol = select_protocol(self.device)
        creds = self._safe_credentials(protocol=protocol, operation="get_interfaces")
        if isinstance(creds, ProtocolResult):
            return creds
        username, password = creds

        if protocol == "netconf":
            result = netconf_service.get_config(self.device.ip_address, self.device.netconf_port, username, password, source="running")
            return self._record(
                protocol="netconf", operation="get_interfaces", success=result.success,
                request=result.request_xml, response=result.response_xml, http_status=None,
                error=result.error, execution_time_ms=result.execution_time_ms,
            )

        if protocol == "restconf":
            result = restconf_service.get(self.device.restconf_url, "data/ietf-interfaces:interfaces", username, password)
            return self._record(
                protocol="restconf", operation="get_interfaces", success=result.success,
                request=None, response=result.response_body, http_status=result.http_status,
                error=result.error, execution_time_ms=result.execution_time_ms,
            )

        start = time.perf_counter()
        facts = _napalm_getter(self.device, username, password, "get_interfaces")
        elapsed = (time.perf_counter() - start) * 1000
        return self._record(
            protocol="ssh", operation="get_interfaces", success=facts is not None,
            request=None, response=str(facts) if facts is not None else None, http_status=None,
            error=None if facts is not None else "SSH/NAPALM get_interfaces failed or unsupported platform",
            execution_time_ms=elapsed,
        )

    def get_facts(self) -> ProtocolResult:
        protocol = select_protocol(self.device)
        creds = self._safe_credentials(protocol=protocol, operation="get_facts")
        if isinstance(creds, ProtocolResult):
            return creds
        username, password = creds

        if protocol == "netconf":
            caps = netconf_service.discover_capabilities(self.device.ip_address, self.device.netconf_port, username, password)
            return self._record(
                protocol="netconf", operation="get_facts", success=caps is not None,
                request="<hello/>", response=str(caps) if caps else None, http_status=None,
                error=None if caps is not None else "NETCONF hello/capability exchange failed",
                execution_time_ms=0.0,
            )

        if protocol == "restconf":
            result = restconf_service.get(self.device.restconf_url, "data/ietf-yang-library:yang-library", username, password)
            return self._record(
                protocol="restconf", operation="get_facts", success=result.success,
                request=None, response=result.response_body, http_status=result.http_status,
                error=result.error, execution_time_ms=result.execution_time_ms,
            )

        start = time.perf_counter()
        facts = _napalm_getter(self.device, username, password, "get_facts")
        elapsed = (time.perf_counter() - start) * 1000
        return self._record(
            protocol="ssh", operation="get_facts", success=facts is not None,
            request=None, response=str(facts) if facts is not None else None, http_status=None,
            error=None if facts is not None else "SSH/NAPALM get_facts failed or unsupported platform",
            execution_time_ms=elapsed,
        )

    def health_check(self) -> ProtocolResult:
        """Cheap reachability probe used before deployment/drift runs and
        by GET /devices/{id}/health. Tries the selected protocol's
        lightest-weight read; falls through the same NETCONF > RESTCONF >
        SSH priority as everything else.
        """
        protocol = select_protocol(self.device)
        creds = self._safe_credentials(protocol=protocol, operation="health_check")
        if isinstance(creds, ProtocolResult):
            return creds
        username, password = creds

        if protocol == "netconf":
            caps = netconf_service.discover_capabilities(self.device.ip_address, self.device.netconf_port, username, password)
            return self._record(
                protocol="netconf", operation="health_check", success=caps is not None,
                request=None, response=None, http_status=None,
                error=None if caps is not None else "NETCONF unreachable", execution_time_ms=0.0,
            )

        if protocol == "restconf":
            result = restconf_service.get(self.device.restconf_url, "data", username, password)
            return self._record(
                protocol="restconf", operation="health_check", success=result.success,
                request=None, response=None, http_status=result.http_status,
                error=result.error, execution_time_ms=result.execution_time_ms,
            )

        start = time.perf_counter()
        device_type = _netmiko_device_type(self.device)
        config, used_protocol = deployment_engine.read_running_config(device_type, self.device.ip_address, username, password)
        elapsed = (time.perf_counter() - start) * 1000
        return self._record(
            protocol=used_protocol if config is not None else "ssh",
            operation="health_check", success=config is not None,
            request=None, response=None, http_status=None,
            error=None if config is not None else "Unreachable via SSH or Telnet", execution_time_ms=elapsed,
        )


_DEVICE_TYPE_MAP = {
    "cisco": "cisco_ios",
    "juniper": "juniper_junos",
    "arista": "arista_eos",
    "linux": "linux",
}


def _netmiko_device_type(device: Device) -> str:
    vendor = device.vendor.value if hasattr(device.vendor, "value") else str(device.vendor)
    return _DEVICE_TYPE_MAP.get(vendor, "cisco_ios")


def _napalm_getter(device: Device, username: str, password: str, getter_name: str) -> dict | None:
    """Same lazy-import-and-swallow-errors pattern as
    deployment_engine.read_running_config: a platform with no NAPALM
    driver (or an unreachable device) returns None, not an exception.
    """
    driver_name = deployment_engine.NAPALM_DRIVER_MAP.get(_netmiko_device_type(device))
    if driver_name is None:
        return None
    try:
        import napalm

        driver = napalm.get_network_driver(driver_name)
        conn = driver(hostname=device.ip_address, username=username, password=password, timeout=10)
        conn.open()
        try:
            return getattr(conn, getter_name)()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - best-effort, caller records the failure
        return None