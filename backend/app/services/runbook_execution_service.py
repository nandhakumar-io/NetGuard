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
from app.services import audit_service, jit_service

logger = logging.getLogger(__name__)


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
        result = manager.deploy_config(runbook.remediation_command)

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
