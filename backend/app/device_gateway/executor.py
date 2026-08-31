"""Executes a validated DeviceJobRequest against the real device.

Only ever called after validator.validate() has succeeded -- this module
assumes the job is already authorized and just does the work. It reuses
app.services.protocol_manager (the existing, working NETCONF/RESTCONF/SSH
dispatch + audit-record logic) rather than reimplementing device protocol
handling -- the thing that changed is WHERE this code runs (a network-
isolated Gateway process instead of inside the API), not the protocol
logic itself.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.device import Device
from app.schemas.device_job import DeviceJobRequest, DeviceJobResult, DeviceOperation
from app.services import protocol_manager

logger = logging.getLogger("netguard.device_gateway.executor")


def execute(job: DeviceJobRequest, device: Device, db: Session) -> DeviceJobResult:
    from datetime import datetime, timezone

    if job.operation == DeviceOperation.SNMP_POLL:
        # SNMP polling doesn't go through ProtocolManager (SSH/NETCONF/
        # RESTCONF) at all -- it's app.services.metrics_service.poll_device,
        # a separate SNMP-specific code path that returns a plain dict and
        # writes directly to VictoriaMetrics + Device.last_snmp_poll_*,
        # rather than a ProtocolResult. Handled here as a special case
        # rather than forcing it through the pm.<method>() dispatch below.
        import json

        from app.services import credential_service, metrics_service

        try:
            sample = metrics_service.poll_device(db, device)
            return DeviceJobResult(
                job_id=job.job_id,
                success=True,
                output=json.dumps(sample, default=str),
                error=None,
                executed_at=datetime.now(timezone.utc).isoformat(),
            )
        except (metrics_service.SnmpNotConfiguredError, credential_service.CredentialNotFoundError) as exc:
            return DeviceJobResult(
                job_id=job.job_id,
                success=False,
                error=str(exc),
                executed_at=datetime.now(timezone.utc).isoformat(),
            )

    pm = protocol_manager.ProtocolManager(db, device, operator=f"device-gateway:{job.requested_by}")

    try:
        if job.operation == DeviceOperation.GET_RUNNING_CONFIG:
            result = pm.get_running_config()
        elif job.operation == DeviceOperation.GET_STARTUP_CONFIG:
            result = pm.backup_config() if hasattr(pm, "backup_config") else pm.get_running_config()
        elif job.operation == DeviceOperation.DEPLOY_CONFIG:
            config_text = job.params.get("config_text", "")
            result = pm.deploy_config(config_text) if hasattr(pm, "deploy_config") else None
        elif job.operation == DeviceOperation.ROLLBACK_CONFIG:
            # NOTE: this used to call pm.rollback(snapshot_id), a method
            # that doesn't exist on ProtocolManager -- ProtocolManager
            # only has restore_config(config_text). That meant every
            # ROLLBACK_CONFIG job silently no-opped into the "operation
            # not available on protocol_manager in this build" branch
            # below, regardless of what the caller sent. Callers now pass
            # the already-resolved restore text (not a snapshot_id) --
            # see app.services.pipeline_service's self-healing rollback
            # call site, the only one migrated to this job so far.
            config_text = job.params.get("config_text", "")
            result = pm.restore_config(config_text) if hasattr(pm, "restore_config") else None
        elif job.operation == DeviceOperation.REBOOT:
            result = pm.reboot() if hasattr(pm, "reboot") else None
        elif job.operation == DeviceOperation.GET_FACTS:
            result = pm.get_facts()
        elif job.operation == DeviceOperation.GET_BGP_NEIGHBORS:
            result = pm.get_bgp_neighbors()
        elif job.operation == DeviceOperation.GET_OSPF_NEIGHBORS:
            result = pm.get_ospf_neighbors()
        elif job.operation == DeviceOperation.GET_VPN_STATUS:
            result = pm.get_vpn_status()
        else:
            return DeviceJobResult(
                job_id=job.job_id,
                success=False,
                error=f"operation {job.operation} has no executor mapping",
                executed_at=datetime.now(timezone.utc).isoformat(),
            )

        if result is None:
            return DeviceJobResult(
                job_id=job.job_id,
                success=False,
                error=f"operation {job.operation} not available on protocol_manager in this build",
                executed_at=datetime.now(timezone.utc).isoformat(),
            )

        return DeviceJobResult(
            job_id=job.job_id,
            success=result.success,
            output=result.output or "",
            error=result.error,
            executed_at=datetime.now(timezone.utc).isoformat(),
            correlation_id=result.correlation_id,
            protocol=getattr(result.protocol, "value", result.protocol) if hasattr(result, "protocol") else None,
            execution_time_ms=getattr(result, "execution_time_ms", None),
        )
    except Exception as exc:  # noqa: BLE001 - a device-execution failure must produce a
        # result message, not crash the Gateway's consume loop
        logger.exception("device_gateway: execution failed for job %s", job.job_id)
        return DeviceJobResult(
            job_id=job.job_id,
            success=False,
            error=f"execution error: {exc}",
            executed_at=datetime.now(timezone.utc).isoformat(),
        )
