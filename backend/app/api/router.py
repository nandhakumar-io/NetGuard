from fastapi import APIRouter

from app.api import auth, devices, change_requests, audit, dashboard, deployments, drift

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(devices.router)
api_router.include_router(change_requests.router)
api_router.include_router(audit.router)
api_router.include_router(dashboard.router)
api_router.include_router(deployments.router)
api_router.include_router(drift.router)
