import datetime
import uuid

from pydantic import BaseModel, ConfigDict

from app.models.syslog_message import SyslogSeverity


class SyslogMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    device_id: uuid.UUID | None = None
    device_hostname: str | None = None  # joined in, not a model column -- see app.api.syslog
    source_ip: str
    facility: int | None = None
    severity: SyslogSeverity
    reported_hostname: str | None = None
    tag: str | None = None
    message: str
    device_reported_at: datetime.datetime | None = None
    received_at: datetime.datetime
    correlated_category: str | None = None
    correlated_alert_id: uuid.UUID | None = None


class SyslogIngestRequest(BaseModel):
    """POST /syslog/ingest body -- for TCP-relay/test-injection paths that
    can't reach the UDP listener directly. `source_ip` defaults to the
    caller's own address if omitted, but a relay forwarding on behalf of
    many devices needs to state the real originating device explicitly."""

    raw: str
    source_ip: str | None = None


class SyslogVolumePoint(BaseModel):
    hour: str
    count: int


class SyslogSummary(BaseModel):
    total: int
    correlated: int
    by_severity: dict[str, int]
    volume_by_hour: list[SyslogVolumePoint]
