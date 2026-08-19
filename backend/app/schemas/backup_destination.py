from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

DestinationType = Literal["s3", "azure_blob", "sftp"]


class BackupDestinationCreate(BaseModel):
    name: str
    type: DestinationType
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class BackupDestinationUpdate(BaseModel):
    """All fields optional -- e.g. flipping `enabled` off doesn't require
    resending every secret. When `config` is omitted, the existing
    encrypted config is left untouched."""

    name: str | None = None
    enabled: bool | None = None
    config: dict[str, Any] | None = None


class BackupDestinationRead(BaseModel):
    id: str
    name: str
    type: DestinationType
    enabled: bool
    config: dict[str, Any]  # secrets masked to booleans -- see backup_destination_service.masked_config
    created_by: str | None
    created_at: datetime | None
    last_run_at: datetime | None
    last_run_status: str | None
    last_error: str | None
