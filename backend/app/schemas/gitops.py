import datetime
import uuid
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, field_validator


def _validate_template_path(v: str) -> str:
    """Reject absolute paths and any '..' segment at save-time -- fails
    fast in the create/update response instead of only surfacing as a
    sync error later. This is a UX/fail-fast check, not the actual
    security boundary: git_sync_service._resolve_template_dir re-checks
    containment against the real clone directory at use-time regardless,
    since a string-level check here can't account for symlinks inside
    the cloned repo itself.
    """
    if not v:
        return v
    pure = PurePosixPath(v.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError("template_path must be a relative path within the repo, with no '..' segments")
    return v


class GitRepoConfigCreate(BaseModel):
    name: str
    repo_url: str
    branch: str = "main"
    template_path: str = "templates/"
    direction: str = "pull"  # "pull" | "push" | "bidirectional"
    auto_sync_enabled: bool = True
    access_token: str | None = None  # write-only; never echoed back
    webhook_secret: str | None = None  # write-only; never echoed back

    @field_validator("direction")
    @classmethod
    def _valid_direction(cls, v: str) -> str:
        if v not in ("pull", "push", "bidirectional"):
            raise ValueError("direction must be 'pull', 'push', or 'bidirectional'")
        return v

    @field_validator("template_path")
    @classmethod
    def _valid_template_path(cls, v: str) -> str:
        return _validate_template_path(v)


class GitRepoConfigUpdate(BaseModel):
    name: str | None = None
    repo_url: str | None = None
    branch: str | None = None
    template_path: str | None = None
    direction: str | None = None
    auto_sync_enabled: bool | None = None
    access_token: str | None = None
    webhook_secret: str | None = None

    @field_validator("template_path")
    @classmethod
    def _valid_template_path(cls, v: str | None) -> str | None:
        return v if v is None else _validate_template_path(v)


class GitRepoConfigRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    repo_url: str
    branch: str
    template_path: str
    direction: str
    auto_sync_enabled: bool
    has_access_token: bool = False
    has_webhook_secret: bool = False
    last_synced_commit: str | None = None
    last_synced_at: datetime.datetime | None = None
    last_sync_status: str
    last_sync_error: str | None = None
    created_by: str
    created_at: datetime.datetime

    @classmethod
    def from_orm_row(cls, row) -> "GitRepoConfigRead":
        return cls(
            id=row.id, name=row.name, repo_url=row.repo_url, branch=row.branch,
            template_path=row.template_path,
            direction=row.direction.value if hasattr(row.direction, "value") else row.direction,
            auto_sync_enabled=row.auto_sync_enabled,
            has_access_token=bool(row.access_token_encrypted),
            has_webhook_secret=bool(row.webhook_secret_encrypted),
            last_synced_commit=row.last_synced_commit, last_synced_at=row.last_synced_at,
            last_sync_status=row.last_sync_status.value if hasattr(row.last_sync_status, "value") else row.last_sync_status,
            last_sync_error=row.last_sync_error, created_by=row.created_by, created_at=row.created_at,
        )


class GitSyncTriggerResult(BaseModel):
    status: str
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: list[str] = []
