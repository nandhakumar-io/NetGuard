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
import time
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.alert import Alert, AlertSeverity, AlertSource
from app.models.device import Device
from app.models.protocol_operation import ProtocolName, ProtocolOperation
from app.services import audit_service, credential_service, deployment_engine, netconf_service, restconf_service


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
            alert = Alert(
                device_id=self.device.id,
                severity=AlertSeverity.WARNING,
                source=AlertSource.PROTOCOL_FAILURE,
                category=f"Protocol {operation.replace('_', ' ').title()} Failed",
                message=f"{protocol.upper()} {operation} failed on {self.device.hostname}: {error or 'unknown error'}",
            )
            self.db.add(alert)
            self.db.commit()

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

    def get_running_config(self) -> ProtocolResult:
        protocol = select_protocol(self.device)
        username, password = self._credentials()

        if protocol == ProtocolName.NETCONF:
            result = netconf_service.get_config(
                self.device.ip_address, self.device.netconf_port, username, password, source="running"
            )
            return self._record(
                protocol="netconf", operation="get_running_config", success=result.success,
                request=result.request_xml, response=result.response_xml, http_status=None,
                error=result.error, execution_time_ms=result.execution_time_ms,
            )

        if protocol == ProtocolName.RESTCONF:
            result = restconf_service.get(self.device.restconf_url, "data", username, password)
            return self._record(
                protocol="restconf", operation="get_running_config", success=result.success,
                request=None, response=result.response_body, http_status=result.http_status,
                error=result.error, execution_time_ms=result.execution_time_ms,
            )

        # SSH fallback via NAPALM (deployment_engine.read_running_config)
        start = time.perf_counter()
        device_type = _netmiko_device_type(self.device)
        config = deployment_engine.read_running_config(device_type, self.device.ip_address, username, password)
        elapsed = (time.perf_counter() - start) * 1000
        return self._record(
            protocol="ssh", operation="get_running_config", success=config is not None,
            request=None, response=config, http_status=None,
            error=None if config is not None else "SSH/NAPALM read failed or unsupported platform",
            execution_time_ms=elapsed,
        )

    def deploy_config(self, config_text: str) -> ProtocolResult:
        protocol = select_protocol(self.device)
        username, password = self._credentials()

        if protocol == ProtocolName.NETCONF:
            result = netconf_service.push_config(self.device.ip_address, self.device.netconf_port, username, password, config_text)
            return self._record(
                protocol="netconf", operation="deploy_config", success=result.success,
                request=result.request_xml, response=result.response_xml, http_status=None,
                error=result.error, execution_time_ms=result.execution_time_ms,
            )

        if protocol == ProtocolName.RESTCONF:
            result = restconf_service.patch(self.device.restconf_url, "data", username, password, {"raw": config_text})
            return self._record(
                protocol="restconf", operation="deploy_config", success=result.success,
                request=result.request_body, response=result.response_body, http_status=result.http_status,
                error=result.error, execution_time_ms=result.execution_time_ms,
            )

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
        """
        result = self.get_running_config()
        result.operation = "backup_config"
        return result

    def restore_config(self, config_text: str) -> ProtocolResult:
        result = self.deploy_config(config_text)
        result.operation = "restore_config"
        return result

    def get_interfaces(self) -> ProtocolResult:
        protocol = select_protocol(self.device)
        username, password = self._credentials()

        if protocol == ProtocolName.NETCONF:
            result = netconf_service.get_config(self.device.ip_address, self.device.netconf_port, username, password, source="running")
            return self._record(
                protocol="netconf", operation="get_interfaces", success=result.success,
                request=result.request_xml, response=result.response_xml, http_status=None,
                error=result.error, execution_time_ms=result.execution_time_ms,
            )

        if protocol == ProtocolName.RESTCONF:
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
        username, password = self._credentials()

        if protocol == ProtocolName.NETCONF:
            caps = netconf_service.discover_capabilities(self.device.ip_address, self.device.netconf_port, username, password)
            return self._record(
                protocol="netconf", operation="get_facts", success=caps is not None,
                request="<hello/>", response=str(caps) if caps else None, http_status=None,
                error=None if caps is not None else "NETCONF hello/capability exchange failed",
                execution_time_ms=0.0,
            )

        if protocol == ProtocolName.RESTCONF:
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
        username, password = self._credentials()

        if protocol == ProtocolName.NETCONF:
            caps = netconf_service.discover_capabilities(self.device.ip_address, self.device.netconf_port, username, password)
            return self._record(
                protocol="netconf", operation="health_check", success=caps is not None,
                request=None, response=None, http_status=None,
                error=None if caps is not None else "NETCONF unreachable", execution_time_ms=0.0,
            )

        if protocol == ProtocolName.RESTCONF:
            result = restconf_service.get(self.device.restconf_url, "data", username, password)
            return self._record(
                protocol="restconf", operation="health_check", success=result.success,
                request=None, response=None, http_status=result.http_status,
                error=result.error, execution_time_ms=result.execution_time_ms,
            )

        start = time.perf_counter()
        device_type = _netmiko_device_type(self.device)
        config = deployment_engine.read_running_config(device_type, self.device.ip_address, username, password)
        elapsed = (time.perf_counter() - start) * 1000
        return self._record(
            protocol="ssh", operation="health_check", success=config is not None,
            request=None, response=None, http_status=None,
            error=None if config is not None else "SSH unreachable", execution_time_ms=elapsed,
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