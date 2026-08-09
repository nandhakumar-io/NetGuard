import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models.device import Device
from app.models.device_group import DeviceGroup
from app.models.user import UserRole
from app.schemas.device import DeviceRead
from app.schemas.device_group import (
    DeviceGroupAssignRequest,
    DeviceGroupCreate,
    DeviceGroupRead,
    DeviceGroupRuleApplyResult,
    DeviceGroupRuleMatch,
    DeviceGroupRulePreview,
    DeviceGroupUpdate,
    GroupHealthRollup,
)
from app.services import group_membership_service

router = APIRouter(prefix="/device-groups", tags=["device-groups"])

# Same rationale as devices.INVENTORY_MANAGER_ROLES -- group structure is
# inventory, everyone authenticated can view it, only Network Admins
# restructure it.
GROUP_MANAGER_ROLES = require_roles(UserRole.NETWORK_ADMIN)


def _rules_to_json(rules) -> str | None:
    if not rules:
        return None
    return json.dumps([r.model_dump() if hasattr(r, "model_dump") else r for r in rules])


def _to_read(db: Session, group: DeviceGroup) -> DeviceGroupRead:
    device_count = db.query(func.count(Device.id)).filter(Device.group_id == group.id).scalar() or 0
    child_group_count = (
        db.query(func.count(DeviceGroup.id)).filter(DeviceGroup.parent_group_id == group.id).scalar() or 0
    )
    return DeviceGroupRead(
        id=group.id,
        name=group.name,
        description=group.description,
        group_type=group.group_type,
        parent_group_id=group.parent_group_id,
        is_dynamic=group.is_dynamic,
        membership_rules=group_membership_service.parse_rules(group),
        created_at=group.created_at,
        updated_at=group.updated_at,
        device_count=device_count,
        child_group_count=child_group_count,
    )


@router.get("", response_model=list[DeviceGroupRead])
def list_groups(db: Session = Depends(get_db), _=Depends(get_current_user)):
    groups = db.query(DeviceGroup).order_by(DeviceGroup.group_type, DeviceGroup.name).all()
    return [_to_read(db, g) for g in groups]


@router.post("", response_model=DeviceGroupRead, status_code=201)
def create_group(payload: DeviceGroupCreate, db: Session = Depends(get_db), _=Depends(GROUP_MANAGER_ROLES)):
    if payload.parent_group_id is not None and not db.get(DeviceGroup, payload.parent_group_id):
        raise HTTPException(status_code=400, detail="Parent group not found")
    data = payload.model_dump()
    data["membership_rules"] = _rules_to_json(payload.membership_rules)
    group = DeviceGroup(**data)
    db.add(group)
    db.commit()
    db.refresh(group)
    return _to_read(db, group)


@router.get("/{group_id}", response_model=DeviceGroupRead)
def get_group(group_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    group = db.get(DeviceGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return _to_read(db, group)


@router.patch("/{group_id}", response_model=DeviceGroupRead)
def update_group(
    group_id: uuid.UUID, payload: DeviceGroupUpdate, db: Session = Depends(get_db), _=Depends(GROUP_MANAGER_ROLES)
):
    group = db.get(DeviceGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    updates = payload.model_dump(exclude_unset=True)
    if "parent_group_id" in updates and updates["parent_group_id"] is not None:
        if updates["parent_group_id"] == group_id:
            raise HTTPException(status_code=400, detail="A group can't be its own parent")
        if not db.get(DeviceGroup, updates["parent_group_id"]):
            raise HTTPException(status_code=400, detail="Parent group not found")
        # Guard against creating a cycle (setting an ancestor's parent to
        # one of its own descendants) -- walk up from the proposed parent
        # and reject if we hit this group.
        cursor = db.get(DeviceGroup, updates["parent_group_id"])
        seen = set()
        while cursor is not None and cursor.parent_group_id is not None:
            if cursor.id in seen:
                break  # already-corrupt data elsewhere; don't loop forever
            seen.add(cursor.id)
            if cursor.parent_group_id == group_id:
                raise HTTPException(status_code=400, detail="That would create a group cycle")
            cursor = db.get(DeviceGroup, cursor.parent_group_id)

    if "membership_rules" in updates:
        updates["membership_rules"] = _rules_to_json(payload.membership_rules)

    for field, value in updates.items():
        setattr(group, field, value)

    db.commit()
    db.refresh(group)
    return _to_read(db, group)


@router.delete("/{group_id}", status_code=204)
def delete_group(
    group_id: uuid.UUID,
    reassign_children_to_parent: bool = True,
    db: Session = Depends(get_db),
    _=Depends(GROUP_MANAGER_ROLES),
):
    """Deletes a group. Member devices are unassigned (group_id -> NULL),
    same as the DB-level ON DELETE SET NULL. Child groups are, by
    default, re-parented up to this group's own parent (so deleting a
    rack under a datacenter just flattens that one level instead of
    orphaning everything under it); pass reassign_children_to_parent=false
    to leave them as top-level groups instead.
    """
    group = db.get(DeviceGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    children = db.query(DeviceGroup).filter(DeviceGroup.parent_group_id == group_id).all()
    for child in children:
        child.parent_group_id = group.parent_group_id if reassign_children_to_parent else None

    db.query(Device).filter(Device.group_id == group_id).update({"group_id": None}, synchronize_session=False)
    db.delete(group)
    db.commit()


@router.get("/{group_id}/devices", response_model=list[DeviceRead])
def list_group_devices(
    group_id: uuid.UUID,
    include_descendants: bool = False,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    group = db.get(DeviceGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    group_ids = {group_id}
    if include_descendants:
        # BFS down the parent_group_id tree.
        frontier = [group_id]
        while frontier:
            children = db.query(DeviceGroup.id).filter(DeviceGroup.parent_group_id.in_(frontier)).all()
            frontier = [c.id for c in children if c.id not in group_ids]
            group_ids.update(frontier)

    devices = db.query(Device).filter(Device.group_id.in_(group_ids)).order_by(Device.hostname).all()
    return [DeviceRead.from_device(d) for d in devices]


@router.post("/{group_id}/devices", response_model=list[DeviceRead])
def assign_devices(
    group_id: uuid.UUID,
    payload: DeviceGroupAssignRequest,
    db: Session = Depends(get_db),
    _=Depends(GROUP_MANAGER_ROLES),
):
    group = db.get(DeviceGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    devices = db.query(Device).filter(Device.id.in_(payload.device_ids)).all()
    found_ids = {d.id for d in devices}
    missing = set(payload.device_ids) - found_ids
    if missing:
        raise HTTPException(status_code=404, detail=f"Device(s) not found: {', '.join(str(m) for m in missing)}")

    for device in devices:
        device.group_id = group_id
    db.commit()
    return [DeviceRead.from_device(d) for d in devices]


@router.delete("/{group_id}/devices/{device_id}", response_model=DeviceRead)
def remove_device(
    group_id: uuid.UUID, device_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(GROUP_MANAGER_ROLES)
):
    device = db.get(Device, device_id)
    if not device or device.group_id != group_id:
        raise HTTPException(status_code=404, detail="Device not found in this group")
    device.group_id = None
    db.commit()
    db.refresh(device)
    return DeviceRead.from_device(device)


@router.get("/{group_id}/rules/preview", response_model=DeviceGroupRulePreview)
def preview_group_rules(group_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Shows which devices a group's `membership_rules` currently match,
    without writing anything -- lets an admin sanity-check a pattern
    (e.g. "edge-*") before committing to it via POST .../rules/apply.
    """
    group = db.get(DeviceGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    matches = group_membership_service.preview_matches(db, group)
    return DeviceGroupRulePreview(
        matches=[
            DeviceGroupRuleMatch(
                device_id=m.device.id,
                hostname=m.device.hostname,
                matched_rule=m.matched_rule,
                already_member=m.already_member,
            )
            for m in matches
        ]
    )


@router.post("/{group_id}/rules/apply", response_model=DeviceGroupRuleApplyResult)
def apply_group_rules(
    group_id: uuid.UUID, db: Session = Depends(get_db), current_user=Depends(GROUP_MANAGER_ROLES)
):
    """Assigns every device currently matching this group's
    `membership_rules` into the group (Device.group_id = this group).
    Devices that no longer match an existing rule are NOT removed --
    same growth-only semantics as manual assignment; use the normal
    remove-device / reassign endpoints to take a device back out.
    """
    from app.services import audit_service

    group = db.get(DeviceGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if not group.membership_rules:
        raise HTTPException(status_code=400, detail="This group has no membership rules configured")

    assigned, already_member = group_membership_service.apply_rules(db, group)
    if assigned:
        audit_service.record_event(
            db,
            actor=current_user.email,
            action="Applied group rules",
            result="Success",
            detail=f"Group '{group.name}': {len(assigned)} device(s) newly assigned by rule",
        )
    return DeviceGroupRuleApplyResult(assigned_device_ids=assigned, already_member_device_ids=already_member)


@router.get("/{group_id}/health-rollup", response_model=GroupHealthRollup)
def group_health_rollup(
    group_id: uuid.UUID,
    include_descendants: bool = False,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Health rollup (green/yellow/red/gray counts + avg/worst score) for
    a named group -- the same rollup the rack/data-center view already
    computes, extended to explicit DeviceGroups.
    """
    group = db.get(DeviceGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return group_membership_service.health_rollup(db, group, include_descendants=include_descendants)
