import datetime

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.deployment import Deployment, DeploymentStatus
from app.models.device import Device
from app.services import compliance_report

router = APIRouter(prefix="/reports", tags=["reports"])

# Deployment outcomes that count as "finished" for success-rate purposes --
# QUEUED/IN_PROGRESS are still in flight and shouldn't dilute the rate.
_FINISHED_STATUSES = [DeploymentStatus.SUCCEEDED, DeploymentStatus.FAILED, DeploymentStatus.ROLLED_BACK]


@router.get("/deployment-success-rate")
def get_deployment_success_rate(
    days: int = Query(30, ge=1, le=365, description="Trailing window in days"),
    group_by: str = Query("day", pattern="^(day|protocol|device)$"),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Change success rate over the trailing window: finished deployments
    (succeeded vs. failed/rolled-back) from the existing `deployments`
    table -- no new data collection needed, just an aggregation the
    dashboard's single point-in-time `deployment_success_rate` figure
    doesn't give you (see app.api.dashboard._compute_summary).

    `group_by=day` (default) returns a daily trend series suitable for a
    line/bar chart; `protocol` and `device` return a breakdown instead of
    a trend, e.g. to spot "ssh deploys fail more than netconf" or "this
    device is the one dragging the rate down".
    """
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    base = db.query(Deployment).filter(
        Deployment.created_at >= since,
        Deployment.status.in_(_FINISHED_STATUSES),
    )

    total_finished = base.count()
    total_succeeded = base.filter(Deployment.status == DeploymentStatus.SUCCEEDED).count()
    total_failed = base.filter(Deployment.status == DeploymentStatus.FAILED).count()
    total_rolled_back = base.filter(Deployment.status == DeploymentStatus.ROLLED_BACK).count()
    overall_rate = round((total_succeeded / total_finished * 100), 1) if total_finished else 100.0

    series: list[dict] = []

    if group_by == "day":
        bucket = func.date_trunc("day", Deployment.created_at)
        rows = (
            db.query(
                bucket.label("bucket"),
                func.count(Deployment.id).label("total"),
                func.sum(func.cast(Deployment.status == DeploymentStatus.SUCCEEDED, sa.Integer)).label("succeeded"),
                func.sum(func.cast(Deployment.status == DeploymentStatus.FAILED, sa.Integer)).label("failed"),
                func.sum(func.cast(Deployment.status == DeploymentStatus.ROLLED_BACK, sa.Integer)).label("rolled_back"),
            )
            .filter(Deployment.created_at >= since, Deployment.status.in_(_FINISHED_STATUSES))
            .group_by(bucket)
            .order_by(bucket)
            .all()
        )
        for r in rows:
            rate = round((r.succeeded / r.total * 100), 1) if r.total else 0.0
            series.append({
                "date": r.bucket.date().isoformat() if r.bucket else None,
                "total": r.total,
                "succeeded": int(r.succeeded or 0),
                "failed": int(r.failed or 0),
                "rolled_back": int(r.rolled_back or 0),
                "success_rate": rate,
            })

    elif group_by == "protocol":
        rows = (
            base.with_entities(
                Deployment.protocol,
                func.count(Deployment.id).label("total"),
                func.sum(func.cast(Deployment.status == DeploymentStatus.SUCCEEDED, sa.Integer)).label("succeeded"),
            )
            .group_by(Deployment.protocol)
            .order_by(Deployment.protocol)
            .all()
        )
        for protocol, total, succeeded in rows:
            rate = round((succeeded / total * 100), 1) if total else 0.0
            series.append({"protocol": protocol, "total": total, "succeeded": int(succeeded or 0), "success_rate": rate})

    else:  # group_by == "device"
        rows = (
            db.query(
                Device.hostname,
                func.count(Deployment.id).label("total"),
                func.sum(func.cast(Deployment.status == DeploymentStatus.SUCCEEDED, sa.Integer)).label("succeeded"),
            )
            .join(Device, Device.id == Deployment.device_id)
            .filter(Deployment.created_at >= since, Deployment.status.in_(_FINISHED_STATUSES))
            .group_by(Device.hostname)
            .order_by(Device.hostname)
            .all()
        )
        for hostname, total, succeeded in rows:
            rate = round((succeeded / total * 100), 1) if total else 0.0
            series.append({"hostname": hostname, "total": total, "succeeded": int(succeeded or 0), "success_rate": rate})

    return {
        "window_days": days,
        "group_by": group_by,
        "total_finished": total_finished,
        "total_succeeded": total_succeeded,
        "total_failed": total_failed,
        "total_rolled_back": total_rolled_back,
        "overall_success_rate": overall_rate,
        "series": series,
    }




_MEDIA_TYPES = {
    "csv": "text/csv",
    "pdf": "application/pdf",
}


@router.get("/compliance")
def get_compliance_report(
    format: str = Query("pdf", pattern="^(pdf|csv)$"),
    days: int = Query(30, ge=1, le=365, description="Reporting window in days"),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Fleet-wide compliance report (drift compliance scores, open drift
    counts, and AI Configuration Analyzer risk stats per device over the
    given window), rendered as a downloadable PDF or CSV.

    Available to any authenticated user (read-only, no device/config
    content is included) -- consistent with the other dashboard/summary
    endpoints in this API.
    """
    report = compliance_report.build_report(db, window_days=days)

    if format == "csv":
        body = compliance_report.render_csv(report)
    else:
        try:
            body = compliance_report.render_pdf(report)
        except ImportError:
            raise HTTPException(
                status_code=503,
                detail="PDF generation is unavailable: the 'reportlab' package is not installed on this server. "
                "Use format=csv instead, or install reportlab.",
            )

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    filename = f"netguard-compliance-report-{timestamp}.{format}"
    return Response(
        content=body,
        media_type=_MEDIA_TYPES[format],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
