import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field

# Valid Device fields a membership rule can match against. Kept as a
# plain literal list (not a Device-derived enum) since it's a small,
# intentionally curated subset -- not every Device column makes sense as
# a rule target.
RULE_FIELDS = ("hostname", "tag", "site", "device_type", "device_role")


class DeviceGroupRule(BaseModel):
    field: str = Field(description=f"One of: {', '.join(RULE_FIELDS)}")
    pattern: str = Field(description="fnmatch-style glob, e.g. 'edge-*' or '*-core-??'")


class DeviceGroupCreate(BaseModel):
    name: str
    description: str | None = None
    group_type: str = "static"
    parent_group_id: uuid.UUID | None = None
    is_dynamic: bool = False
    membership_rules: list[DeviceGroupRule] = Field(default_factory=list)


class DeviceGroupUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    group_type: str | None = None
    parent_group_id: uuid.UUID | None = None
    is_dynamic: bool | None = None
    membership_rules: list[DeviceGroupRule] | None = None


class DeviceGroupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None = None
    group_type: str
    parent_group_id: uuid.UUID | None = None
    is_dynamic: bool = False
    membership_rules: list[DeviceGroupRule] = Field(default_factory=list)
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None
    # Computed in app.api.device_groups._to_read, not a DB column.
    device_count: int = 0
    child_group_count: int = 0


class DeviceGroupAssignRequest(BaseModel):
    device_ids: list[uuid.UUID]


class DeviceGroupRuleMatch(BaseModel):
    device_id: uuid.UUID
    hostname: str
    matched_rule: DeviceGroupRule
    already_member: bool


class DeviceGroupRulePreview(BaseModel):
    matches: list[DeviceGroupRuleMatch] = Field(default_factory=list)


class DeviceGroupRuleApplyResult(BaseModel):
    assigned_device_ids: list[uuid.UUID] = Field(default_factory=list)
    already_member_device_ids: list[uuid.UUID] = Field(default_factory=list)


class GroupHealthRollup(BaseModel):
    group_id: uuid.UUID
    group_name: str
    include_descendants: bool
    device_count: int
    unmonitored_count: int  # no metric row / no status available
    green_count: int = 0
    yellow_count: int = 0
    red_count: int = 0
    gray_count: int = 0
    average_health_score: float | None = None
    worst_health_score: int | None = None
    worst_device_hostname: str | None = None
