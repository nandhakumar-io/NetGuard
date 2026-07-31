from fastapi import APIRouter

from app.api import (
    auth,
    devices,
    change_requests,
    audit,
    dashboard,
    deployments,
    config_management,
    drift,
    metrics,
    alerts,
    gns3,
    notification,
    reports,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(devices.router)
api_router.include_router(config_management.router)
api_router.include_router(change_requests.router)
api_router.include_router(audit.router)
api_router.include_router(dashboard.router)
api_router.include_router(deployments.router)
api_router.include_router(drift.router)
api_router.include_router(metrics.router)
api_router.include_router(alerts.router)
api_router.include_router(gns3.router)
api_router.include_router(notification.router)
api_router.include_router(reports.router)