import uuid

from sqlalchemy import Column, DateTime, Enum, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base
from app.models.device import DeviceStatus


class DeviceStatusHistory(Base):
    """One row per Device.status *transition* (online/offline/degraded/
    unknown), not one row per poll -- same "transition log" shape as
    app.models.interface_status.InterfaceStatus, just at the whole-device
    reachability level instead of per-interface.

    Nothing wrote this before: Device.status (app.models.device.Device)
    was always a single live column that got overwritten in place by
    app.services.reachability_service.check_device and
    app.services.metrics_service.poll_device, with no record of *when* a
    device flipped or how long it stayed in a given state. That made an
    uptime/availability rollup ("what % of the last 24h was this device
    reachable") and a flap-detection widget both impossible -- there was
    nothing to compute them from. This table is written by both of those
    call sites whenever the status they're about to set differs from the
    device's current status, and is what
    app.services.metrics_service.fleet_availability_summary /
    unstable_devices are computed from.
    """

    __tablename__ = "device_status_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False, index=True)

    status = Column(Enum(DeviceStatus), nullable=False)
    previous_status = Column(Enum(DeviceStatus), nullable=True)  # null on the very first-ever row for a device

    changed_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
