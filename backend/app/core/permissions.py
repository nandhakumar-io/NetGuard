"""Fine-grained, additive permission grants an admin can hand a specific
user on top of their base role -- complements (doesn't replace)
`User.extra_roles` / `require_roles` in app.core.deps, which grants a
*whole other role's* surface. That's a coarse instrument: it was the only
option even for, say, a NOC Engineer who just needs to pull a compliance
export, or a Network Engineer who only needs to fix one failing GitOps
repo sync -- either meant a blanket promotion to Auditor or Network Admin.

These PERMISSIONS are individually grantable: a handful of specific
capabilities, plus per-page access for the sidebar pages that aren't
already open to every authenticated role. Stored on
`User.extra_permissions` as a comma-separated list of `key` values below
(same storage shape as `extra_roles`).

`implies_roles` lets a granted permission also satisfy an existing
`require_roles(...)` check on the backend -- see
app.core.deps.require_roles -- so granting "Configuration Management"
actually unlocks the config write endpoints it names, not just a frontend
nav item that 403s the moment it's clicked.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Permission:
    key: str
    label: str
    category: str  # "capability" | "page"
    description: str
    implies_roles: tuple[str, ...] = field(default_factory=tuple)


CAPABILITY_PERMISSIONS: list[Permission] = [
    Permission(
        key="config_management",
        label="Configuration Management",
        category="capability",
        description="Push, back up, restore device configs, and manage golden configs -- without full Network Admin access.",
        implies_roles=("network_admin",),
    ),
    Permission(
        key="network_discovery",
        label="Network Discovery (Radar)",
        category="capability",
        description="Run and manage network discovery scans and schedules.",
        implies_roles=("network_admin",),
    ),
    Permission(
        key="logs_export",
        label="Logs & Audit Export",
        category="capability",
        description="Export audit logs, syslog, and compliance bundles.",
        implies_roles=(),
    ),
]

# Sidebar pages that default to a privileged role only -- gated on the
# frontend nav/route level (components/Layout.tsx). Keyed by the route
# path used there (without the leading slash). Granting one of these lets
# a non-privileged user see and use that specific page without a whole
# extra_roles promotion.
#
# Deliberately NOT included here: /security (personal MFA/session
# settings, not an admin panel), /jit-access (any user can request an
# elevation -- only approving is admin-gated), /push-settings (personal
# notification prefs) -- these are already open to every authenticated
# role today and restricting their *nav visibility* while their backend
# stays open would just be confusing, not more secure.
PAGE_PERMISSIONS: list[Permission] = [
    Permission(
        "page:users", "Users", "page",
        "User management page.", implies_roles=("network_admin",),
    ),
    Permission(
        "page:integrations", "Integrations", "page",
        "ChatOps / GitOps / webhook integrations page.", implies_roles=("network_admin",),
    ),
    Permission(
        "page:backups", "Backups", "page",
        "Database & device config backups page.", implies_roles=("network_admin",),
    ),
    Permission(
        "page:terminal-recordings", "Session Recordings", "page",
        "Terminal session recording review page.", implies_roles=("security",),
    ),
    Permission("page:audit-log", "Audit Log", "page", "Full audit log page."),
    Permission("page:auditor-export", "Auditor Export", "page", "Compliance export bundle page."),
    Permission("page:rbac-audit", "RBAC Audit", "page", "RBAC audit page."),
]

ALL_PERMISSIONS: list[Permission] = CAPABILITY_PERMISSIONS + PAGE_PERMISSIONS
PERMISSION_KEYS: set[str] = {p.key for p in ALL_PERMISSIONS}
PERMISSION_BY_KEY: dict[str, Permission] = {p.key: p for p in ALL_PERMISSIONS}


def implied_roles_for(permission_keys) -> set[str]:
    """Union of every role value that any of `permission_keys` implies --
    consulted by require_roles alongside extra_roles/JIT so a granted
    permission actually bypasses the endpoints it names."""
    roles: set[str] = set()
    for key in permission_keys:
        perm = PERMISSION_BY_KEY.get(key)
        if perm:
            roles.update(perm.implies_roles)
    return roles
