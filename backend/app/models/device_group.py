import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class DeviceGroup(Base):
    """A named, user-managed device group -- distinct from the free-text
    data_center/rack fields on Device (see app.services.topology_service
    and the Groups page's "Data Center / Rack" view). Those two fields are
    physical-location grouping; DeviceGroup is logical grouping an admin
    defines explicitly ("Edge Firewalls", "Branch Routers - East Region",
    "Q3 Migration Batch"), with optional nesting via parent_group_id so a
    broad group can contain narrower sub-groups (e.g. "All Switches" ->
    "Core Switches" / "Access Switches").

    group_type is free-text like Device.device_type/device_role -- kept
    open-ended (e.g. "static", "site", "role", "project") rather than an
    enum since orgs organize fleets differently and this app doesn't
    enforce a taxonomy on any of its other grouping fields either.
    """

    __tablename__ = "device_groups"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False, index=True)
    description = Column(String, nullable=True)
    group_type = Column(String, nullable=False, default="static", server_default="static")
    parent_group_id = Column(UUID(as_uuid=True), ForeignKey("device_groups.id"), nullable=True, index=True)

    # --- Dynamic membership (auto-add by hostname/tag/site/... pattern) ---
    # When true, this group's membership is (also) computed by matching
    # `membership_rules` against every device, rather than being purely
    # explicit via POST /device-groups/{id}/devices. Devices matched by a
    # rule are assigned the same way manual assignment works today (i.e.
    # Device.group_id is actually set to this group) -- there's no
    # separate "virtual membership" list to keep in sync elsewhere, so
    # every other feature that already reads group_id (health rollups,
    # bulk actions, the Groups page) keeps working unmodified. Rules are
    # evaluated on demand via POST /device-groups/{id}/rules/apply
    # (and previewed without writing via .../rules/preview) rather than
    # on every device write, so a bulk CSV import of 500 devices doesn't
    # trigger 500 rule scans.
    is_dynamic = Column(Boolean, nullable=False, default=False, server_default="false")
    # JSON-encoded list of rule objects: [{"field": "hostname" | "tag" |
    # "site" | "device_type" | "device_role", "pattern": "edge-*"}, ...].
    # `pattern` is a glob (fnmatch) pattern, matched case-insensitively.
    # A device matches the group if it satisfies ANY rule (OR semantics)
    # -- see app.services.group_membership_service.device_matches_rule.
    membership_rules = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
