"""Central change-validation orchestrator.

Calls, in order, the existing `validation_engine` (syntax/structural),
`opa_service` (policy-as-code), `batfish_service` (behavioral simulation),
`risk_engine`, and `impact_simulation_service`, then combines them into one
deterministic decision (Section 12 of the integration spec):

    syntax FAIL              -> BLOCK
    OPA DENY                 -> BLOCK
    Batfish CRITICAL          -> BLOCK
    high-risk behavior change -> REVIEW
    OPA REVIEW                -> REVIEW
    Batfish REVIEW             -> REVIEW
    Batfish UNAVAILABLE (crit/high risk change) -> REVIEW
    everything else            -> PASS

A lower-severity PASS from one engine never overrides a higher-severity
result from another -- this module is the only place that combines them,
so both `POST /change-requests/{id}/validate` and the pre-deploy gate in
`pipeline_service.py` call the same code path and can never disagree.

This module does not duplicate `validation_engine`, `risk_engine`, or
`impact_simulation_service` -- it calls them.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.blockchain_evidence import EvidenceType
from app.services import (
    audit_service,
    event_bus,
    evidence_service,
    fabric_service,
    impact_simulation_service,
    risk_engine,
    validation_engine,
)
from app.services.batfish_service import BatfishResult, BatfishStatus, batfish_service
from app.services.opa_service import OpaDecision, OpaResult, opa_service

logger = logging.getLogger(__name__)

# NATS subjects (Section 18). Emitted from this module only, so the API
# endpoint (POST/GET .../validate(ion)) and the pipeline_service pre-deploy
# gate -- the only two callers of validate_change() -- always emit
# identically instead of each maintaining its own copy of this wiring.
EVT_VALIDATION_REQUESTED = "netguard.change.validation.requested"
EVT_VALIDATION_COMPLETED = "netguard.change.validation.completed"
EVT_VALIDATION_FAILED = "netguard.change.validation.failed"
EVT_OPA_COMPLETED = "netguard.opa.evaluation.completed"
EVT_BATFISH_COMPLETED = "netguard.batfish.validation.completed"


class CombinedDecision:
    BLOCK = "block"
    REVIEW = "review"
    PASS = "pass"


@dataclass
class ChangeValidationResult:
    decision: str
    overall_score: int | None
    syntax_passed: bool
    syntax_errors: list[str]
    syntax_warnings: list[str]
    opa: OpaResult | None
    batfish: BatfishResult | None
    risk_score: int | None
    risk_classification: str | None
    blast_radius_devices: int | None
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "overall_score": self.overall_score,
            "syntax": {"passed": self.syntax_passed, "errors": self.syntax_errors, "warnings": self.syntax_warnings},
            "opa": self.opa.to_dict() if self.opa else None,
            "batfish": self.batfish.to_dict() if self.batfish else None,
            "risk": {"score": self.risk_score, "classification": self.risk_classification},
            "blast_radius": {"devices": self.blast_radius_devices},
            "reasons": self.reasons,
        }


def _anchor_validation_evidence(
    db: Session,
    *,
    device,
    change_request,
    proposed_config: str,
    result: "ChangeValidationResult",
    actor: str,
) -> None:
    """Generate + anchor the CHANGE_VALIDATION evidence record for one
    validate_change() run (Section 3/9). Deliberately isolated behind a
    try/except at the call site (never here) so a caller can decide
    whether an evidence failure should be visible -- in practice both
    call sites below swallow it, since Fabric anchoring is additive
    audit infrastructure and must never block or fail a validation
    decision it has no authority over (Section 1)."""
    fabric_service.anchor_evidence(
        db,
        evidence_type=EvidenceType.CHANGE_VALIDATION,
        change_request=change_request,
        device=device,
        actor_subject=actor,
        configuration_hash=evidence_service.hash_config(proposed_config),
        policy_version=getattr(result.opa, "policy_version", None) if result.opa else None,
        batfish_version=getattr(result.batfish, "batfish_version", None) if result.batfish else None,
        fields={
            "result": result.decision,
            "syntax": {"passed": result.syntax_passed, "errors": result.syntax_errors},
            "opa": result.opa.to_dict() if result.opa else None,
            "batfish": result.batfish.to_dict() if result.batfish else None,
            "risk": {"score": result.risk_score, "level": result.risk_classification},
            "blast_radius_devices": result.blast_radius_devices,
            "configuration_hash": evidence_service.hash_config(proposed_config),
            "reasons": result.reasons,
        },
    )


async def validate_change(
    db: Session,
    *,
    device,
    change_request,
    current_config: str | None,
    proposed_config: str,
    user_context: dict[str, Any] | None = None,
    uplink_interfaces: set[str] | None = None,
    mgmt_ip: str | None = None,
    run_batfish: bool = True,
    actor: str = "system",
) -> ChangeValidationResult:
    reasons: list[str] = []
    validation_id = str(uuid.uuid4())
    trace_id = validation_id  # single-hop today; kept as a distinct field so a
    # future caller (e.g. an API request already carrying its own trace id)
    # can pass one through without changing the event/audit shape.
    cr_id = getattr(change_request, "id", None)
    device_hostname = getattr(device, "hostname", None)

    def _emit(event_type: str, **payload) -> None:
        event_bus.publish_event(
            event_type,
            channel=event_type,
            change_request_id=str(cr_id) if cr_id else None,
            validation_id=validation_id,
            trace_id=trace_id,
            **payload,
        )

    def _audit(action: str, result: str, detail: str | None = None) -> None:
        audit_service.record_event(
            db, actor=actor, action=action, result=result,
            device_hostname=device_hostname, change_request_id=cr_id,
            detail=f"[validation_id={validation_id}] {detail}" if detail else f"[validation_id={validation_id}]",
        )

    _emit(EVT_VALIDATION_REQUESTED)
    _audit("CHANGE_VALIDATION_STARTED", "Started")

    try:
        # 1. Existing syntax/structural validation engine -- unchanged, hard gate.
        syntax_result = validation_engine.validate_syntax(
            proposed_config,
            vendor=getattr(device, "vendor", None) or "cisco",
            current_config=current_config,
            uplink_interfaces=uplink_interfaces,
            mgmt_ip=mgmt_ip,
        )

        if not syntax_result.passed:
            reasons.append("Syntax/structural validation failed")
            result = ChangeValidationResult(
                decision=CombinedDecision.BLOCK,
                overall_score=None,
                syntax_passed=False,
                syntax_errors=syntax_result.errors,
                syntax_warnings=syntax_result.warnings,
                opa=None,
                batfish=None,
                risk_score=None,
                risk_classification=None,
                blast_radius_devices=None,
                reasons=reasons,
            )
            _audit("CHANGE_VALIDATION_BLOCKED", "Blocked", "; ".join(syntax_result.errors))
            _emit(EVT_VALIDATION_COMPLETED, decision=result.decision)
            try:
                _anchor_validation_evidence(
                    db, device=device, change_request=change_request,
                    proposed_config=proposed_config, result=result, actor=actor,
                )
            except Exception:  # noqa: BLE001 -- evidence anchoring is additive, never blocks validation
                logger.exception("Fabric evidence anchoring failed for validation %s", validation_id)
            return result

        # 2. Existing risk engine + impact simulation (used both for the OPA
        #    input and for combined decision precedence).
        risk_result = risk_engine.analyze(proposed_config, current_config)
        impact_result = impact_simulation_service.simulate_impact(db, device, proposed_config, current_config)
        blast_radius_devices = len(impact_result.isolated_devices) + len(impact_result.degraded_devices)

        device_dict = {
            "id": getattr(device, "id", None),
            "hostname": getattr(device, "hostname", None) or getattr(device, "name", None),
            "vendor": getattr(device, "vendor", None),
            "platform": getattr(device, "platform", None) or getattr(device, "vendor", None),
            "role": getattr(device, "device_role", None) or getattr(device, "role", None) or "unknown",
        }
        change_dict = {
            "id": getattr(change_request, "id", None),
            "description": getattr(change_request, "description", ""),
            "priority": getattr(getattr(change_request, "priority", None), "value", "medium"),
            "business_justification": getattr(change_request, "business_justification", None),
        }

        # 3. OPA policy evaluation.
        opa_result = await opa_service.evaluate_change(
            device=device_dict,
            current_config=current_config,
            proposed_config=proposed_config,
            change_request=change_dict,
            user_context=user_context,
            risk={"score": risk_result.risk_score, "classification": risk_result.classification},
            blast_radius={"devices": blast_radius_devices},
        )
        _audit("OPA_EVALUATED", "Info", f"decision={opa_result.decision}")
        _emit(EVT_OPA_COMPLETED, decision=opa_result.decision, violations=len(opa_result.violations))
        if opa_result.decision == OpaDecision.DENY:
            reasons.append("OPA policy evaluation denied this change")
            _audit(
                "OPA_POLICY_VIOLATION", "Denied",
                "; ".join(v.policy for v in opa_result.violations) or "policy denied",
            )

        # 4. Batfish behavioral simulation (after OPA per the required pipeline
        #    order; skipped only if the caller/orchestrating task disabled it,
        #    e.g. for a dry-run preview).
        batfish_result: BatfishResult | None = None
        if run_batfish:
            _audit("BATFISH_STARTED", "Started")
            batfish_result = await batfish_service.validate_configuration(
                change_request_id=str(getattr(change_request, "id", "")),
                device=device_dict,
                current_config=current_config,
                proposed_config=proposed_config,
            )
            _audit("BATFISH_COMPLETED", "Info", f"status={batfish_result.status.value}")
            _emit(
                EVT_BATFISH_COMPLETED,
                status=batfish_result.status.value,
                behavior_changes=batfish_result.behavior_changes,
            )
            if batfish_result.status == BatfishStatus.CRITICAL:
                reasons.append("Batfish detected a critical network-behavior violation")
            if batfish_result.behavior_changes > 0:
                _audit(
                    "BATFISH_BEHAVIOR_CHANGE", "Info",
                    f"{batfish_result.behavior_changes} behavior change(s) detected",
                )

        risk_score = risk_result.risk_score
        risk_classification = risk_result.classification
        high_risk = risk_score >= 70 or "critical" in risk_classification.lower()

        decision = _combine(
            opa_result=opa_result,
            batfish_result=batfish_result,
            high_risk=high_risk,
            reasons=reasons,
        )

        result = ChangeValidationResult(
            decision=decision,
            overall_score=risk_score,
            syntax_passed=True,
            syntax_errors=[],
            syntax_warnings=syntax_result.warnings,
            opa=opa_result,
            batfish=batfish_result,
            risk_score=risk_score,
            risk_classification=risk_classification,
            blast_radius_devices=blast_radius_devices,
            reasons=reasons,
        )

        if decision == CombinedDecision.BLOCK:
            _audit("CHANGE_VALIDATION_BLOCKED", "Blocked", "; ".join(reasons))
        elif decision == CombinedDecision.REVIEW:
            _audit("CHANGE_VALIDATION_REVIEW", "Review", "; ".join(reasons))
        else:
            _audit("CHANGE_VALIDATION_PASSED", "Passed")

        _emit(EVT_VALIDATION_COMPLETED, decision=decision)
        try:
            _anchor_validation_evidence(
                db, device=device, change_request=change_request,
                proposed_config=proposed_config, result=result, actor=actor,
            )
        except Exception:  # noqa: BLE001 -- evidence anchoring is additive, never blocks validation
            logger.exception("Fabric evidence anchoring failed for validation %s", validation_id)
        return result
    except Exception as exc:  # noqa: BLE001 -- always emit .failed before propagating
        _audit("CHANGE_VALIDATION_BLOCKED", "Failed", str(exc))
        _emit(EVT_VALIDATION_FAILED, error=str(exc))
        raise


def _combine(
    *,
    opa_result: OpaResult,
    batfish_result: BatfishResult | None,
    high_risk: bool,
    reasons: list[str],
) -> str:
    # Highest severity first -- a later, lower-severity check can never
    # downgrade an earlier BLOCK/REVIEW.
    if opa_result.decision == OpaDecision.DENY:
        return CombinedDecision.BLOCK
    if batfish_result is not None and batfish_result.status == BatfishStatus.CRITICAL:
        return CombinedDecision.BLOCK

    if high_risk:
        reasons.append("Risk engine classified this change as high risk")
        return CombinedDecision.REVIEW
    if opa_result.decision == OpaDecision.REVIEW:
        reasons.append("OPA flagged this change for manual review")
        return CombinedDecision.REVIEW
    if batfish_result is not None and batfish_result.status == BatfishStatus.REVIEW:
        reasons.append("Batfish detected behavior changes requiring review")
        return CombinedDecision.REVIEW
    if batfish_result is not None and batfish_result.status == BatfishStatus.UNAVAILABLE and (
        high_risk or batfish_service.fail_closed
    ):
        reasons.append(
            "Batfish was unavailable for a high-risk change"
            if high_risk
            else "Batfish was unavailable and BATFISH_FAIL_CLOSED=true"
        )
        return CombinedDecision.REVIEW

    return CombinedDecision.PASS
