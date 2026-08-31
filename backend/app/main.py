"""NetGuard FastAPI application entrypoint.

Wires together the ~57 route modules aggregated in app.api.router,
CORS, the Demo Mode gate, and startup-time production-secrets
validation (Section 8 / Section 16 of the hardening spec: this process
is the one assumed-compromisable component, so it deliberately holds
no Keycloak admin credentials, no OpenBao root token, and -- once
DEVICE_GATEWAY_ENABLED is true, the default -- no device-management
network path and no device-credential decryption key; see
app/device_gateway/main.py, which runs as its own separate container).

Run via `uvicorn app.main:app` (see backend/Dockerfile's CMD and
entrypoint.sh, which applies Alembic migrations first).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.core.demo_mode import DemoModeMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("netguard.api")

# Threat model T17 (Denial of service): no application-level rate limiting
# existed anywhere in the API prior to this -- a single client (including,
# on the local-auth login endpoint, an unauthenticated one) could send
# unlimited requests. Per-client-IP default; individual routers can layer
# tighter limits (e.g. login/MFA) via the same `limiter` instance.
limiter = Limiter(key_func=get_remote_address, default_limits=[settings.RATE_LIMIT_DEFAULT])

# Threat model T17: no request body size cap existed either. Rejects
# oversized bodies before they reach route handlers/JSON parsing.
_MAX_BODY_BYTES = settings.MAX_REQUEST_BODY_BYTES


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > _MAX_BODY_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "Request body too large"},
                    )
            except ValueError:
                pass
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Defense-in-depth backstop named explicitly in threat model T2: even
    with React's default JSX escaping, an XSS that lands should not have a
    free ride. Conservative defaults; loosen CSP per-route if a legitimate
    need (e.g. embedding Grafana) requires it."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'self'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail closed rather than boot with dev-default secrets in production
    # (see Settings.validate_production_secrets' own docstring for why).
    settings.validate_production_secrets()
    logger.info(
        "NetGuard API starting: environment=%s device_gateway_enabled=%s",
        settings.ENVIRONMENT, settings.DEVICE_GATEWAY_ENABLED,
    )
    yield
    logger.info("NetGuard API shutting down")


app = FastAPI(
    title=settings.APP_NAME,
    lifespan=lifespan,
    # Root-level docs stay on for now (auth is still required on every
    # data endpoint); flip to None in production if the spec's threat
    # model is later extended to treat schema disclosure itself as
    # sensitive.
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

# CORS: exact origin allow-list only, never "*" -- see
# Settings.CORS_ALLOWED_ORIGINS' own comment for why "*" combined with
# allow_credentials=True is a credential-theft CSRF hole, not just a
# lint warning.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ahead of routing so no new endpoint can ever forget to check Demo
# Mode -- see app/core/demo_mode.py's module docstring.
app.add_middleware(DemoModeMiddleware)

app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/health", tags=["health"])
async def liveness() -> dict[str, str]:
    """Unauthenticated liveness probe for the load balancer/orchestrator
    -- answers "is the process up" in ~0ms. Deliberately separate from
    the authenticated, dependency-dialing GET /api/v1/health/detailed
    in app.api.health, which can take a couple of seconds and must not
    be on the hot path Traefik/Docker healthchecks hit every few
    seconds."""
    return {"status": "ok"}
