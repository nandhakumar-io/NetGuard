import datetime
import uuid

from pydantic import BaseModel, ConfigDict


class InterfaceStatusRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    device_id: uuid.UUID
    if_index: str
    if_descr: str
    status: str
    previous_status: str | None = None
    changed_at: datetime.datetime | None = None


class InterfaceCurrentStatus(BaseModel):
    """Latest known status of one interface -- what the device detail
    panel / topology drawer's "Interfaces" tab lists, one row per port
    currently known about (not one row per historical change)."""

    if_index: str
    if_descr: str
    status: str
    changed_at: datetime.datetime | None = None
    # How long this interface has held its current status, in seconds --
    # computed at read time (now - changed_at), handy for spotting a port
    # that's been flapping vs. one that's been stably down for weeks.
    seconds_in_status: float | None = None