import datetime

from pydantic import BaseModel


class ComplianceBaselineSet(BaseModel):
    config: str
    description: str | None = None


class ComplianceBaselineRead(BaseModel):
    device_role: str
    config: str
    config_pretty: str | None = None
    is_xml: bool = False
    checksum: str
    description: str | None = None
    set_by: str
    device_count: int = 0  # how many devices currently have this device_role
    created_at: datetime.datetime
    updated_at: datetime.datetime


class ComplianceBaselineSummary(BaseModel):
    """Lighter-weight row for the list view (no full config body)."""

    device_role: str
    checksum: str
    description: str | None = None
    set_by: str
    device_count: int = 0
    updated_at: datetime.datetime