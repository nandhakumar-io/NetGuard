"""Pydantic schemas for ApprovalDelegate (the "while I'm out, B acts for A"
feature). Mirrors app.models.approval_delegate and is consumed by
app.api.approval_delegates.
"""
from __future__ import annotations

import datetime
import uuid

from pydantic import BaseModel


class ApprovalDelegateCreate(BaseModel):
    """Request body for POST /approval-delegates.

    ``stage_type`` must be a valid app.models.approval_chain.ApprovalStageType
    value (e.g. ``"peer_review"``, ``"manager_signoff"``).
    Both ``starts_at`` and ``ends_at`` are optional; omit both to create an
    open-ended delegation that is effective immediately and lasts until
    explicitly revoked.
    """

    delegate_user_id: uuid.UUID
    stage_type: str
    starts_at: datetime.datetime | None = None
    ends_at: datetime.datetime | None = None
    reason: str | None = None


class ApprovalDelegateRead(BaseModel):
    """Response body for all approval-delegate endpoints."""

    id: uuid.UUID
    delegator_id: uuid.UUID
    delegator_name: str | None = None
    delegate_id: uuid.UUID
    delegate_name: str | None = None
    stage_type: str
    starts_at: datetime.datetime | None = None
    ends_at: datetime.datetime | None = None
    active: bool
    reason: str | None = None
    created_at: datetime.datetime
    revoked_at: datetime.datetime | None = None

    model_config = {"from_attributes": True}
