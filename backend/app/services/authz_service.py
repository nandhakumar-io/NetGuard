"""OPA API-level authorization service (Phase 3 of Hybrid Auth).

Separate from app.services.opa_service which handles *config-compliance*
checks. That module answers "is this proposed config change safe per our
network policies?" -- a per-change-request evaluation with a rich
violations/warnings/review result.

This module answers the simpler question: "may this user perform <action>
on <resource>?" -- a boolean allow/deny per API call, evaluated against the
netguard.authz Rego package (opa/policies/authz.rego).

The two are intentionally separate:
  - Different policy path (/v1/data/netguard/authz/allow vs /v1/data/netguard)
  - Different input shape (user+action+resource vs device+config+change)
  - Different failure semantics (fail-open by default here, fail-closed
    for config compliance where the cost of a false-allow is a bad deploy)
  - Different call sites (FastAPI dependency vs change-request pipeline)

Callers should use `authz_service.check(...)` directly for one-off checks,
or wire `require_opa_authz(action)` from app.core.deps into FastAPI route
dependencies for declarative enforcement.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from app.core.config import settings

if TYPE_CHECKING:
    from app.models.user import User

logger = logging.getLogger(__name__)


def _build_authz_input(user: "User", action: str, resource: dict[str, Any]) -> dict[str, Any]:
    """Build the OPA input document for an API authZ check.

    Explicit allow-list only -- never pass credentials, tokens, or any
    attribute not needed for the policy decision.
    """
    from app.services import jit_service  # local import: avoids import cycle

    # Collect the user's current set of active JIT-elevated role values so
    # the authz.rego policy can grant actions that require an elevated role.
    # We pass DB session=None here and handle the AttributeError gracefully
    # because the dep is always called with an open session via FastAPI;
    # a None db would only happen in tests that bypass the dependency.
    try:
        from app.core.database import SessionLocal

        _db = SessionLocal()
        try:
            jit_roles = list(jit_service.active_roles_for_user(_db, user.id))
        finally:
            _db.close()
    except Exception:
        jit_roles = []

    return {
        "user": {
            "id": str(user.id),
            "roles": [user.role.value]
            + [r.strip() for r in (user.extra_roles or "").split(",") if r.strip()],
            "tenant_id": str(user.tenant_id) if user.tenant_id else None,
            "is_msp_staff": bool(user.is_msp_staff),
            "jit_roles": jit_roles,
        },
        "action": action,
        "resource": resource,
    }


class AuthzService:
    """Thin async OPA client for API-level authorization decisions.

    `check()` is the only method callers should use -- it returns a bool and
    never leaks raw OPA response shape upward.
    """

    def __init__(
        self,
        base_url: str | None = None,
        policy_path: str | None = None,
        timeout_seconds: float = 5.0,
        fail_closed: bool | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.base_url = (base_url or settings.OPA_URL).rstrip("/")
        self.policy_path = policy_path or settings.OPA_AUTHZ_POLICY_PATH
        self.timeout_seconds = timeout_seconds
        self.fail_closed = settings.OPA_AUTHZ_FAIL_CLOSED if fail_closed is None else fail_closed
        self.enabled = settings.OPA_ENABLED if enabled is None else enabled

    async def check(
        self,
        user: "User",
        action: str,
        resource: dict[str, Any] | None = None,
    ) -> bool:
        """Ask OPA whether `user` may perform `action` on `resource`.

        Returns True  -- allow (OPA said yes, or OPA is disabled/down and
                         fail_closed=False so we fall back to "allow and let
                         the existing require_roles guard decide").
        Returns False -- deny (OPA said no, or OPA is down and
                         fail_closed=True).

        Never raises -- callers (require_opa_authz in deps.py) convert a
        False result into an HTTP 403.
        """
        if not self.enabled:
            logger.debug("authz_service: OPA_ENABLED=false, skipping API authZ check")
            return True

        opa_input = _build_authz_input(user, action, resource or {})
        url = f"{self.base_url}{self.policy_path}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(url, json={"input": opa_input})
                resp.raise_for_status()
                body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "authz_service: OPA unreachable for action=%r user=%s: %s "
                "(fail_closed=%s)",
                action,
                user.id,
                exc,
                self.fail_closed,
            )
            # fail_closed=True -> deny (return False), fail_closed=False -> allow
            return not self.fail_closed

        # OPA returns {"result": true} or {"result": false} for a boolean rule.
        # An undefined rule (policy doesn't cover this action yet) produces
        # {"result": null} or an absent "result" key -- treat as deny so a
        # missing policy never silently grants access.
        result = body.get("result")
        if result is True:
            logger.debug("authz_service: OPA allowed action=%r for user=%s", action, user.id)
            return True

        logger.info(
            "authz_service: OPA denied action=%r for user=%s (result=%r)",
            action,
            user.id,
            result,
        )
        return False


# Module-level singleton -- same pattern as opa_service.opa_service and
# the other service singletons in this package. Tests can override settings
# by instantiating AuthzService directly with explicit constructor args.
authz_service = AuthzService()
