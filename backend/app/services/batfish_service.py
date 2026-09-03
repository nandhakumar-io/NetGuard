"""Batfish integration -- network behavior and pre-deployment simulation.

Answers "if this configuration is applied, what will the network actually
allow or break?" -- reachability, ACL behavior, routing behavior, and
before/after behavioral comparisons. This is NOT a generic config parser;
`validation_engine` already owns syntax/structural checks, and this module
never duplicates that.

All Batfish-specific logic (pybatfish client, snapshot lifecycle, query
construction) is isolated here so the rest of NetGuard only ever sees the
dataclasses defined below.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

# Vendors Batfish has meaningful config-parsing support for. Anything else
# is BATFISH_UNSUPPORTED -- never silently treated as SAFE (see
# `validate_configuration` below and Section 8/21 of the integration spec).
SUPPORTED_VENDOR_PLATFORMS = {
    ("cisco", "ios"),
    ("cisco", "ios-xe"),
    ("cisco", "iosxe"),
    ("cisco", "nx-os"),
    ("cisco", "nxos"),
    ("arista", "eos"),
    ("juniper", "junos"),
}


class BatfishStatus(str, Enum):
    PASS = "pass"
    REVIEW = "review"
    CRITICAL = "critical"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"


@dataclass
class BehaviorFinding:
    """One before/after behavioral comparison result (Section 10)."""

    query: str
    source: str
    destination: str
    protocol: str | None
    port: int | None
    before: str  # e.g. "DENIED" / "ACCEPTED" / "UNKNOWN"
    after: str
    behavior_changed: bool
    severity: str  # critical | high | medium | low | info
    explanation: str
    affected_device: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "source": self.source,
            "destination": self.destination,
            "protocol": self.protocol,
            "port": self.port,
            "before": self.before,
            "after": self.after,
            "behavior_changed": self.behavior_changed,
            "severity": self.severity,
            "explanation": self.explanation,
            "affected_device": self.affected_device,
        }


@dataclass
class BatfishResult:
    status: BatfishStatus
    snapshot_id: str | None = None
    findings: list[BehaviorFinding] = field(default_factory=list)
    behavior_changes: int = 0
    duration_ms: float = 0.0
    batfish_version: str | None = None
    reason: str | None = None

    @property
    def passed(self) -> bool:
        return self.status in (BatfishStatus.PASS, BatfishStatus.UNSUPPORTED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "snapshot_id": self.snapshot_id,
            "findings": [f.to_dict() for f in self.findings],
            "behavior_changes": self.behavior_changes,
            "duration_ms": self.duration_ms,
            "batfish_version": self.batfish_version,
            "reason": self.reason,
        }


def is_vendor_supported(vendor: str | None, platform: str | None) -> bool:
    if not vendor or not platform:
        return False
    return (vendor.lower(), platform.lower()) in SUPPORTED_VENDOR_PLATFORMS


def snapshot_name(change_request_id: str, revision: int = 1) -> str:
    prefix = settings.BATFISH_SNAPSHOT_NAME_PREFIX
    return f"{prefix}-cr-{change_request_id}-{revision}"


class BatfishService:
    """Isolates all pybatfish-specific logic.

    Deliberately lazy-imports `pybatfish` so the rest of the backend can
    import this module (e.g. for `is_vendor_supported`, dataclasses, or
    tests) without pybatfish installed / Batfish reachable.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        timeout_seconds: float | None = None,
        enabled: bool | None = None,
        fail_closed: bool | None = None,
    ) -> None:
        self.host = host or settings.BATFISH_HOST
        self.port = port or settings.BATFISH_PORT
        self.timeout_seconds = timeout_seconds or settings.BATFISH_TIMEOUT_SECONDS
        self.enabled = settings.BATFISH_ENABLED if enabled is None else enabled
        self.fail_closed = settings.BATFISH_FAIL_CLOSED if fail_closed is None else fail_closed
        self._session = None

    def _get_session(self):
        if self._session is not None:
            return self._session
        from pybatfish.client.session import Session  # lazy import

        session = Session(host=self.host)
        session.port = self.port
        self._session = session
        return session

    def _unavailable(self, reason: str, started: float) -> BatfishResult:
        logger.warning("Batfish unavailable: %s", reason)
        return BatfishResult(
            status=BatfishStatus.UNAVAILABLE,
            reason=reason,
            duration_ms=(time.monotonic() - started) * 1000,
        )

    async def create_snapshot(
        self,
        change_request_id: str,
        configs: dict[str, str],
        revision: int = 1,
    ) -> str:
        """Write an isolated snapshot dir (current + proposed configs for
        the affected devices only) and upload it under a deterministic
        name (`cr-{id}-{revision}`). Never touches production device
        state -- this is a purely local, ephemeral snapshot directory.
        """
        import tempfile
        from pathlib import Path

        name = snapshot_name(change_request_id, revision)
        with tempfile.TemporaryDirectory(prefix=f"bf-{name}-") as tmp:
            configs_dir = Path(tmp) / "configs"
            configs_dir.mkdir(parents=True, exist_ok=True)
            for hostname, cfg_text in configs.items():
                safe_name = "".join(c for c in hostname if c.isalnum() or c in ("-", "_")) or "device"
                (configs_dir / f"{safe_name}.cfg").write_text(cfg_text)

            session = self._get_session()
            session.init_snapshot(str(tmp), name=name, overwrite=True)
        return name

    async def validate_configuration(
        self,
        change_request_id: str,
        device: dict[str, Any],
        current_config: str | None,
        proposed_config: str,
        related_configs: dict[str, str] | None = None,
        before_after_queries: list[dict[str, Any]] | None = None,
    ) -> BatfishResult:
        started = time.monotonic()

        if not self.enabled:
            return BatfishResult(status=BatfishStatus.UNSUPPORTED, reason="BATFISH_ENABLED=false")

        if not is_vendor_supported(device.get("vendor"), device.get("platform")):
            return BatfishResult(
                status=BatfishStatus.UNSUPPORTED,
                reason=(
                    f"Batfish does not have supported parsing for "
                    f"{device.get('vendor')}/{device.get('platform')}; behavioral "
                    f"simulation was not performed for this device."
                ),
                duration_ms=(time.monotonic() - started) * 1000,
            )

        try:
            proposed_configs = dict(related_configs or {})
            proposed_configs[device.get("hostname", device.get("id", "device"))] = proposed_config
            proposed_snapshot = await self.create_snapshot(change_request_id, proposed_configs, revision=1)

            before_snapshot = None
            if current_config:
                before_configs = dict(related_configs or {})
                before_configs[device.get("hostname", device.get("id", "device"))] = current_config
                before_snapshot = await self.create_snapshot(change_request_id, before_configs, revision=0)

            findings = self._run_standard_checks(proposed_snapshot, before_snapshot, device)
            findings += self._run_before_after_queries(
                proposed_snapshot, before_snapshot, before_after_queries or []
            )
        except Exception as exc:  # pybatfish/session errors -- never raise into the caller
            return self._unavailable(str(exc), started)

        behavior_changes = sum(1 for f in findings if f.behavior_changed)
        critical = any(f.severity == "critical" for f in findings)
        review = any(f.severity in ("high", "medium") for f in findings)

        if critical:
            status = BatfishStatus.CRITICAL
        elif review:
            status = BatfishStatus.REVIEW
        else:
            status = BatfishStatus.PASS

        return BatfishResult(
            status=status,
            snapshot_id=proposed_snapshot,
            findings=findings,
            behavior_changes=behavior_changes,
            duration_ms=(time.monotonic() - started) * 1000,
        )

    def _run_standard_checks(
        self, proposed_snapshot: str, before_snapshot: str | None, device: dict[str, Any]
    ) -> list[BehaviorFinding]:
        """Runs the fixed set of security-relevant reachability checks
        (Section 9): guest->management, internet->management, VLAN
        isolation. Uses `network_policies.json` (see opa/data/) for the
        actual subnet definitions rather than hardcoding them here.
        """
        findings: list[BehaviorFinding] = []
        policies = self._load_network_policies()
        for check in policies.get("reachability_checks", []):
            finding = self._check_reachability(
                proposed_snapshot,
                before_snapshot,
                source=check["source"],
                destination=check["destination"],
                query_name=check.get("name", "reachability_check"),
                expected_denied=check.get("expect_denied", True),
                severity_if_violated=check.get("severity", "critical"),
            )
            if finding is not None:
                findings.append(finding)
        return findings

    def _run_before_after_queries(
        self,
        proposed_snapshot: str,
        before_snapshot: str | None,
        queries: list[dict[str, Any]],
    ) -> list[BehaviorFinding]:
        findings = []
        for q in queries:
            finding = self._check_reachability(
                proposed_snapshot,
                before_snapshot,
                source=q["source"],
                destination=q["destination"],
                query_name=q.get("name", "custom_query"),
                protocol=q.get("protocol"),
                port=q.get("port"),
            )
            if finding is not None:
                findings.append(finding)
        return findings

    def _check_reachability(
        self,
        proposed_snapshot: str,
        before_snapshot: str | None,
        *,
        source: str,
        destination: str,
        query_name: str,
        protocol: str | None = None,
        port: int | None = None,
        expected_denied: bool | None = None,
        severity_if_violated: str = "high",
    ) -> BehaviorFinding | None:
        from pybatfish.datamodel import HeaderConstraints

        session = self._get_session()
        headers = HeaderConstraints(srcIps=source, dstIps=destination, applications=[protocol] if protocol else None)

        session.set_snapshot(proposed_snapshot)
        after_answer = session.q.reachability(headers=headers).answer()
        after_result = "ACCEPTED" if len(after_answer.frame()) > 0 else "DENIED"

        before_result = "UNKNOWN"
        if before_snapshot:
            session.set_snapshot(before_snapshot)
            before_answer = session.q.reachability(headers=headers).answer()
            before_result = "ACCEPTED" if len(before_answer.frame()) > 0 else "DENIED"

        behavior_changed = before_snapshot is not None and before_result != after_result
        violates_policy = expected_denied is True and after_result == "ACCEPTED"

        if not behavior_changed and not violates_policy:
            return None

        severity = severity_if_violated if violates_policy else ("high" if behavior_changed else "info")
        explanation = (
            f"{query_name}: {source} -> {destination} was {before_result} before this change "
            f"and is {after_result} after it."
            if before_snapshot
            else f"{query_name}: {source} -> {destination} is {after_result} in the proposed configuration."
        )
        return BehaviorFinding(
            query=query_name,
            source=source,
            destination=destination,
            protocol=protocol,
            port=port,
            before=before_result,
            after=after_result,
            behavior_changed=behavior_changed,
            severity=severity,
            explanation=explanation,
        )

    def _load_network_policies(self) -> dict[str, Any]:
        import json
        from pathlib import Path

        candidates = [
            Path(settings.OPA_POLICY_DATA_PATH) if getattr(settings, "OPA_POLICY_DATA_PATH", None) else None,
            Path(__file__).resolve().parents[3] / "opa" / "data" / "network_policies.json",
        ]
        for path in candidates:
            if path and path.exists():
                try:
                    return json.loads(path.read_text())
                except (OSError, ValueError):
                    continue
        return {"reachability_checks": []}

    async def health_check(self) -> bool:
        if not self.enabled:
            return True
        try:
            session = self._get_session()
            # A cheap call that requires the coordinator to actually respond.
            session.list_snapshots()
            return True
        except Exception:
            return False

    async def cleanup_snapshots(self, change_request_id: str, keep_latest: int = 1) -> None:
        """Delete temporary Batfish snapshots for a change request beyond
        the configured retention. Best-effort -- failures are logged, not
        raised, since this is housekeeping, not part of the validation
        gate.
        """
        try:
            session = self._get_session()
            prefix = f"{settings.BATFISH_SNAPSHOT_NAME_PREFIX}-cr-{change_request_id}-"
            names = sorted(n for n in session.list_snapshots() if n.startswith(prefix))
            for name in names[:-keep_latest] if keep_latest > 0 else names:
                session.delete_snapshot(name)
        except Exception as exc:
            logger.info("Batfish snapshot cleanup skipped for %s: %s", change_request_id, exc)


batfish_service = BatfishService()
