from pydantic import BaseModel, Field


class DashboardWidgetInfo(BaseModel):
    id: str
    title: str
    data_source: str
    default_visible: bool


class DashboardLayoutEntry(BaseModel):
    id: str
    visible: bool = True


class MetricThreshold(BaseModel):
    """Warn/critical band for one metric, 0-100. `warn` must be < `critical`
    (enforced/clamped in dashboard_widgets.merge_thresholds, not here, so a
    bad PUT payload degrades to the default band instead of 422ing the
    whole request)."""

    warn: float = 70
    critical: float = 90


class DashboardThresholds(BaseModel):
    cpu: MetricThreshold = Field(default_factory=MetricThreshold)
    memory: MetricThreshold = Field(default_factory=MetricThreshold)
    bandwidth: MetricThreshold = Field(default_factory=MetricThreshold)


class DashboardPreferenceRead(BaseModel):
    layout: list[DashboardLayoutEntry]
    # The full widget catalog (id/title/data_source), so the frontend's
    # customize panel can show a human-readable label for every id
    # without hardcoding the registry a second time in TypeScript.
    available_widgets: list[DashboardWidgetInfo]
    thresholds: DashboardThresholds


class DashboardPreferenceUpdate(BaseModel):
    layout: list[DashboardLayoutEntry] = Field(
        ..., description="Full ordered list of {id, visible} -- unknown ids are dropped, missing known ids are appended."
    )
    thresholds: DashboardThresholds | None = Field(
        default=None, description="Per-metric warn/critical bands. Omitted metrics keep their previous/default band."
    )
