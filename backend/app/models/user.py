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
    hashed_password = Column(String, nullable=False)
    role = Column(Enum(UserRole), nullable=False, default=UserRole.NETWORK_ENGINEER)
    is_active = Column(String, default=True)

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
