from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.api.router import api_router
from app import models  # noqa: F401  ensures models are registered on Base.metadata

app = FastAPI(
    title=settings.APP_NAME,
    description="Intelligent Automated Network Change Management & Self-Healing Platform",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.on_event("startup")
def on_startup():
    # Schema is now owned by Alembic migrations (see backend/alembic/),
    # not this startup hook. `Base.metadata.create_all` used to run here
    # as a "prototype convenience", but it only ever creates missing
    # *tables* -- it silently never adds a column to a table that already
    # exists, which is exactly how `users.mfa_secret` ended up missing in
    # production after the model gained that column. Run
    # `alembic upgrade head` before starting the app instead (the Docker
    # image's entrypoint.sh does this automatically).
    pass


@app.get("/")
def root():
    return {"app": settings.APP_NAME, "status": "ok", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "healthy"}
