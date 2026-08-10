from pydantic import BaseModel, Field


class DashboardWidgetInfo(BaseModel):
    id: str
    title: str
    data_source: str
    default_visible: bool


class DashboardLayoutEntry(BaseModel):
    id: str
    visible: bool = True


class DashboardPreferenceRead(BaseModel):
    layout: list[DashboardLayoutEntry]
    # The full widget catalog (id/title/data_source), so the frontend's
    # customize panel can show a human-readable label for every id
    # without hardcoding the registry a second time in TypeScript.
    available_widgets: list[DashboardWidgetInfo]


class DashboardPreferenceUpdate(BaseModel):
    layout: list[DashboardLayoutEntry] = Field(
        ..., description="Full ordered list of {id, visible} -- unknown ids are dropped, missing known ids are appended."
    )
