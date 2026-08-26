"""Tenant management API -- create/list/edit/deactivate managed tenants.

Restricted to MSP staff (require_msp_staff) -- a customer-side user has
no business managing *other* tenants, and the cross-tenant board
(app.api.tenant_board) already shows them read-only. Regular users can
GET /tenants/public-list (name + id only) to populate the registration
form's tenant dropdown without exposing operational details.
"""
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_msp_staff
from app.models.device import Device
from app.models.tenant import Tenant
from app.models.user import User

router = APIRouter(prefix="/tenants", tags=["tenants"])

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}[a-z0-9]$|^[a-z0-9]$")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TenantCreate(BaseModel):
    name: str
    slug: str

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        v = v.lower().strip()
        if not _SLUG_RE.match(v):
            raise ValueError("Slug must be lowercase alphanumeric with optional hyphens, 1–64 chars")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name must not be empty")
        return v


class TenantUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("Name must not be empty")
        return v


class TenantRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    is_active: bool
    device_count: int = 0
    user_count: int = 0

    model_config = {"from_attributes": True}


class TenantPublic(BaseModel):
    """Minimal shape for use in the registration form dropdown --
    any authenticated user (including non-MSP staff trying to register
    a teammate) can read this list."""
    id: uuid.UUID
    name: str
    slug: str

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tenant_read(db: Session, tenant: Tenant) -> TenantRead:
    device_count = db.query(Device).filter(Device.tenant_id == tenant.id).count()
    user_count = db.query(User).filter(User.tenant_id == tenant.id).count()
    return TenantRead(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        is_active=bool(tenant.is_active),
        device_count=device_count,
        user_count=user_count,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/public-list", response_model=list[TenantPublic])
def list_tenants_public(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[TenantPublic]:
    """Active tenants: id, name, slug only. Any authenticated user can
    call this so the registration form dropdown works for non-MSP staff
    inviting a teammate to their own tenant.
    """
    tenants = (
        db.query(Tenant)
        .filter(Tenant.is_active.is_(True))
        .order_by(Tenant.name)
        .all()
    )
    return [TenantPublic(id=t.id, name=t.name, slug=t.slug) for t in tenants]


@router.get("", response_model=list[TenantRead])
def list_tenants(
    db: Session = Depends(get_db),
    _: User = Depends(require_msp_staff),
) -> list[TenantRead]:
    """Full tenant list with device/user counts. MSP staff only."""
    tenants = db.query(Tenant).order_by(Tenant.name).all()
    return [_tenant_read(db, t) for t in tenants]


@router.post("", response_model=TenantRead, status_code=status.HTTP_201_CREATED)
def create_tenant(
    payload: TenantCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_msp_staff),
) -> TenantRead:
    """Create a new managed tenant. MSP staff only."""
    existing = db.query(Tenant).filter(Tenant.slug == payload.slug).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"A tenant with slug '{payload.slug}' already exists")

    tenant = Tenant(name=payload.name, slug=payload.slug)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return _tenant_read(db, tenant)


@router.patch("/{tenant_id}", response_model=TenantRead)
def update_tenant(
    tenant_id: uuid.UUID,
    payload: TenantUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_msp_staff),
) -> TenantRead:
    """Rename a tenant or toggle its active status. MSP staff only."""
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    if payload.name is not None:
        tenant.name = payload.name
    if payload.is_active is not None:
        tenant.is_active = payload.is_active

    db.commit()
    db.refresh(tenant)
    return _tenant_read(db, tenant)


@router.delete("/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tenant(
    tenant_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_msp_staff),
) -> None:
    """Hard-delete a tenant. Guarded: refuses if the tenant still has
    devices or users assigned (deactivate instead if you want a soft remove).
    MSP staff only.
    """
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    device_count = db.query(Device).filter(Device.tenant_id == tenant_id).count()
    user_count = db.query(User).filter(User.tenant_id == tenant_id).count()
    if device_count or user_count:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete tenant '{tenant.name}': still has "
                f"{device_count} device(s) and {user_count} user(s) assigned. "
                "Reassign or delete them first, or use PATCH to deactivate instead."
            ),
        )

    db.delete(tenant)
    db.commit()
