"""Dynamic DeviceGroup membership (auto-add by hostname/tag/site/... glob
pattern) and named-group health rollups.

Rules are stored on DeviceGroup.membership_rules as JSON text (see
app.schemas.device_group.DeviceGroupRule) and evaluated on demand --
there's no background job that keeps membership continuously in sync.
Call `apply_rules` after adding/editing rules, after a bulk import, or on
whatever cadence a caller wants (e.g. a scheduled task could call this
for every is_dynamic=true group, but nothing does so out of the box).

Matching a rule assigns Device.group_id directly, same column manual
assignment (POST /device-groups/{id}/devices) uses -- there is no
separate "virtual/computed membership" concept to keep in sync, so
health rollups, bulk actions, and the existing group_id-based device
listing all keep working unmodified for dynamic groups too.
"""
from __future__ import annotations

import fnmatch
import json
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.device import Device
from app.models.device_group import DeviceGroup
from app.models.device_metric import DeviceMetric, HealthColor

RULE_FIELDS = ("hostname", "tag", "site", "device_type", "device_role")


def _device_field_values(device: Device, field: str) -> list[str]:
    if field == "hostname":
        return [device.hostname or ""]
    if field == "site":
        return [device.site or ""]
    if field == "device_type":
        return [device.device_type or ""]
    if field == "device_role":
        return [device.device_role or ""]
    if field == "tag":
        raw = getattr(device, "tags", None)
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(t) for t in parsed]
        except (ValueError, TypeError):
            pass
        return []
    return []


def device_matches_rule(device: Device, rule: dict) -> bool:
    field = (rule or {}).get("field")
    pattern = (rule or {}).get("pattern")
    if not field or not pattern or field not in RULE_FIELDS:
        return False
    values = _device_field_values(device, field)
    pattern_lower = pattern.lower()
    return any(fnmatch.fnmatch(v.lower(), pattern_lower) for v in values if v)


def parse_rules(group: DeviceGroup) -> list[dict]:
    if not group.membership_rules:
        return []
    try:
        parsed = json.loads(group.membership_rules)
        if isinstance(parsed, list):
            return [r for r in parsed if isinstance(r, dict)]
    except (ValueError, TypeError):
        pass
    return []


def device_matches_any_rule(device: Device, rules: list[dict]) -> dict | None:
    """Returns the first matching rule dict, or None."""
    for rule in rules:
        if device_matches_rule(device, rule):
            return rule
    return None


@dataclass
class RuleMatch:
    device: Device
    matched_rule: dict
    already_member: bool


def preview_matches(db: Session, group: DeviceGroup) -> list[RuleMatch]:
    rules = parse_rules(group)
    if not rules:
        return []
    results: list[RuleMatch] = []
    for device in db.query(Device).all():
        rule = device_matches_any_rule(device, rules)
        if rule is not None:
            results.append(RuleMatch(device=device, matched_rule=rule, already_member=device.group_id == group.id))
    return results


def apply_rules(db: Session, group: DeviceGroup) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Assigns every currently-matching device's group_id to this group.
    Returns (newly_assigned_ids, already_member_ids). Does NOT remove
    devices that no longer match -- membership only grows via rules,
    same as manual assignment; use the normal remove-device endpoint (or
    reassign to another group) to take a device back out.
    """
    matches = preview_matches(db, group)
    newly_assigned: list[uuid.UUID] = []
    already_member: list[uuid.UUID] = []
    for m in matches:
        if m.already_member:
            already_member.append(m.device.id)
        else:
            m.device.group_id = group.id
            newly_assigned.append(m.device.id)
    if newly_assigned:
        db.commit()
    return newly_assigned, already_member


def _descendant_group_ids(db: Session, group_id: uuid.UUID) -> set[uuid.UUID]:
    group_ids = {group_id}
    frontier = [group_id]
    while frontier:
        children = db.query(DeviceGroup.id).filter(DeviceGroup.parent_group_id.in_(frontier)).all()
        frontier = [c.id for c in children if c.id not in group_ids]
        group_ids.update(frontier)
    return group_ids


def health_rollup(db: Session, group: DeviceGroup, include_descendants: bool = False) -> dict:
    group_ids = _descendant_group_ids(db, group.id) if include_descendants else {group.id}
    devices = db.query(Device).filter(Device.group_id.in_(group_ids)).all()

    counts = {"green": 0, "yellow": 0, "red": 0, "gray": 0}
    scores: list[int] = []
    unmonitored = 0
    worst_score: int | None = None
    worst_hostname: str | None = None

    if devices:
        device_ids = [d.id for d in devices]
        # Latest metric row per device -- same "most recent DeviceMetric"
        # pattern used elsewhere (see topology_service._latest_metrics_by_device),
        # reimplemented locally to avoid a circular import.
        rows = (
            db.query(DeviceMetric)
            .filter(DeviceMetric.device_id.in_(device_ids))
            .order_by(DeviceMetric.device_id, DeviceMetric.polled_at.desc())
            .all()
        )
        latest_by_device: dict[uuid.UUID, DeviceMetric] = {}
        for row in rows:
            if row.device_id not in latest_by_device:
                latest_by_device[row.device_id] = row

        hostnames = {d.id: d.hostname for d in devices}
        for device_id in device_ids:
            metric = latest_by_device.get(device_id)
            if metric is None or metric.health_color is None:
                unmonitored += 1
                continue
            color = metric.health_color.value if isinstance(metric.health_color, HealthColor) else metric.health_color
            if color in counts:
                counts[color] += 1
            if metric.health_score is not None:
                scores.append(metric.health_score)
                if worst_score is None or metric.health_score < worst_score:
                    worst_score = metric.health_score
                    worst_hostname = hostnames.get(device_id)

    return {
        "group_id": group.id,
        "group_name": group.name,
        "include_descendants": include_descendants,
        "device_count": len(devices),
        "unmonitored_count": unmonitored,
        "green_count": counts["green"],
        "yellow_count": counts["yellow"],
        "red_count": counts["red"],
        "gray_count": counts["gray"],
        "average_health_score": (sum(scores) / len(scores)) if scores else None,
        "worst_health_score": worst_score,
        "worst_device_hostname": worst_hostname,
    }
