import uuid
from datetime import datetime, timezone

from app.models.backup_destination import BackupDestination
from app.schemas.backup_destination import (
    BackupDestinationCreate,
    BackupDestinationRead,
    BackupDestinationUpdate,
)
from app.services import backup_destination_service


def test_backup_destination_model_instantiation():
    dest_id = uuid.uuid4()
    dest = BackupDestination(
        id=dest_id,
        name="Offsite S3",
        type="s3",
        enabled=True,
        config_encrypted="encrypted-data",
        created_by="admin@example.com",
    )
    assert dest.id == dest_id
    assert dest.name == "Offsite S3"
    assert dest.type == "s3"
    assert dest.enabled is True
    assert dest.config_encrypted == "encrypted-data"
    assert dest.created_by == "admin@example.com"


def test_encrypt_decrypt_config():
    config = {
        "bucket": "my-backup-bucket",
        "region": "us-east-1",
        "access_key_id": "AKIAEXAMPLE",
        "secret_access_key": "secret123",
    }
    encrypted = backup_destination_service.encrypt_config("s3", config)
    assert isinstance(encrypted, str)

    decrypted = backup_destination_service.decrypt_config(encrypted)
    assert decrypted == config


def test_masked_config():
    config = {
        "bucket": "my-backup-bucket",
        "region": "us-east-1",
        "access_key_id": "AKIAEXAMPLE",
        "secret_access_key": "secret123",
    }
    masked = backup_destination_service.masked_config("s3", config)
    assert masked["bucket"] == "my-backup-bucket"
    assert masked["secret_access_key"] is True


def test_schema_validations():
    create_schema = BackupDestinationCreate(
        name="Primary S3",
        type="s3",
        enabled=True,
        config={"bucket": "my-bucket"},
    )
    assert create_schema.name == "Primary S3"
    assert create_schema.type == "s3"

    update_schema = BackupDestinationUpdate(enabled=False)
    assert update_schema.enabled is False
    assert update_schema.name is None

    read_schema = BackupDestinationRead(
        id=str(uuid.uuid4()),
        name="Read S3",
        type="s3",
        enabled=True,
        config={"bucket": "my-bucket", "secret_access_key": True},
        created_by="admin@example.com",
        created_at=datetime.now(timezone.utc),
        last_run_at=None,
        last_run_status="success",
        last_error=None,
    )
    assert read_schema.name == "Read S3"
    assert read_schema.last_run_status == "success"
