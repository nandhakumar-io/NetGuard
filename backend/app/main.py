from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import Base, engine
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
    # Prototype convenience: create tables if they don't exist.
    # Replace with Alembic migrations for production use.
    Base.metadata.create_all(bind=engine)


@app.get("/")
def root():
    return {"app": settings.APP_NAME, "status": "ok", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "healthy"}
