import enum
import uuid

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base


class FirmwareUpgradeStatus(str, enum.Enum):
    """Step-by-step lifecycle, mirroring how Deployment tracks a config
    push -- each state is written to the DB as it's reached so the UI can
    show live progress instead of a single pending/done flag.
    """

    PENDING = "pending"  # created, not yet picked up by the worker
    SCHEDULED = "scheduled"  # waiting for its scheduled_at time (or maintenance window)
    DOWNLOADING = "downloading"  # image transferred to device flash/bootflash
    INSTALLING = "installing"  # image verified + set as boot target
    REBOOTING = "rebooting"  # device reloading into new image
    VERIFYING = "verifying"  # post-reboot reachability + `show version` check
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"  # verification failed; device reverted to previous image
    CANCELLED = "cancelled"  # cancelled before it started


class FirmwareUpgrade(Base):
    """One bulk/individual firmware-or-OS upgrade job for a device.

    SRS gap this closes: EOL tracking (app.services.eol_service) tells an
    operator *which* devices are running unsupported software, but nothing
    in the app could actually act on that -- the operator still had to
    manually console into every box. This gives that a real, auditable
    workflow: pick device(s) + target version + image, optionally attach
    a maintenance window so upgrades only run during approved hours, and
    the worker drives the device through download -> install -> reboot ->
    verify, rolling back automatically if the device doesn't come back
    healthy on the new version.

    Multiple devices upgraded "in bulk" (a SolarWinds-NCM-style batch job)
    are represented as one FirmwareUpgrade row per device sharing a
    `batch_id`, so each device's progress/failure is independent and
    retryable without one bad device blocking the rest of the batch.
    """

    __tablename__ = "firmware_upgrades"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    batch_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False, index=True)

    from_version = Column(String, nullable=True)  # captured at job start from Device.os_version
    target_version = Column(String, nullable=False)
    image_filename = Column(String, nullable=False)  # e.g. "cat9k_iosxe.17.09.05.SPA.bin"
    image_sha256 = Column(String, nullable=True)  # optional integrity check before install

    status = Column(Enum(FirmwareUpgradeStatus), nullable=False, default=FirmwareUpgradeStatus.PENDING)
    current_step_detail = Column(Text, nullable=True)  # human-readable status line for the UI
    error_message = Column(Text, nullable=True)

    # Optional: only start once this window opens, so a batch scheduled
    # for a Saturday 02:00 maintenance window doesn't start early even if
    # the job was queued days ahead.
    maintenance_window_id = Column(UUID(as_uuid=True), ForeignKey("maintenance_windows.id"), nullable=True)
    scheduled_at = Column(DateTime(timezone=True), nullable=True)

    # Pre-upgrade config snapshot id, so a failed upgrade that needed a
    # config rollback (not just an image rollback) has something to
    # restore from -- reuses the existing ConfigSnapshot mechanism rather
    # than inventing a second one.
    pre_upgrade_snapshot_id = Column(UUID(as_uuid=True), ForeignKey("config_snapshots.id"), nullable=True)

    reboot_wait_seconds = Column(Integer, nullable=False, default=90, server_default="90")
    attempts = Column(Integer, nullable=False, default=0, server_default="0")

    initiated_by = Column(String, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    device = relationship("Device")
    maintenance_window = relationship("MaintenanceWindow")