import enum
import uuid

from sqlalchemy import Column, DateTime, Enum, String, func
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
    email = Column(String, unique=True, index=True, nullable=False)
    full_name = Column(String, nullable=False)
    # Nullable: an SSO-only user (see sso_provider below) never has a local
    # NetGuard password to check, so there's nothing to hash. Local
    # email/password accounts still always populate this.
    hashed_password = Column(String, nullable=True)
    role = Column(Enum(UserRole), nullable=False, default=UserRole.NETWORK_ENGINEER)
    is_active = Column(String, default=True)

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
