import enum
import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, String, func, true
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class UserRole(str, enum.Enum):
    NETWORK_ENGINEER = "network_engineer"
    NOC_ENGINEER = "noc_engineer"
    NETWORK_ADMIN = "network_admin"
    SECURITY = "security"
    AUDITOR = "auditor"


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Which managed customer this user belongs to (see app.models.tenant.
    # Tenant). Nullable specifically so is_msp_staff accounts can be
    # tenant-less -- they work across every tenant rather than belonging
    # to one. A non-MSP-staff user should always have this set; every
    # pre-existing row was backfilled onto the "Default" tenant by
    # migration 0092_tenants so this addition didn't require picking
    # nullable=False and breaking existing logins.
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True)

    # MSP staff see and act across every tenant (the cross-tenant NOC
    # board, app.api.tenant_board, is gated on this) rather than being
    # scoped to tenant_id like a regular customer-side user. Distinct
    # from `role`/`extra_roles` -- this is a tenancy-scope switch, not a
    # permission level; an msp staff account still has a normal role
    # (NOC_ENGINEER, NETWORK_ADMIN, ...) that governs what it can *do*.
    is_msp_staff = Column(Boolean, nullable=False, default=False, server_default="false")

    email = Column(String, unique=True, index=True, nullable=False)
    full_name = Column(String, nullable=False)
    # Nullable: an SSO-only user (see sso_provider below) never has a local
    # NetGuard password to check, so there's nothing to hash. Local
    # email/password accounts still always populate this.
    hashed_password = Column(String, nullable=True)
    role = Column(Enum(UserRole), nullable=False, default=UserRole.NETWORK_ENGINEER)
    is_active = Column(String, default=True)

    # Registration-approval gate (migration 0095_approval_and_tenant_scoping).
    # False only for a brand-new POST /auth/register row -- see
    # app.api.auth.register, which explicitly overrides this default.
    # Every other creation path (admin-created via POST /users, seed
    # scripts, pre-existing rows backfilled by the migration) leaves this
    # at its True default, since an admin creating the account directly
    # *is* the approval. NOTE: this column was added to the database by
    # migration 0095 but was missing from this model for a while -- if
    # you're here because approve/reject "didn't seem to do anything",
    # that mapping gap was the cause; it's fixed now, but double check
    # there isn't a similar migration-vs-model drift elsewhere.
    is_approved = Column(Boolean, nullable=False, default=True, server_default=true())

    # Fine-grained access beyond the base `role`: a comma-separated list of
    # additional UserRole values (e.g. a NETWORK_ENGINEER who also needs
    # Security's terminal-recording review, without promoting them to a
    # full extra role's *entire* surface -- see require_roles in
    # app.core.deps, which checks `role in roles OR extra_roles ∩ roles`.
    # String/comma-separated rather than a real array column to match this
    # model's existing is_active/mfa_enabled portability convention (see
    # below) and to avoid a Postgres ARRAY-specific migration.
    extra_roles = Column(String, nullable=True)

    # Fine-grained, individually-grantable capability/page permissions --
    # complements extra_roles (which grants a *whole other role's*
    # surface). Comma-separated list of app.core.permissions.Permission
    # `key` values (e.g. "config_management", "network_discovery",
    # "page:backups"). See app.core.permissions and
    # app.core.deps.require_roles (which consults
    # permissions.implied_roles_for(...) against this column).
    extra_permissions = Column(String, nullable=True)

    # SSO identity link (Google OIDC today; provider is a plain string so
    # Okta/Entra OIDC can reuse the same columns later without a migration).
    # sso_subject is the IdP's stable `sub` claim, not email -- email can be
    # reassigned at some IdPs, sub can't.
    sso_provider = Column(String, nullable=True)
    sso_subject = Column(String, nullable=True)

    # Multi-Factor Authentication (NFR Security / FR-1)
    mfa_secret = Column(String, nullable=True)  # TOTP secret; set on /mfa/setup, unused until enabled
    mfa_enabled = Column(String, default="false")  # "true" / "false" -- string for SQLite/Postgres portability

    # Security PIN (step-up auth): a short numeric PIN, separate from the
    # login password, that can be required as a second check immediately
    # before opening a device terminal or firing a critical, high-blast-
    # radius action (device delete, config rollback, etc.) -- see
    # app.core.deps.require_pin_step_up. Opt-in per user (pin_required
    # defaults False so setting this up never locks anyone out
    # retroactively); hashed with the same bcrypt helper as the login
    # password (app.core.security.hash_password/verify_password), just
    # applied to the PIN string instead. Nullable: most users will never
    # set one.
    security_pin_hash = Column(String, nullable=True)
    security_pin_set_at = Column(DateTime(timezone=True), nullable=True)
    # Whether the PIN, once set, is actually *enforced* for terminal/
    # critical-action step-up -- kept separate from security_pin_hash so a
    # user can set a PIN in advance and only flip enforcement on when
    # ready, and so turning enforcement off doesn't require throwing away
    # (and later re-typing) the PIN itself.
    pin_required = Column(Boolean, nullable=False, default=False, server_default="false")

    # ChatOps identity links (FR: two-way Slack/Teams). Populated only via
    # POST /chatops/links by a Network Admin -- never self-service from an
    # unauthenticated Slack/Teams message, since that would let anyone who
    # can DM the bot claim to be any NetGuard user. Nullable/unique: a
    # given Slack or Teams account maps to at most one NetGuard user.
    slack_user_id = Column(String, unique=True, index=True, nullable=True)
    msteams_user_id = Column(String, unique=True, index=True, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Stamped by app.api.auth._issue_token_pair -- the one choke point
    # every successful login path (plain password, post-MFA, SSO) already
    # runs through, so this is set in exactly one place rather than
    # duplicated per login route. Backs User Management's Last Login
    # column (app.api.user_management) -- previously there was no way to
    # tell a genuinely stale account from one that logs in weekly.
    last_login_at = Column(DateTime(timezone=True), nullable=True)

    from sqlalchemy.orm import relationship
    tenant = relationship("Tenant")
