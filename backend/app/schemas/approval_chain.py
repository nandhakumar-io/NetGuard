import datetime
import uuid

from pydantic import BaseModel, ConfigDict


class ApprovalStageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    change_request_id: uuid.UUID
    sequence: int
    stage_type: str
    required_role: str
    status: str
    acted_by: uuid.UUID | None = None
    acted_by_name: str | None = None
    acted_at: datetime.datetime | None = None
    notes: str | None = None


class ApprovalChainRead(BaseModel):
    change_request_id: uuid.UUID
    stages: list[ApprovalStageRead]
    fully_approved: bool
    current_stage_sequence: int | None = None


class ApprovalStageActionRequest(BaseModel):
    approve: bool = True
    notes: str | None = None
