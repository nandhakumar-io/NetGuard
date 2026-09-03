"""Open Policy Agent integration -- policy-as-code decision engine.

Answers "is this proposed change allowed by NetGuard's security and
operational policies?" This is deliberately separate from
`validation_engine` (structural/syntax correctness) and from `risk_engine`
(quantitative risk scoring): OPA only evaluates declarative policy rules
against a normalized snapshot of the change, and is the *only* layer whose
rules live outside Python (in `opa/policies/*.rego`), so a policy change
never requires a backend deploy.

This module never sees device credentials or secrets -- see
`build_opa_input()`, which builds the request body from an explicit
allow-list of fields.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class OpaDecision:
    ALLOW = "allow"
    DENY = "deny"
    REVIEW = "review"
    UNAVAILABLE = "unavailable"


@dataclass
class OpaViolation:
    policy: str
    severity: str  # critical | high | medium | low
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class OpaResult:
    passed: bool
    decision: str
    violations: list[OpaViolation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    matched_policies: list[str] = field(default_factory=list)
    policy_version: str | None = None
    evaluation_time_ms: float = 0.0
    raw_decision: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "decision": self.decision,
            "violations": [
                {
                    "policy": v.policy,
                    "severity": v.severity,
                    "message": v.message,
                    "details": v.details,
                }
                for v in self.violations
            ],
            "warnings": self.warnings,
            "matched_policies": self.matched_policies,
            "policy_version": self.policy_version,
            "evaluation_time_ms": self.evaluation_time_ms,
            "error": self.error,
        }


def build_opa_input(
    *,
    device: dict[str, Any],
    current_config: str | None,
    proposed_config: str,
    change_request: dict[str, Any],
    topology: dict[str, Any] | None = None,
    risk: dict[str, Any] | None = None,
    blast_radius: dict[str, Any] | None = None,
    user_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the normalized OPA input document.

    Explicit allow-list of fields only -- never pass through credentials,
    tokens, or secrets, even if the caller's `device`/`user_context` dicts
    happen to contain them.
    """
    return {
        "change": {
            "id": str(change_request.get("id", "")),
            "description": change_request.get("description", ""),
            "priority": change_request.get("priority", "medium"),
            "maintenance_window": bool(change_request.get("in_maintenance_window", False)),
            "business_justification": change_request.get("business_justification"),
        },
        "device": {
            "id": str(device.get("id", "")),
            "hostname": device.get("hostname"),
            "vendor": device.get("vendor"),
            "platform": device.get("platform"),
            "role": device.get("role", "unknown"),
        },
        "user": {
            "id": str((user_context or {}).get("id", "")),
            "roles": (user_context or {}).get("roles", []),
        },
        "configuration": {
            "current": current_config or "",
            "proposed": proposed_config,
        },
        "topology": topology or {},
        "risk": risk or {},
        "blast_radius": blast_radius or {},
    }


class OpaService:
    """Thin, fail-closed-by-default client for the OPA HTTP API.

    `evaluate_change()` is the only method the rest of NetGuard should
    call -- it returns a structured `OpaResult` and never leaks raw OPA
    response shape to callers (see `to_dict()`).
    """

    def __init__(
        self,
        base_url: str | None = None,
        policy_path: str | None = None,
        timeout_seconds: float | None = None,
        fail_closed: bool | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.base_url = (base_url or settings.OPA_URL).rstrip("/")
        self.policy_path = policy_path or settings.OPA_POLICY_PATH
        self.timeout_seconds = timeout_seconds or settings.OPA_TIMEOUT_SECONDS
        self.fail_closed = settings.OPA_FAIL_CLOSED if fail_closed is None else fail_closed
        self.enabled = settings.OPA_ENABLED if enabled is None else enabled

    def _unavailable_result(self, error: str) -> OpaResult:
        logger.warning("OPA evaluation unavailable: %s (fail_closed=%s)", error, self.fail_closed)
        if self.fail_closed:
            return OpaResult(
                passed=False,
                decision=OpaDecision.DENY,
                warnings=[f"OPA unavailable and OPA_FAIL_CLOSED=true: {error}"],
                error=error,
            )
        return OpaResult(
            passed=True,
            decision=OpaDecision.UNAVAILABLE,
            warnings=[f"OPA unavailable, proceeding per OPA_FAIL_CLOSED=false: {error}"],
            error=error,
        )

    async def evaluate_change(
        self,
        device: dict[str, Any],
        current_config: str | None,
        proposed_config: str,
        change_request: dict[str, Any],
        topology: dict[str, Any] | None = None,
        user_context: dict[str, Any] | None = None,
        risk: dict[str, Any] | None = None,
        blast_radius: dict[str, Any] | None = None,
    ) -> OpaResult:
        if not self.enabled:
            return OpaResult(passed=True, decision=OpaDecision.ALLOW, warnings=["OPA_ENABLED=false, skipped"])

        opa_input = build_opa_input(
            device=device,
            current_config=current_config,
            proposed_config=proposed_config,
            change_request=change_request,
            topology=topology,
            risk=risk,
            blast_radius=blast_radius,
            user_context=user_context,
        )

        url = f"{self.base_url}{self.policy_path}"
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(url, json={"input": opa_input})
                resp.raise_for_status()
                body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return self._unavailable_result(str(exc))

        elapsed_ms = (time.monotonic() - started) * 1000
        return self._parse_decision(body, elapsed_ms)

    def _parse_decision(self, body: dict[str, Any], elapsed_ms: float) -> OpaResult:
        data = body.get("result", {}) if isinstance(body, dict) else {}

        violations = [
            OpaViolation(
                policy=v.get("policy", "unknown"),
                severity=v.get("severity", "medium"),
                message=v.get("message", ""),
                details=v.get("details", {}),
            )
            for v in data.get("violations", [])
        ]
        warnings = list(data.get("warnings", []))
        matched_policies = list(data.get("matched_policies", []))
        policy_version = data.get("policy_version")

        has_deny = any(v.severity in ("critical", "high") for v in violations) or bool(data.get("deny"))
        has_review = bool(data.get("review")) or any(v.severity == "medium" for v in violations)

        if has_deny:
            decision = OpaDecision.DENY
        elif has_review:
            decision = OpaDecision.REVIEW
        else:
            decision = OpaDecision.ALLOW

        return OpaResult(
            passed=decision != OpaDecision.DENY,
            decision=decision,
            violations=violations,
            warnings=warnings,
            matched_policies=matched_policies,
            policy_version=policy_version,
            evaluation_time_ms=elapsed_ms,
            raw_decision=data,
        )

    async def health_check(self) -> bool:
        if not self.enabled:
            return True
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.get(f"{self.base_url}/health")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False


opa_service = OpaService()
