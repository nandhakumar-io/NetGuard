"""Google OIDC login ("Sign in with Google").

Auth today is local email/password + TOTP (app.api.auth). That's fine
for a handful of engineers, but any org past that size wants logins
gated behind their IdP so a departing employee's Google Workspace
account being disabled is enough to cut off NetGuard access too --
without this, offboarding means someone has to remember a *second*
place to revoke access.

Deliberately a plain "OAuth code exchange + tokeninfo verify" flow via
httpx rather than pulling in Authlib/google-auth: the codebase already
depends on httpx for every other outbound HTTP call (Slack, Teams,
push, webhooks), and OIDC's authorization-code flow is a handful of
well-documented HTTP calls, so a new heavy dependency isn't buying much.
The one thing a hand-rolled flow gives up vs. a JWKS-based verifier is
validating the id_token's signature locally; instead this posts the
token to Google's own /tokeninfo endpoint, which validates signature,
issuer, audience and expiry server-side and hands back the verified
claims. That's an extra network round trip per login (acceptable --
login is not a hot path) in exchange for not maintaining a JWKS
cache/rotation ourselves.

Provisioning: first successful login for a not-yet-known Google
identity auto-creates a NetGuard User with SSO_DEFAULT_ROLE (lowest
privilege, same posture as local /auth/register). If
SSO_GROUP_ROLE_MAP + Workspace Admin SDK access are configured, the
user's highest-privilege matching group wins instead -- see
resolve_role_from_groups. Existing local accounts matching by email are
*linked* (sso_provider/sso_subject populated) rather than duplicated,
so someone who registered locally before SSO was turned on doesn't end
up with two accounts.
"""
from __future__ import annotations

import json
import logging
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO_ENDPOINT = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_GROUPS_ENDPOINT = "https://admin.googleapis.com/admin/directory/v1/groups"

TIMEOUT_SECONDS = 5.0


class SsoNotConfigured(Exception):
    pass


class SsoLoginError(Exception):
    """Anything that should surface to the user as "SSO login failed"
    rather than a 500 -- wrong hosted domain, revoked consent, Google
    outage, tampered state, etc."""


def _require_configured() -> None:
    if not (settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET and settings.GOOGLE_REDIRECT_URI):
        raise SsoNotConfigured("Google SSO is not configured (GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI unset)")


def build_authorization_url(state: str) -> str:
    """URL to redirect the browser to -- kicks off the standard OIDC
    authorization-code flow. `state` is the signed CSRF token from
    create_sso_state_token(); Google returns it unmodified to the
    callback so we can verify this request/response pair line up.
    """
    _require_configured()
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        # Re-prompts for consent/account chooser each time rather than
        # silently reusing whichever Google session is active in the
        # browser -- avoids a shared-workstation engineer accidentally
        # logging into NetGuard as whoever last signed into Google there.
        "prompt": "select_account",
    }
    if settings.GOOGLE_ALLOWED_HD:
        # Restricts the Google *account chooser* to the Workspace domain
        # as a UX nicety -- this is NOT the security boundary. The `hd`
        # claim on the returned id_token (checked in exchange_code_for_claims)
        # is the actual enforcement, since `hd` here is just a client-side
        # hint a user could strip from the URL.
        params["hd"] = settings.GOOGLE_ALLOWED_HD
    return f"{GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_code_for_claims(code: str) -> dict:
    """Exchanges the one-time authorization `code` for tokens, then
    verifies the id_token via Google's tokeninfo endpoint. Returns the
    verified claims dict (sub, email, email_verified, hd, name, ...).
    """
    _require_configured()
    try:
        token_resp = httpx.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise SsoLoginError(f"Could not reach Google to exchange login code: {exc}")

    if token_resp.status_code != 200:
        raise SsoLoginError("Google rejected the login code (it may have expired or already been used)")

    id_token = token_resp.json().get("id_token")
    if not id_token:
        raise SsoLoginError("Google did not return an id_token")

    try:
        info_resp = httpx.get(GOOGLE_TOKENINFO_ENDPOINT, params={"id_token": id_token}, timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise SsoLoginError(f"Could not verify login with Google: {exc}")

    if info_resp.status_code != 200:
        raise SsoLoginError("Google could not verify the login token")

    claims = info_resp.json()
    if claims.get("aud") != settings.GOOGLE_CLIENT_ID:
        # tokeninfo already checks this server-side, but a defense-in-depth
        # re-check here costs nothing and protects against ever swapping
        # in a JWKS-based local verifier later without remembering to
        # re-add the audience check.
        raise SsoLoginError("Login token was not issued for this application")
    if claims.get("email_verified") not in ("true", True):
        raise SsoLoginError("Google account email is not verified")
    if settings.GOOGLE_ALLOWED_HD and claims.get("hd") != settings.GOOGLE_ALLOWED_HD:
        raise SsoLoginError("This Google account is not part of the allowed organization")

    return claims


def resolve_role_from_groups(email: str) -> UserRole | None:
    """Best-effort: looks up the user's Google Workspace group
    memberships via the Admin SDK Directory API and returns the
    highest-privilege UserRole among SSO_GROUP_ROLE_MAP's matches, or
    None if group mapping isn't configured / lookup fails / no group
    matched -- callers fall back to SSO_DEFAULT_ROLE in that case.

    Requires GOOGLE_WORKSPACE_SERVICE_ACCOUNT_JSON (a service account
    key with domain-wide delegation) + GOOGLE_WORKSPACE_ADMIN_EMAIL (the
    Workspace admin to impersonate, since the Directory API has no
    non-impersonated service-account auth mode) in addition to
    SSO_GROUP_ROLE_MAP. This is genuinely optional -- most deployments
    should start with SSO_DEFAULT_ROLE alone and an admin promoting
    people afterward via the existing PATCH /auth/users/{id}/role, the
    same as local accounts already work today.
    """
    if not settings.SSO_GROUP_ROLE_MAP:
        return None
    try:
        group_role_map: dict[str, str] = json.loads(settings.SSO_GROUP_ROLE_MAP)
    except (json.JSONDecodeError, TypeError):
        logger.warning("SSO_GROUP_ROLE_MAP is not valid JSON; ignoring")
        return None
    if not (settings.GOOGLE_WORKSPACE_SERVICE_ACCOUNT_JSON and settings.GOOGLE_WORKSPACE_ADMIN_EMAIL):
        logger.warning("SSO_GROUP_ROLE_MAP is set but Workspace Admin SDK credentials are not; skipping group lookup")
        return None

    # Role privilege order, highest first -- if a user is in multiple
    # mapped groups, the most privileged one wins rather than whichever
    # group the API happened to list first.
    role_precedence = [UserRole.NETWORK_ADMIN, UserRole.SECURITY, UserRole.NETWORK_ENGINEER, UserRole.NOC_ENGINEER, UserRole.AUDITOR]

    try:
        import google.auth.transport.requests
        from google.oauth2 import service_account

        creds_info = json.loads(settings.GOOGLE_WORKSPACE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            creds_info,
            scopes=["https://www.googleapis.com/auth/admin.directory.group.readonly"],
            subject=settings.GOOGLE_WORKSPACE_ADMIN_EMAIL,
        )
        creds.refresh(google.auth.transport.requests.Request())
    except ImportError:
        logger.warning("google-auth is not installed; cannot resolve SSO group -> role mapping (pip install google-auth)")
        return None
    except Exception:
        logger.warning("Failed to load Workspace service account credentials for SSO group lookup", exc_info=True)
        return None

    try:
        resp = httpx.get(
            GOOGLE_GROUPS_ENDPOINT,
            params={"userKey": email},
            headers={"Authorization": f"Bearer {creds.token}"},
            timeout=TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        member_of = {g["email"] for g in resp.json().get("groups", [])}
    except Exception:
        logger.warning("Workspace group lookup failed for %s; falling back to SSO_DEFAULT_ROLE", email, exc_info=True)
        return None

    matched = {group_role_map[g] for g in member_of if g in group_role_map}
    for role in role_precedence:
        if role.value in matched:
            return role
    return None


def find_or_create_user(db: Session, *, provider: str, claims: dict) -> User:
    """Links or provisions a NetGuard user for a verified SSO identity.
    Match order: existing SSO link (sub) first, then existing local
    account by email (link it), then create new.
    """
    subject = claims["sub"]
    email = claims["email"]

    user = db.query(User).filter(User.sso_provider == provider, User.sso_subject == subject).first()
    if user:
        return user

    user = db.query(User).filter(User.email == email).first()
    if user:
        # Pre-existing local (or different-provider) account with this
        # email -- link rather than duplicate. Doesn't touch role or
        # hashed_password, so an admin-granted role and any existing
        # local password both survive the account being linked to SSO.
        user.sso_provider = provider
        user.sso_subject = subject
        db.commit()
        db.refresh(user)
        return user

    role = resolve_role_from_groups(email) or UserRole(settings.SSO_DEFAULT_ROLE)
    user = User(
        email=email,
        full_name=claims.get("name") or email.split("@")[0],
        hashed_password=None,
        role=role,
        sso_provider=provider,
        sso_subject=subject,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
