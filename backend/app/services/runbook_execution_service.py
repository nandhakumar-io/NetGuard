"""Turns an AlertRunbook's optional remediation step from reference text
into an actual job run against a device.

Gating is deliberately layered, not just "the endpoint checked a role":

  1. The API endpoint (app.api.alert_runbooks.execute_runbook_remediation)
     requires require_roles(NETWORK_ADMIN) -- which already folds in
     User.extra_roles, extra_permissions, and an active JIT elevation to
     network_admin (see app.core.deps.require_roles). A non-admin with no
     JIT grant never reaches this module at all.
  2. A runbook can additionally set remediation_required_role to demand
     a *specific* role even stricter than "any admin" -- checked here via
     jit_service.active_roles_for_user so a JIT grant satisfies it the
     same way a standing role would.
  3. The actual device write goes through the existing
     ProtocolManager.deploy_config path, so it gets the same
     ProtocolOperation audit row, the same protocol-failure alerting, and
     the same credential handling as every other config push in the app
     -- this isn't a second, weaker way to reach a device.

Every trigger -- successful or not -- is logged to RunbookExecution
before the config push happens (status=PENDING) and updated afterward,
so a crash mid-push still leaves a visible "someone tried this" record
rather than silently vanishing.
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.alert_runbook import (
    AlertRunbook,
    RemediationActionType,
    RunbookExecution,
    RunbookExecutionStatus,
)
from app.models.device import Device
from app.models.user import User
from app.services import audit_service, config_intent_service, jit_service

logger = logging.getLogger(__name__)

# Prefix marking a remediation_command as a vendor-agnostic config intent
# (see app.services.config_intent_service) rather than literal CLI text.
# A runbook authored this way renders to the right syntax for whatever
# vendor the target device actually is at execution time, instead of the
# fixed string every other runbook's remediation_command is -- e.g.
# "clear arp-cache" is valid Cisco IOS but would fail outright pushed
# verbatim to a Juniper or Linux device. Used by alert_runbook_seed's
# default "clear ARP cache" / "clear MAC table" runbooks; an admin
# hand-authoring a runbook still just types literal CLI text as before,
# since most runbooks are legitimately single-vendor (a specific
# interface bounce, a specific service restart) and don't benefit from
# this at all.
INTENT_COMMAND_PREFIX = "__config_intent__:"


class RemediationNotConfiguredError(Exception):
    """Raised when a runbook has no remediation action to run."""


class RemediationForbiddenError(Exception):
    """Raised when the runbook demands a role the caller doesn't hold,
    even after accounting for JIT elevation."""


def _user_satisfies_required_role(db: Session, user: User, required_role) -> bool:
    if required_role is None:
        return True
    if user.role == required_role:
        return True
    if required_role.value in jit_service.active_roles_for_user(db, user.id):
        return True
    return False


def _resolve_command(runbook: AlertRunbook, device: Device) -> str:
    """Returns the literal command text to push for this runbook against
    this device -- either runbook.remediation_command verbatim (the
    common case), or, for an INTENT_COMMAND_PREFIX-marked runbook, that
    intent rendered for the device's actual vendor.

    Raises RemediationNotConfiguredError (same error a caller already
    handles for "no remediation configured at all") if the device's
    vendor has no mapping to a config_intent_service.Vendor, or if this
    particular intent has no renderer for that vendor -- e.g. a Linux
    device has no MAC-table-clear equivalent, so a runbook whose command
    is `__config_intent__:clear_mac_table` is simply not runnable
    against one, the same "doc-only" outcome as if remediation had never
    been configured for that device at all.
    """
    command = runbook.remediation_command or ""
    if not command.startswith(INTENT_COMMAND_PREFIX):
        return command

    kind_value = command[len(INTENT_COMMAND_PREFIX):]
    try:
        intent_kind = config_intent_service.IntentKind(kind_value)
    except ValueError:
        raise RemediationNotConfiguredError(
            f"Runbook '{runbook.title}' references an unknown config intent '{kind_value}'"
        )

    model_vendor = device.vendor.value if hasattr(device.vendor, "value") else str(device.vendor)
    vendor = config_intent_service.DEVICE_MODEL_VENDOR_DEFAULT.get(model_vendor)
    if vendor is None:
        raise RemediationNotConfiguredError(
            f"Runbook '{runbook.title}' has no command mapping for device vendor '{model_vendor}'"
        )

    try:
        return config_intent_service.render_intent(
            config_intent_service.ConfigIntent(kind=intent_kind, params={}), vendor
        )
    except config_intent_service.UnsupportedIntentError as exc:
        raise RemediationNotConfiguredError(str(exc))


def trigger_remediation(
    db: Session,
    *,
    runbook: AlertRunbook,
    device: Device,
    user: User,
    alert_id: uuid.UUID | None,
) -> RunbookExecution:
    """Runs `runbook`'s remediation command against `device` and returns
    the RunbookExecution row (already committed) recording the outcome.

    Raises RemediationNotConfiguredError / RemediationForbiddenError
    before anything is written to the device or logged, so a bad request
    never shows up in the execution history as a phantom attempt.
    """
    if not runbook.remediation_enabled or not runbook.remediation_command:
        raise RemediationNotConfiguredError(
            f"Runbook '{runbook.title}' has no remediation action configured"
        )
    if not _user_satisfies_required_role(db, user, runbook.remediation_required_role):
        raise RemediationForbiddenError(
            f"This remediation requires the '{runbook.remediation_required_role.value}' role"
        )

    # Resolved (and, for an intent-marked runbook, vendor-rendered) up
    # front and before any RunbookExecution row is written -- a runbook
    # with no valid command for this device's vendor should show up as
    # "never actually ran" in the execution history, not as a phantom
    # failed attempt with a device write nothing actually issued.
    command = _resolve_command(runbook, device)

    execution = RunbookExecution(
        runbook_id=runbook.id,
        alert_id=alert_id,
        device_id=device.id,
        triggered_by=user.email,
        status=RunbookExecutionStatus.PENDING,
    )
    db.add(execution)
    db.commit()
    db.refresh(execution)

    # Local import: avoids a service<->service import cycle at module
    # load time (protocol_manager itself pulls in alert_service, which
    # some callers of *this* module also import).
    from app.services.protocol_manager import ProtocolManager

    action_label = (
        "Restart service" if runbook.remediation_action_type == RemediationActionType.RESTART_SERVICE
        else "Push config"
    )

    try:
        manager = ProtocolManager(db, device, operator=user.email)
        result = manager.deploy_config(command)

        execution.status = RunbookExecutionStatus.SUCCESS if result.success else RunbookExecutionStatus.FAILED
        execution.output = result.output
        execution.error = result.error
        execution.completed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(execution)

        audit_service.record_event(
            db,
            actor=user.email,
            action=f"Runbook remediation: {runbook.remediation_label or action_label}",
            result="Success" if result.success else "Failed",
            device_hostname=device.hostname,
            detail=(
                f"runbook={runbook.title!r} runbook_id={runbook.id} "
                f"execution_id={execution.id}" + (f" error={result.error}" if result.error else "")
            ),
        )
        return execution
    except Exception as exc:
        logger.exception("Runbook remediation failed for runbook=%s device=%s", runbook.id, device.id)
        execution.status = RunbookExecutionStatus.FAILED
        execution.error = str(exc)
        execution.completed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(execution)

        audit_service.record_event(
            db,
            actor=user.email,
            action=f"Runbook remediation: {runbook.remediation_label or action_label}",
            result="Failed",
            device_hostname=device.hostname,
            detail=f"runbook={runbook.title!r} runbook_id={runbook.id} execution_id={execution.id} error={exc}",
        )
        return execution


def list_executions_for_runbook(db: Session, runbook_id: uuid.UUID, limit: int = 50) -> list[RunbookExecution]:
    return (
        db.query(RunbookExecution)
        .filter(RunbookExecution.runbook_id == runbook_id)
        .order_by(RunbookExecution.created_at.desc())
        .limit(limit)
        .all()
    )
