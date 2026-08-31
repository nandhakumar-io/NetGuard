"""Google OIDC ("Sign in with Google") login endpoints.

Two-step redirect flow, same shape as any OAuth "Login with X" button:

  GET /sso/google/login    -> 302 to Google's consent screen
  GET /sso/google/callback -> Google redirects back here with ?code=&state=,
                               we exchange/verify/provision, then 302 to the
                               frontend with a short-lived access token in
                               the URL fragment (never sent to any server,
                               including ours, on the next request) plus the
                               refresh token set as the same httpOnly cookie
                               the local-login flow uses.

Kept separate from app.api.auth (rather than folded into it) since it's
a fundamentally different trust model -- no password, no MFA challenge,
identity comes from Google instead -- and every failure mode here should
surface as a friendly redirect back to the login page, not a raw 401/500.
"""
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_sso_state_token, decode_sso_state_token
from app.services import sso_service
from app.services.sso_service import SsoLoginError, SsoNotConfigured

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sso", tags=["sso"])


def _login_error_redirect(reason: str) -> RedirectResponse:
    url = f"{settings.FRONTEND_URL.rstrip('/')}/login?{urlencode({'sso_error': reason})}"
    return RedirectResponse(url, status_code=302)


@router.get("/google/login")
def google_login(next: str | None = Query(None, description="Optional post-login frontend path, e.g. /incidents")):
    if not (settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET and settings.GOOGLE_REDIRECT_URI):
        return _login_error_redirect("sso_not_configured")

    state = create_sso_state_token(extra_claims={"next": next} if next else None)
    return RedirectResponse(sso_service.build_authorization_url(state), status_code=302)


@router.get("/google/callback")
def google_callback(
    request: Request,
    response: Response,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    db: Session = Depends(get_db),
):
    # Imported lazily to avoid a circular import with app.api.auth (both
    # need _issue_token_pair-equivalent logic); see _issue_token below.
    from app.api.auth import _issue_token_pair

    if error:
        # User clicked "Cancel" on Google's consent screen, or Google
        # itself returned an error (access_denied, etc).
        return _login_error_redirect(error)
    if not code or not state:
        return _login_error_redirect("missing_code_or_state")

    try:
        state_claims = decode_sso_state_token(state)
    except JWTError:
        return _login_error_redirect("invalid_or_expired_state")

    try:
        claims = sso_service.exchange_code_for_claims(code)
    except SsoNotConfigured:
        return _login_error_redirect("sso_not_configured")
    except SsoLoginError as exc:
        logger.warning("Google SSO login failed: %s", exc)
        return _login_error_redirect("google_login_failed")

    user = sso_service.find_or_create_user(db, provider="google", claims=claims)
    if user.is_active in (False, "false", "False"):
        return _login_error_redirect("account_disabled")

    # Reuses the exact same token-issuance path as local login (JWT
    # access token + rotated-on-use refresh cookie) so downstream code
    # (get_current_user, RBAC, session listing/revocation) can't tell
    # the difference between a local and an SSO-originated session.
    token = _issue_token_pair(db, user, response, request)

    next_path = state_claims.get("next") or "/"
    redirect_url = f"{settings.FRONTEND_URL.rstrip('/')}{next_path}#access_token={token.access_token}"
    redirect = RedirectResponse(redirect_url, status_code=302)
    # RedirectResponse is a fresh Response object -- copy over the
    # refresh-token Set-Cookie header _issue_token_pair wrote onto the
    # `response` dependency, since FastAPI only sends headers set on the
    # object it was given, not ones set on a response we return instead.
    for header, value in response.headers.items():
        if header.lower() == "set-cookie":
            redirect.headers.append(header, value)
    return redirect


@router.get("/providers")
def sso_providers():
    """Lets the frontend know whether to show a "Sign in with Google"
    button at all -- avoids a dead/erroring button in deployments that
    haven't configured SSO.
    """
    return {
        "google": bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET and settings.GOOGLE_REDIRECT_URI),
        "keycloak": bool(settings.OIDC_ISSUER and settings.OIDC_CLIENT_ID and settings.OIDC_REDIRECT_URI),
        "local": True,
    }
