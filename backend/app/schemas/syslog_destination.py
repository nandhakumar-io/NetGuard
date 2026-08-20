from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SyslogProtocolLiteral = Literal["udp", "tcp"]
SeverityLiteral = Literal["info", "warning", "critical"]


class SyslogDestinationCreate(BaseModel):
    name: str
    host: str
    port: int = Field(default=514, ge=1, le=65535)
    protocol: SyslogProtocolLiteral = "udp"
    facility: int = Field(default=16, ge=0, le=23)
    min_severity: SeverityLiteral = "info"
    use_rfc5424: bool = False
    enabled: bool = True


class SyslogDestinationUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    protocol: SyslogProtocolLiteral | None = None
    facility: int | None = Field(default=None, ge=0, le=23)
    min_severity: SeverityLiteral | None = None
    use_rfc5424: bool | None = None
    enabled: bool | None = None


class SyslogDestinationRead(BaseModel):
    id: str
    name: str
    host: str
    port: int
    protocol: str
    facility: int
    min_severity: str
    use_rfc5424: bool
    enabled: bool
    created_by: str | None
    created_at: datetime | None
    last_sent_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
