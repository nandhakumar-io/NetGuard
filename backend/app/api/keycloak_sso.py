"""Keycloak login endpoints (Section 1).

  GET /sso/keycloak/login    -> 302 to Keycloak's login page, sets a
                                 short-lived httpOnly PKCE-verifier cookie
  GET /sso/keycloak/callback -> Keycloak redirects back with ?code=&state=,
                                 we verify PKCE + state, exchange the code,
                                 verify the id_token against Keycloak's
                                 JWKS, provision/link a NetGuard user, then
                                 issue NetGuard's own token pair exactly
                                 like local login does.

Deliberately separate from app.api.sso (Google) rather than a shared
"generic OIDC" router: the two providers are allowed to be enabled
simultaneously during a Keycloak migration (Section 1), and keeping them
in different files/routes makes that overlap unambiguous rather than
something one shared handler has to branch on.
"""
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Cookie, Depends, Query, Request, Response
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_sso_state_token, decode_sso_state_token
from app.services import oidc_service
from app.services.oidc_service import OidcLoginError, OidcNotConfigured

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sso/keycloak", tags=["sso"])


def _login_error_redirect(reason: str) -> Response:
    from fastapi.responses import RedirectResponse

    url = f"{settings.FRONTEND_URL.rstrip('/')}/login?{urlencode({'sso_error': reason})}"
    resp = RedirectResponse(url, status_code=302)
    resp.delete_cookie(key=oidc_service.PKCE_COOKIE_NAME, path=oidc_service.PKCE_COOKIE_PATH)
    return resp


@router.get("/login")
def keycloak_login(
    response: Response,
    next: str | None = Query(None, description="Optional post-login frontend path, e.g. /incidents"),
):
    if not (settings.OIDC_ISSUER and settings.OIDC_CLIENT_ID and settings.OIDC_REDIRECT_URI):
        return _login_error_redirect("sso_not_configured")

    state = create_sso_state_token(extra_claims={"next": next} if next else None)
    try:
        auth_url, code_verifier = oidc_service.build_authorization_url(state)
    except OidcNotConfigured:
        return _login_error_redirect("sso_not_configured")
    except OidcLoginError as exc:
        logger.warning("Keycloak SSO login init failed: %s", exc)
        return _login_error_redirect("keycloak_login_failed")

    from fastapi.responses import RedirectResponse

    redirect = RedirectResponse(auth_url, status_code=302)
    # httpOnly + Secure(prod) + SameSite=Lax, same posture as the refresh
    # token cookie (app.api.auth) -- see oidc_service.py's docstring for
    # why the verifier lives here rather than inside `state`. 10-minute
    # expiry matches create_sso_state_token's own window: if the user
    # takes longer than that at Keycloak's login page, both expire
    # together rather than the verifier outliving/underliving the state.
    redirect.set_cookie(
        key=oidc_service.PKCE_COOKIE_NAME,
        value=code_verifier,
        httponly=True,
        secure=settings.ENVIRONMENT == "production",
        samesite="lax",
        path=oidc_service.PKCE_COOKIE_PATH,
        max_age=600,
    )
    return redirect


@router.get("/callback")
def keycloak_callback(
    request: Request,
    response: Response,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    db: Session = Depends(get_db),
    pkce_verifier: str | None = Cookie(default=None, alias=oidc_service.PKCE_COOKIE_NAME),
):
    from app.api.auth import _issue_token_pair

    if error:
        return _login_error_redirect(error)
    if not code or not state:
        return _login_error_redirect("missing_code_or_state")
    if not pkce_verifier:
        # Cookie missing/expired -- e.g. the user took the redirect URL
        # to a different browser/device, or it genuinely expired. Fail
        # closed: there is no safe way to complete PKCE without it.
        return _login_error_redirect("missing_pkce_verifier")

    try:
        state_claims = decode_sso_state_token(state)
    except JWTError:
        return _login_error_redirect("invalid_or_expired_state")

    try:
        claims = oidc_service.exchange_code_for_claims(code, pkce_verifier)
    except OidcNotConfigured:
        return _login_error_redirect("sso_not_configured")
    except OidcLoginError as exc:
        logger.warning("Keycloak SSO login failed: %s", exc)
        return _login_error_redirect("keycloak_login_failed")

    user = oidc_service.find_or_create_user(db, claims=claims)
    if user.is_active in (False, "false", "False"):
        return _login_error_redirect("account_disabled")

    token = _issue_token_pair(db, user, response, request)

    next_path = state_claims.get("next") or "/"
    redirect_url = f"{settings.FRONTEND_URL.rstrip('/')}{next_path}#access_token={token.access_token}"

    from fastapi.responses import RedirectResponse

    redirect = RedirectResponse(redirect_url, status_code=302)
    for header, value in response.headers.items():
        if header.lower() == "set-cookie":
            redirect.headers.append(header, value)
    redirect.delete_cookie(key=oidc_service.PKCE_COOKIE_NAME, path=oidc_service.PKCE_COOKIE_PATH)
    return redirect


@router.get("/discovery-health")
def keycloak_discovery_health():
    """Lightweight readiness check the frontend (or a deploy script) can
    poll to confirm NetGuard can currently reach Keycloak's discovery
    endpoint and fetch its JWKS -- separate from /sso/providers below so
    it can be probed without implying "someone is trying to log in"."""
    if not settings.OIDC_ISSUER:
        return {"configured": False}
    try:
        oidc_service._discovery_document()
        oidc_service._jwks()
        return {"configured": True, "reachable": True}
    except OidcLoginError as exc:
        return {"configured": True, "reachable": False, "error": str(exc)}
