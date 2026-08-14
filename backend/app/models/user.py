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
