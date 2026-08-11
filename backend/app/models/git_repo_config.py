import enum
import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class GitSyncDirection(str, enum.Enum):
    """Which way config templates move between NetGuard and the repo.

    PULL: repo is the source of truth -- a push/PR-merge webhook (or a
        manual sync) reads `*.j2`/`*.yaml` files under `template_path` and
        creates a new PENDING_APPROVAL ConfigTemplateVersion for review in
        NetGuard, same as a human submitting a version.
    PUSH: NetGuard is the source of truth -- publishing a
        ConfigTemplateVersion (POST .../versions/{id}/approve) also
        commits+pushes the rendered file to the repo, so the repo mirrors
        exactly what's live.
    BIDIRECTIONAL: both of the above.
    """

    PULL = "pull"
    PUSH = "push"
    BIDIRECTIONAL = "bidirectional"


class GitSyncStatus(str, enum.Enum):
    NEVER_SYNCED = "never_synced"
    SYNCING = "syncing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class GitRepoConfig(Base):
    """A Git repository wired up for config-as-code / GitOps sync of
    app.models.config_template.ConfigTemplate bodies.

    Templates in the repo are plain files under `template_path`, one per
    template, with a small YAML front-matter header identifying which
    ConfigTemplate they map to:

        ---
        name: standard-access-switch
        device_role: access
        vendor: cisco
        ---
        interface {{ interface_name }}
         switchport mode access
         switchport access vlan {{ vlan_id }}

    `access_token_encrypted` is a Personal Access Token (GitHub/GitLab/
    Bitbucket) with repo read (PULL) or read+write (PUSH/BIDIRECTIONAL)
    scope, encrypted at rest the same way SNMP/SSH credentials are (see
    app.core.crypto) -- never returned by any API response.

    `webhook_secret_encrypted` is a random shared secret the operator also
    pastes into the repo host's webhook config (GitHub: "Secret" field on
    the webhook). Inbound webhook deliveries are HMAC-SHA256 verified
    against it (GitHub's X-Hub-Signature-256 convention) before anything
    in the payload is trusted.
    """

    __tablename__ = "git_repo_configs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False, unique=True)
    repo_url = Column(String, nullable=False)  # e.g. https://github.com/org/network-configs.git
    branch = Column(String, nullable=False, default="main")
    template_path = Column(String, nullable=False, default="templates/")

    direction = Column(Enum(GitSyncDirection), nullable=False, default=GitSyncDirection.PULL)
    auto_sync_enabled = Column(Boolean, nullable=False, default=True)

    access_token_encrypted = Column(Text, nullable=True)
    webhook_secret_encrypted = Column(Text, nullable=True)

    last_synced_commit = Column(String, nullable=True)
    last_synced_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_status = Column(Enum(GitSyncStatus), nullable=False, default=GitSyncStatus.NEVER_SYNCED)
    last_sync_error = Column(Text, nullable=True)

    created_by = Column(String, nullable=False, default="system")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
