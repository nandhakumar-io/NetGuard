import uuid

from sqlalchemy import Boolean, Column, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class BackupDestination(Base):
    """Off-site copy target (S3, Azure Blob Storage, or remote server over SFTP)
    that completed database backups get pushed to. See
    app.services.backup_destination_service and app.api.backups.
    """

    __tablename__ = "backup_destinations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)  # "s3" | "azure_blob" | "sftp"
    enabled = Column(Boolean, nullable=False, default=True)

    config_encrypted = Column(Text, nullable=False)

    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    last_run_at = Column(DateTime(timezone=True), nullable=True)
    last_run_status = Column(String, nullable=True)  # "success" | "failed"
    last_error = Column(Text, nullable=True)
