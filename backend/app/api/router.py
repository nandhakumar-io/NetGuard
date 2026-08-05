from fastapi import APIRouter

from app.api import (
    auth,
    devices,
    change_requests,
    audit,
    dashboard,
    deployments,
    config_management,
    config_search,
    drift,
    metrics,
    alerts,
    alert_rules,
    gns3,
    notification,
    reports,
    topology,
    terminal,
    compliance_baselines,
    config_templates,
    syslog,
    path_trace,
    maintenance_windows,
    firmware_upgrades,
    flows,
    webhooks,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(devices.router)
api_router.include_router(config_management.router)
api_router.include_router(config_search.router)
api_router.include_router(change_requests.router)
api_router.include_router(audit.router)
api_router.include_router(dashboard.router)
api_router.include_router(deployments.router)
api_router.include_router(drift.router)
api_router.include_router(metrics.router)
api_router.include_router(alerts.router)
api_router.include_router(alert_rules.router)
api_router.include_router(gns3.router)
api_router.include_router(notification.router)
api_router.include_router(reports.router)
api_router.include_router(topology.router)
api_router.include_router(terminal.router)
api_router.include_router(compliance_baselines.router)
api_router.include_router(config_templates.router)
api_router.include_router(syslog.router)
api_router.include_router(path_trace.router)
api_router.include_router(maintenance_windows.router)
api_router.include_router(firmware_upgrades.router)
api_router.include_router(flows.router)
api_router.include_router(webhooks.router)