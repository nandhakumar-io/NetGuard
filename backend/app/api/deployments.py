import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.deployment import Deployment, HealthCheckResult
from app.models.snapshot import ConfigSnapshot

router = APIRouter(prefix="/deployments", tags=["deployments"])


def _serialize(d: Deployment, db: Session) -> dict:
    checks = db.query(HealthCheckResult).filter(HealthCheckResult.deployment_id == d.id).all()
    return {
        "id": str(d.id),
        "change_request_id": str(d.change_request_id),
        "device_id": str(d.device_id),
        "snapshot_id": str(d.snapshot_id) if d.snapshot_id else None,
        "protocol": d.protocol,
        "status": d.status.value,
        "error_message": d.error_message,
        "created_at": d.created_at,
        "health_checks": [
            {
                "category": c.category,
                "check_name": c.check_name,
                "passed": c.passed == "true",
                "detail": c.detail,
                "checked_at": c.checked_at,
            }
            for c in checks
        ],
    }


@router.get("")
def list_deployments(change_request_id: uuid.UUID | None = None, db: Session = Depends(get_db), _=Depends(get_current_user)):
    q = db.query(Deployment).order_by(Deployment.created_at.desc())
    if change_request_id:
        q = q.filter(Deployment.change_request_id == change_request_id)
    return [_serialize(d, db) for d in q.all()]


@router.get("/{deployment_id}")
def get_deployment(deployment_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    d = db.get(Deployment, deployment_id)
    if not d:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return _serialize(d, db)


@router.get("/snapshots/{snapshot_id}/checksum")
def get_snapshot_checksum(snapshot_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Expose checksum/version only -- never the encrypted config contents."""
    snap = db.get(ConfigSnapshot, snapshot_id)
    if not snap:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return {"id": str(snap.id), "checksum": snap.checksum, "version": snap.version, "created_at": snap.created_at}
