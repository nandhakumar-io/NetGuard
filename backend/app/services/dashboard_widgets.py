"""Canonical registry of dashboard widgets, and the merge logic that
turns a user's stored DashboardPreference.layout into a full ordered
layout the frontend can render directly.

The registry (not the DB) is the source of truth for which widget ids
exist, their default order, and whether they're visible out of the box
-- a DashboardPreference row only ever stores an *override* of that
default (which widgets a specific user hid, and what order they dragged
them into). This means:
  - Shipping a new widget is a one-line addition here, no migration.
  - A user's saved layout that predates a new widget doesn't need
    backfilling -- merge_layout appends anything missing from their
    saved list using the registry's default order/visibility.
  - Deleting a widget doesn't require cleaning up every user's saved
    layout -- merge_layout silently drops ids no longer in the registry.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class DashboardWidget:
    id: str
    title: str
    # Which /dashboard/summary field(s) this widget is driven by, for
    # reference/documentation -- not enforced at runtime.
    data_source: str
    default_visible: bool = True


# Order here is the default layout for a user who has never customized
# anything (DashboardPreference row doesn't exist yet).
DASHBOARD_WIDGETS: list[DashboardWidget] = [
    DashboardWidget("fleet_health", "Fleet Health", "devices_online/devices_total/global_health_score"),
    DashboardWidget("fleet_history_chart", "Fleet History (CPU/Memory/Bandwidth)", "fleet_health_history"),
    DashboardWidget("uplinks", "Uplinks & WAN Links", "uplinks"),
    DashboardWidget("active_alerts", "Active Alerts", "GET /alerts"),
    DashboardWidget("fleet_availability", "Fleet Availability", "GET /devices/fleet-availability"),
    DashboardWidget("top_flapping_devices", "Top Flapping Devices", "GET /devices/unstable"),
    DashboardWidget("offline_devices", "Offline / Degraded Devices", "offline_devices"),
    DashboardWidget("top_interface_errors", "Top Interface Errors", "top_error_devices"),
    DashboardWidget("flapping_interfaces", "Flapping Interfaces", "flapping_interfaces"),
    DashboardWidget("top_cpu_devices", "Top CPU Devices", "top_cpu_devices", default_visible=False),
    DashboardWidget("top_memory_devices", "Top Memory Devices", "top_memory_devices", default_visible=False),
    DashboardWidget("top_bandwidth_devices", "Top Bandwidth Devices", "top_bandwidth_devices", default_visible=False),
    DashboardWidget("down_ports", "Down Ports", "down_ports", default_visible=False),
    DashboardWidget("recent_reboots", "Recent Reboots", "recent_reboots", default_visible=False),
    DashboardWidget("recent_backups", "Recent Backups", "recent_backups", default_visible=False),
    DashboardWidget(
        "recent_protocol_operations", "Recent Protocol Operations", "recent_protocol_operations", default_visible=False
    ),
    DashboardWidget("group_availability", "Group Availability", "GET /device-groups"),
]

WIDGET_IDS = {w.id for w in DASHBOARD_WIDGETS}
_DEFAULT_ORDER = [w.id for w in DASHBOARD_WIDGETS]
_DEFAULTS_BY_ID = {w.id: w for w in DASHBOARD_WIDGETS}


def default_layout() -> list[dict]:
    return [{"id": w.id, "visible": w.default_visible} for w in DASHBOARD_WIDGETS]


def merge_layout(saved: list[dict]) -> list[dict]:
    """Reconciles a user's saved layout against the current registry:
    keeps the user's order/visibility for any id that's still a real
    widget, drops ids that no longer exist (a widget that was removed),
    and appends any registry widget the user's saved layout doesn't
    mention yet (a widget added after they last customized), in the
    registry's default order/visibility, at the end.
    """
    seen: set[str] = set()
    merged: list[dict] = []
    for entry in saved:
        wid = entry.get("id")
        if wid not in WIDGET_IDS or wid in seen:
            continue
        seen.add(wid)
        merged.append({"id": wid, "visible": bool(entry.get("visible", True))})
    for wid in _DEFAULT_ORDER:
        if wid not in seen:
            merged.append({"id": wid, "visible": _DEFAULTS_BY_ID[wid].default_visible})
    return merged
