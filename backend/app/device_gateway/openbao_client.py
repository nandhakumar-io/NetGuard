"""Minimal OpenBao client: AppRole login + KV v2 read, used to resolve
device credentials instead of the DB-Fernet path (Section 4 of the
hardening spec).

Deliberately narrow: this module can log in with a Role ID + Secret ID
and read one secret path. It cannot list secrets, write secrets, manage
policies, or do anything with a root/unseal token -- those capabilities
simply aren't implemented here, so even if every line of this file were
handed to an attacker verbatim, it wouldn't let them do more than what
the AppRole's own OpenBao-side policy already allows (see
`openbao/policies/device-gateway-policy.hcl`).

Why AppRole and not a static token: a static token baked into an env var
is exactly the kind of long-lived, broad credential the whole OpenBao
integration exists to get away from. AppRole exchanges a Role ID
(non-secret, safe to put in an env var / compose file) plus a Secret ID
(the actual secret, ideally injected via a mounted file or orchestrator
secret rather than plain env in a real deployment) for a short-lived
client token, scoped to whatever policy that AppRole is bound to on the
OpenBao side. The client token this module obtains is cached in memory
only, never persisted, and re-fetched on expiry.

Which container gets OPENBAO_ROLE_ID / OPENBAO_SECRET_ID at all is the
actual enforcement point: only `device-gateway` has them in
docker-compose.yaml. The `api` container has no OpenBao credentials of
any kind, so even if this exact module were imported into API code, it
would have nothing to authenticate with -- this is what makes "a
compromised API container cannot retrieve device credentials from
OpenBao" true, not a check inside this file.
"""
from __future__ import annotations

import logging
import threading
import time

import httpx

logger = logging.getLogger("netguard.device_gateway.openbao_client")


class OpenBaoError(Exception):
    pass


class OpenBaoAuthError(OpenBaoError):
    pass


class OpenBaoSecretNotFoundError(OpenBaoError):
    pass


class OpenBaoClient:
    """One instance per process (device-gateway). Not safe to share
    across processes; safe to share across threads/coroutines within one
    process (token refresh is lock-protected)."""

    def __init__(
        self,
        *,
        addr: str,
        role_id: str,
        secret_id: str,
        mount: str = "netguard-devices",
        timeout_seconds: float = 5.0,
        # Refresh this many seconds before actual expiry, so an
        # in-flight request never races a token that expires mid-call.
        refresh_margin_seconds: int = 30,
    ) -> None:
        self._addr = addr.rstrip("/")
        self._role_id = role_id
        self._secret_id = secret_id
        self._mount = mount
        self._timeout = timeout_seconds
        self._refresh_margin = refresh_margin_seconds

        self._client_token: str | None = None
        self._token_expires_at: float = 0.0
        self._lock = threading.Lock()

    def _login(self, http: httpx.Client) -> None:
        resp = http.post(
            f"{self._addr}/v1/auth/approle/login",
            json={"role_id": self._role_id, "secret_id": self._secret_id},
            timeout=self._timeout,
        )
        if resp.status_code != 200:
            raise OpenBaoAuthError(
                f"OpenBao AppRole login failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        data = resp.json()
        auth = data.get("auth") or {}
        token = auth.get("client_token")
        lease_duration = auth.get("lease_duration", 0)
        if not token:
            raise OpenBaoAuthError("OpenBao AppRole login response had no client_token")
        self._client_token = token
        self._token_expires_at = time.monotonic() + max(lease_duration - self._refresh_margin, 0)
        logger.info("openbao_client: obtained client token, ttl=%ss", lease_duration)

    def _ensure_token(self, http: httpx.Client) -> str:
        with self._lock:
            if self._client_token is None or time.monotonic() >= self._token_expires_at:
                self._login(http)
            return self._client_token  # type: ignore[return-value]

    def read_device_credential(self, credential_ref: str) -> dict:
        """Reads `<mount>/data/<credential_ref>` (KV v2 layout) and
        returns the secret's `data.data` dict (e.g. {"username": ...,
        "password": ...} or {"username": ..., "private_key": ...}).

        Raises OpenBaoSecretNotFoundError if nothing is stored at that
        path -- callers should treat this the same as
        CredentialNotFoundError from the legacy path, not as a crash.
        """
        with httpx.Client() as http:
            token = self._ensure_token(http)
            resp = http.get(
                f"{self._addr}/v1/{self._mount}/data/{credential_ref}",
                headers={"X-Vault-Token": token},
                timeout=self._timeout,
            )
            if resp.status_code == 404:
                raise OpenBaoSecretNotFoundError(
                    f"no credential stored at {self._mount}/data/{credential_ref}"
                )
            if resp.status_code == 403:
                # Distinguish "not found" from "policy forbids this path"
                # -- a 403 here means the AppRole's policy doesn't grant
                # this specific ref, which is a policy/scoping problem
                # worth its own log line, not a normal missing-credential.
                raise OpenBaoAuthError(
                    f"OpenBao denied read of {self._mount}/data/{credential_ref} "
                    "(check the AppRole's policy covers this path)"
                )
            if resp.status_code != 200:
                raise OpenBaoError(f"OpenBao read failed: HTTP {resp.status_code} {resp.text[:200]}")

            body = resp.json()
            data = (body.get("data") or {}).get("data")
            if not data:
                raise OpenBaoSecretNotFoundError(
                    f"empty secret at {self._mount}/data/{credential_ref}"
                )
            return data


_client: OpenBaoClient | None = None
_client_lock = threading.Lock()


def get_client():
    """Lazily builds the process-wide client from settings. Returns None
    if OpenBao isn't configured (OPENBAO_ADDR unset) -- callers fall back
    to the legacy credential path in that case, so a Gateway deployed
    without OpenBao yet still works, just without this specific
    hardening (see credential_service.get_ssh_password's fallback
    chain)."""
    global _client
    from app.core.config import settings

    if not settings.OPENBAO_ADDR or not settings.OPENBAO_ROLE_ID or not settings.OPENBAO_SECRET_ID:
        return None

    with _client_lock:
        if _client is None:
            _client = OpenBaoClient(
                addr=settings.OPENBAO_ADDR,
                role_id=settings.OPENBAO_ROLE_ID,
                secret_id=settings.OPENBAO_SECRET_ID,
                mount=settings.OPENBAO_MOUNT,
            )
        return _client
