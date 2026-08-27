"""POST /security/secrets/rotate -- re-encrypts every stored secret
(SSH/SNMP credentials, stored config text) under the current primary
SECRET_ENCRYPTION_KEY. See app.services.secrets_rotation_service and the
key-rotation procedure documented in app.core.crypto's module docstring.

Restricted to NETWORK_ADMIN and SECURITY -- this touches every device's
SSH password/private key and SNMP credentials in one pass, so it's held
to the same bar as credential entry itself, not just general admin
actions.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.user import User, UserRole
from app.services import audit_service
from app.services.secrets_rotation_service import rotate_all_secrets

router = APIRouter(prefix="/security/secrets", tags=["secrets-rotation"])

ROTATION_ROLES = require_roles(UserRole.NETWORK_ADMIN, UserRole.SECURITY)


@router.get("/rotation-status")
def rotation_status(current_user: User = Depends(get_current_user)):
    """Lightweight status check for the Security page -- how many keys
    are currently configured (2+ means an old key is still kept around
    as a fallback, which is expected mid-rotation and worth flagging if
    it lingers)."""
    return {"active_key_count": crypto.active_key_count()}


@router.post("/rotate")
def rotate_secrets(
    db: Session = Depends(get_db),
    current_user: User = Depends(ROTATION_ROLES),
):
    """Runs the rotation job synchronously and returns a per-table
    summary. Synchronous (not a Celery task) on purpose: the operator
    needs to see success/failure counts immediately to decide whether
    it's safe to drop the old key from config next -- a fire-and-forget
    background job would need its own polling/notification path for a
    security-critical action that should already be reasonably fast
    (one pass over a handful of tables, no external calls)."""
    summary = rotate_all_secrets(db)
    result_dict = summary.as_dict()

    audit_service.record_event(
        db,
        actor=current_user.email, tenant_id=current_user.tenant_id,
        action="secrets.rotate",
        result="failed" if summary.total_failed else "success",
        detail=(
            f"Rotated {summary.total_rotated} secret(s) to the current primary key; "
            f"{summary.total_failed} row(s) could not be rotated (see failed_ids); "
            f"{summary.key_count} key(s) currently configured."
        ),
    )

    return result_dict
