package netguard.authz

import rego.v1

# ---------------------------------------------------------------------------
# NetGuard API-level authorization policy (Hybrid Auth – Phase 2).
#
# This package answers: "may this authenticated user perform <action> on
# <resource>?" for REST API calls. It is intentionally separate from the
# netguard (config compliance) package -- different input shape, different
# semantics, different policy path (/v1/data/netguard/authz/allow).
#
# Input document shape (built by authz_service._build_authz_input):
#
#   input.user.id              -- NetGuard user UUID
#   input.user.roles[]         -- effective roles: [base_role, ...extra_roles]
#   input.user.tenant_id       -- UUID of user's tenant (null for MSP staff)
#   input.user.is_msp_staff    -- bool; MSP staff see across all tenants
#   input.user.jit_roles[]     -- active JIT-elevation role values
#   input.action               -- e.g. "device:write", "config:push", "terminal:open"
#   input.resource.device_id              -- optional: target device UUID
#   input.resource.device_tenant_id       -- optional: tenant owning the device
#   input.resource.device_role            -- optional: device role (core/edge/…)
#
# The "allow" rule is the single boolean the HTTP client expects
# (OPA returns {"result": true|false} for a boolean entrypoint).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Helper: tenant ownership check.
#
# Allow if the user belongs to the same tenant as the target device, OR the
# user is MSP staff (can act across all tenants).
# ---------------------------------------------------------------------------
_same_tenant if {
	input.user.is_msp_staff == true
}

_same_tenant if {
	input.user.tenant_id != null
	input.resource.device_tenant_id != null
	input.user.tenant_id == input.resource.device_tenant_id
}

# No resource tenant set (e.g. non-device endpoints) → skip tenant check.
_same_tenant if {
	not input.resource.device_tenant_id
}

# ---------------------------------------------------------------------------
# network_admin: full access to everything within their tenant.
# ---------------------------------------------------------------------------
allow if {
	"network_admin" in input.user.roles
	_same_tenant
}

# ---------------------------------------------------------------------------
# network_engineer: config push, backup, terminal; cannot delete devices.
# ---------------------------------------------------------------------------
_engineer_actions := {
	"config:push",
	"config:backup",
	"config:golden",
	"config:rollback",
	"terminal:open",
}

allow if {
	"network_engineer" in input.user.roles
	input.action in _engineer_actions
	_same_tenant
}

# Device read/write (not delete) for engineers.
allow if {
	"network_engineer" in input.user.roles
	input.action in {"device:read", "device:write"}
	_same_tenant
}

# ---------------------------------------------------------------------------
# noc_engineer: terminal and read-only device/config access.
# ---------------------------------------------------------------------------
_noc_actions := {"terminal:open", "device:read", "config:backup"}

allow if {
	"noc_engineer" in input.user.roles
	input.action in _noc_actions
	_same_tenant
}

# ---------------------------------------------------------------------------
# security: terminal (auditing), read everything; cannot push/delete.
# ---------------------------------------------------------------------------
_security_actions := {"terminal:open", "device:read", "config:backup"}

allow if {
	"security" in input.user.roles
	input.action in _security_actions
	_same_tenant
}

# ---------------------------------------------------------------------------
# auditor: read-only access.
# ---------------------------------------------------------------------------
allow if {
	"auditor" in input.user.roles
	input.action in {"device:read", "config:backup"}
	_same_tenant
}

# ---------------------------------------------------------------------------
# JIT elevation: a user with an active JIT grant for a role inherits that
# role's allowed actions for the duration of the grant.
# ---------------------------------------------------------------------------
allow if {
	some jit_role in input.user.jit_roles
	jit_role == "network_admin"
	_same_tenant
}

allow if {
	some jit_role in input.user.jit_roles
	jit_role == "network_engineer"
	input.action in (_engineer_actions | {"device:read", "device:write"})
	_same_tenant
}

# ---------------------------------------------------------------------------
# firmware deployment: network_admin or network_engineer only.
# ---------------------------------------------------------------------------
allow if {
	input.action == "firmware:deploy"
	some role in input.user.roles
	role in {"network_admin", "network_engineer"}
	_same_tenant
}

# ---------------------------------------------------------------------------
# JIT approval: only network_admin can approve JIT elevation requests.
# ---------------------------------------------------------------------------
allow if {
	input.action == "jit:approve"
	"network_admin" in input.user.roles
}
