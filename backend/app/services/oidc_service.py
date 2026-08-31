"""Keycloak login via generic OIDC (Section 1).

Identity vs. authorization split (Section 2): this module's ONLY job is
"prove this browser belongs to Keycloak subject X, with these claims" --
it never grants a NetGuard role/permission directly. find_or_create_user()
below provisions/links a NetGuard User exactly the way sso_service.py
already does for Google, and every subsequent request still goes through
NetGuard's own RBAC/JIT/approval pipeline. Keycloak group/role claims can
set the *initial* role for a newly-provisioned user (same posture as
Google's SSO_GROUP_ROLE_MAP), but an admin can always change it afterward
via the existing PATCH /auth/users/{id}/role -- Keycloak is not the
source of truth for NetGuard permissions.

Why this deployment uses Authorization Code + PKCE *and* a client
secret, rather than treating them as alternatives: PKCE (RFC 7636)
defends the authorization-code leg against interception by something
that can see the redirect but not the subsequent back-channel token
request (a malicious app on the same device, a leaky proxy/log that
captured the redirect URL) -- it doesn't require a confidential client
to be effective. A client secret defends the token-exchange call itself
(only the real backend, which holds the secret, can redeem a code even
if it also somehow obtained a valid code_verifier). NetGuard's API is a
confidential client (server-side, secret storable via config/OpenBao)
with browser-facing redirects, so there's no cost to using both, and
each closes a gap the other doesn't.

Why the code_verifier lives in a short-lived httpOnly cookie rather than
inside the signed `state` JWT (the way Google's `next` param is carried
today, see app.core.security.create_sso_state_token): `state` travels
through the front channel -- the same browser redirect an attacker
capable of leaking the authorization `code` could also observe. Putting
the verifier there would let that same attacker read it and defeat PKCE
entirely. A cookie set on /login and read back on /callback travels via
Cookie/Set-Cookie headers instead, which is a materially different
exposure surface (not logged in browser history, not sent via HTTP
Referer), so it preserves PKCE's actual threat-model benefit.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from urllib.parse import urlencode

import httpx
from jose import jwt
from jose.exceptions import JWTError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5.0
PKCE_COOKIE_NAME = "netguard_oidc_pkce_verifier"
PKCE_COOKIE_PATH = f"{settings.API_V1_PREFIX}/sso/keycloak"


class OidcNotConfigured(Exception):
    pass


class OidcLoginError(Exception):
    """Anything that should surface as a friendly "login failed" redirect
    rather than a raw 500 -- discovery-doc fetch failure, expired code,
    signature mismatch, wrong issuer/audience, Keycloak outage, etc."""


def _require_configured() -> None:
    if not (settings.OIDC_ISSUER and settings.OIDC_CLIENT_ID and settings.OIDC_REDIRECT_URI):
        raise OidcNotConfigured("Keycloak OIDC is not configured (OIDC_ISSUER/CLIENT_ID/REDIRECT_URI unset)")


# --- Discovery + JWKS caching -----------------------------------------
#
# Both caches are process-local and time-bounded rather than fetched on
# every login (an extra round trip per login is fine; an extra round
# trip is NOT fine on a request that's already blocking a user's
# browser redirect if Keycloak is briefly slow) or cached forever
# (Keycloak signing-key rotation must eventually be picked up without a
# NetGuard restart -- see OIDC_JWKS_CACHE_SECONDS).

_discovery_cache: dict | None = None
_discovery_cached_at: float = 0.0
_DISCOVERY_CACHE_SECONDS = 3600

_jwks_cache: dict | None = None
_jwks_cached_at: float = 0.0


def _discovery_document() -> dict:
    global _discovery_cache, _discovery_cached_at
    _require_configured()
    now = time.monotonic()
    if _discovery_cache is not None and (now - _discovery_cached_at) < _DISCOVERY_CACHE_SECONDS:
        return _discovery_cache
    url = f"{settings.OIDC_ISSUER.rstrip('/')}/.well-known/openid-configuration"
    try:
        resp = httpx.get(url, timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        if _discovery_cache is not None:
            # Serve the stale doc rather than break login entirely on a
            # transient discovery-endpoint hiccup -- it's still signed
            # against the same JWKS either way.
            logger.warning("oidc_service: discovery refresh failed, using cached copy: %s", exc)
            return _discovery_cache
        raise OidcLoginError(f"Could not reach Keycloak discovery endpoint: {exc}")
    doc = resp.json()
    if doc.get("issuer") != settings.OIDC_ISSUER:
        raise OidcLoginError(
            f"Keycloak discovery document issuer ({doc.get('issuer')}) does not match "
            f"configured OIDC_ISSUER ({settings.OIDC_ISSUER})"
        )
    _discovery_cache = doc
    _discovery_cached_at = now
    return doc


def _jwks() -> dict:
    global _jwks_cache, _jwks_cached_at
    now = time.monotonic()
    if _jwks_cache is not None and (now - _jwks_cached_at) < settings.OIDC_JWKS_CACHE_SECONDS:
        return _jwks_cache
    jwks_uri = _discovery_document()["jwks_uri"]
    try:
        resp = httpx.get(jwks_uri, timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        if _jwks_cache is not None:
            logger.warning("oidc_service: JWKS refresh failed, using cached keys: %s", exc)
            return _jwks_cache
        raise OidcLoginError(f"Could not fetch Keycloak signing keys: {exc}")
    _jwks_cache = resp.json()
    _jwks_cached_at = now
    return _jwks_cache


def _signing_key_for(token: str):
    """Picks the JWK matching the token's `kid` header. Refreshes the
    JWKS cache once on a miss (handles the case where Keycloak rotated
    keys since our last fetch) before giving up -- a `kid` that's still
    unknown after a fresh fetch is treated as invalid, not silently
    accepted with the wrong key."""
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    keys = _jwks()
    match = next((k for k in keys.get("keys", []) if k.get("kid") == kid), None)
    if match is None:
        global _jwks_cached_at
        _jwks_cached_at = 0.0  # force refetch
        keys = _jwks()
        match = next((k for k in keys.get("keys", []) if k.get("kid") == kid), None)
    if match is None:
        raise OidcLoginError(f"id_token signed with unknown key id {kid!r}")
    return match


# --- PKCE ---------------------------------------------------------------

def _generate_pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorization_url(state: str) -> tuple[str, str]:
    """Returns (authorization_url, code_verifier). Caller is responsible
    for putting code_verifier in the PKCE cookie -- kept out of this
    function since cookie-setting needs the FastAPI Response object."""
    _require_configured()
    doc = _discovery_document()
    verifier, challenge = _generate_pkce_pair()
    params = {
        "client_id": settings.OIDC_CLIENT_ID,
        "redirect_uri": settings.OIDC_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{doc['authorization_endpoint']}?{urlencode(params)}", verifier


def exchange_code_for_claims(code: str, code_verifier: str) -> dict:
    """Exchanges the authorization code (+ PKCE verifier) for tokens,
    then verifies the id_token's signature locally against Keycloak's
    JWKS and checks iss/aud/exp -- unlike Google's tokeninfo-based
    verification, this does NOT trust a third-party endpoint to have
    done the check; it's done here, against keys NetGuard fetched
    itself, per Section 1's explicit requirement to validate JWKS/iss/
    aud/exp."""
    _require_configured()
    doc = _discovery_document()

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.OIDC_REDIRECT_URI,
        "client_id": settings.OIDC_CLIENT_ID,
        "code_verifier": code_verifier,
    }
    if settings.OIDC_CLIENT_SECRET:
        data["client_secret"] = settings.OIDC_CLIENT_SECRET

    try:
        token_resp = httpx.post(doc["token_endpoint"], data=data, timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise OidcLoginError(f"Could not reach Keycloak to exchange login code: {exc}")

    if token_resp.status_code != 200:
        raise OidcLoginError("Keycloak rejected the login code (it may have expired, already been used, or PKCE verification failed)")

    id_token = token_resp.json().get("id_token")
    if not id_token:
        raise OidcLoginError("Keycloak did not return an id_token")

    audience = settings.OIDC_AUDIENCE or settings.OIDC_CLIENT_ID
    try:
        key = _signing_key_for(id_token)
        claims = jwt.decode(
            id_token,
            key,
            algorithms=[key.get("alg", "RS256")],
            audience=audience,
            issuer=settings.OIDC_ISSUER,
            options={"require_exp": True, "require_iat": True},
        )
    except JWTError as exc:
        raise OidcLoginError(f"Could not verify Keycloak login token: {exc}")

    if claims.get("email_verified") is False:
        raise OidcLoginError("Keycloak account email is not verified")

    return claims


def resolve_role_from_claims(claims: dict) -> UserRole | None:
    """Maps Keycloak realm/client role or group claims to a NetGuard
    UserRole via OIDC_GROUP_ROLE_MAP (same '{"claim-value": "role"}' JSON
    shape as Google's SSO_GROUP_ROLE_MAP). Looks at both `groups` and
    Keycloak's default `realm_access.roles` claim shapes since which one
    a given realm emits depends on its client scope mapper configuration
    -- checking both means this works without requiring the operator to
    add a custom mapper just to match NetGuard's expectations."""
    if not settings.OIDC_GROUP_ROLE_MAP:
        return None
    try:
        mapping: dict[str, str] = json.loads(settings.OIDC_GROUP_ROLE_MAP)
    except (json.JSONDecodeError, TypeError):
        logger.warning("OIDC_GROUP_ROLE_MAP is not valid JSON; ignoring")
        return None

    role_precedence = [
        UserRole.NETWORK_ADMIN,
        UserRole.SECURITY,
        UserRole.NETWORK_ENGINEER,
        UserRole.NOC_ENGINEER,
        UserRole.AUDITOR,
    ]
    claim_values = set(claims.get("groups") or []) | set((claims.get("realm_access") or {}).get("roles") or [])
    matched = {mapping[c] for c in claim_values if c in mapping}
    for role in role_precedence:
        if role.value in matched:
            return role
    return None


def find_or_create_user(db: Session, *, claims: dict) -> User:
    """Same match order as sso_service.find_or_create_user: existing
    Keycloak link (sub) first, then existing account by email (link it),
    then create new. provider is always "keycloak" here so a user who
    also has a Google-linked account isn't ambiguous -- see
    User.sso_provider/sso_subject, which together form the lookup key."""
    subject = claims["sub"]
    email = claims["email"]

    user = db.query(User).filter(User.sso_provider == "keycloak", User.sso_subject == subject).first()
    if user:
        return user

    user = db.query(User).filter(User.email == email).first()
    if user:
        user.sso_provider = "keycloak"
        user.sso_subject = subject
        db.commit()
        db.refresh(user)
        return user

    role = resolve_role_from_claims(claims) or UserRole(settings.OIDC_DEFAULT_ROLE)
    user = User(
        email=email,
        full_name=claims.get("name") or email.split("@")[0],
        hashed_password=None,
        role=role,
        sso_provider="keycloak",
        sso_subject=subject,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
