import datetime
import enum

from pydantic import BaseModel


class ComponentStatus(str, enum.Enum):
    UP = "up"
    DOWN = "down"
    DEGRADED = "degraded"
    NOT_CONFIGURED = "not_configured"


class ComponentHealth(BaseModel):
    key: str
    label: str
    status: ComponentStatus
    # Whether this component being DOWN should flip overall system status
    # to "unhealthy" rather than just "degraded" -- optional integrations
    # (GNS3, NetBox, SMTP, Ollama when unused) are never critical.
    critical: bool
    latency_ms: float
    detail: str | None = None


class PageHealth(BaseModel):
    page: str
    status: ComponentStatus
    depends_on: list[str]


class SystemHealthReport(BaseModel):
    status: ComponentStatus
    checked_at: datetime.datetime
    components: list[ComponentHealth]
    pages: list[PageHealth]