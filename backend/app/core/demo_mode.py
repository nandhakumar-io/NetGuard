"""Demo Mode gate: when settings.DEMO_MODE is true, every mutating HTTP
request (POST/PUT/PATCH/DELETE) is rejected before it reaches a route
handler, except for a short allowlist of auth endpoints needed to
actually use the demo.

Deliberately implemented as ASGI middleware rather than a per-route
FastAPI dependency: this app has 30+ route modules and well over a
hundred mutating endpoints (see app/api/router.py), and a dependency has
to be remembered on every single one of them, forever, including every
new endpoint added after today -- one missed `Depends(...)` is a real
mutation slipping through in what's supposed to be a locked-down public
demo. A single middleware ahead of routing can't be forgotten.

What's allowed through even in demo mode:
  - Every GET/HEAD/OPTIONS request (the whole read side of the app).
  - POST /api/v1/auth/login, /auth/refresh, /auth/logout, and
    /auth/mfa/verify -- without these nobody can actually sign in and
    look around. Deliberately NOT /auth/register (don't want the public
    creating real accounts) and NOT /auth/mfa/setup|enable|disable or
    PATCH /auth/users/{id}/role (those mutate the demo account itself).
  - DELETE /api/v1/auth/sessions/{id} and POST /auth/logout are both
    session-hygiene, not data mutation, and are covered by the same
    prefix allowlist below.

Everything else matching a mutating method gets a 403 with a message
the frontend already knows how to show -- every page in this app reads
`err.response.data.detail` on a failed request (see e.g.
frontend/src/pages/Security.tsx), so no frontend changes were needed for
this to surface cleanly as an inline error instead of a silent failure
or a broken UI.
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import settings

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Exact path suffixes (after the API_V1_PREFIX) allowed through even
# though they're mutating -- see module docstring for why each is here.
_ALLOWED_MUTATING_PATHS = {
    "/auth/login",
    "/auth/refresh",
    "/auth/logout",
    "/auth/mfa/verify",
}


def _is_allowed_session_revoke(path: str, method: str) -> bool:
    """DELETE /auth/sessions/{id} -- own-session revoke, not a data
    mutation. Path has a variable segment so it's matched by prefix
    rather than being listable in _ALLOWED_MUTATING_PATHS."""
    return method == "DELETE" and path.startswith("/auth/sessions/")


class DemoModeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not settings.DEMO_MODE:
            return await call_next(request)

        if request.method in _SAFE_METHODS:
            return await call_next(request)

        path = request.url.path
        if path.startswith(settings.API_V1_PREFIX):
            path = path[len(settings.API_V1_PREFIX):]

        if path in _ALLOWED_MUTATING_PATHS or _is_allowed_session_revoke(path, request.method):
            return await call_next(request)

        return JSONResponse(
            status_code=403,
            content={
                "detail": "This is a read-only public demo -- creating, editing, and deleting is disabled. "
                "Deploy your own instance to try mutating actions.",
                "demo_mode": True,
            },
        )
