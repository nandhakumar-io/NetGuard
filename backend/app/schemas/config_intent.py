import uuid

from pydantic import BaseModel


class IntentKindInfo(BaseModel):
    kind: str
    description: str
    required_params: list[str]
    optional_params: list[str]


class RenderIntentRequest(BaseModel):
    kind: str
    params: dict
    # Either target a real device (vendor inferred from its inventory
    # record) or an explicit vendor, for a "what would this look like on
    # platform X" preview with no device in hand yet.
    device_id: uuid.UUID | None = None
    vendor: str | None = None


class RenderIntentResponse(BaseModel):
    vendor: str
    rendered_snippet: str
    proposed_config: str | None = None
    device_id: uuid.UUID | None = None
    current_source: str | None = None


class RenderIntentAllVendorsResponse(BaseModel):
    kind: str
    by_vendor: dict[str, str | None]
