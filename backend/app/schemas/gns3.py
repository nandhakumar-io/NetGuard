"""Pydantic schemas for the GNS3 Lab Integration API."""
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class GNS3StatusResponse(BaseModel):
    enabled: bool
    reachable: bool
    version: str | None = None
    controller_url: str | None = None
    detail: str | None = None


class GNS3ProjectSummary(BaseModel):
    project_id: str
    name: str
    status: str | None = None
    filename: str | None = None


class GNS3NodeSummary(BaseModel):
    node_id: str
    name: str
    node_type: str | None = None
    status: str | None = None
    console_host: str | None = None
    console_port: int | None = None
    console_type: str | None = None
    vendor_guess: str
    synced: bool = False
    device_id: uuid.UUID | None = None
    bootstrapped: bool = False
    management_ip: str | None = None


class GNS3BootstrapRequest(BaseModel):
    mgmt_interface: str = Field(default="GigabitEthernet0/0")
    mgmt_ip: str = Field(..., description="Management IPv4 address to configure.")
    mgmt_subnet_mask: str = Field(default="255.255.255.0")
    ssh_username: str = Field(default="admin")
    ssh_password: str = Field(..., min_length=1)
    enable_password: str | None = Field(default=None)
    hostname: str | None = Field(default=None)
    ssh_credential_ref: str | None = Field(default=None)
    create_device: bool = Field(default=True)
    site: str | None = Field(default="GNS3 Lab")


class GNS3BootstrapResponse(BaseModel):
    success: bool
    output: str
    error: str | None = None
    device_id: uuid.UUID | None = None
    hostname: str | None = None
    management_ip: str | None = None
    message: str


class GNS3SyncRequest(BaseModel):
    default_ssh_username: str = Field(default="admin")
    default_ssh_credential_ref: str | None = Field(default=None)
    site: str = Field(default="GNS3 Lab")
    placeholder_ip: str = Field(default="0.0.0.0")


class GNS3SyncNodeResult(BaseModel):
    node_id: str
    name: str
    action: str  # created | updated | skipped
    device_id: uuid.UUID | None = None
    detail: str | None = None


class GNS3SyncResponse(BaseModel):
    project_id: str
    created: int
    updated: int
    skipped: int
    results: list[GNS3SyncNodeResult]


class GNS3NodeActionResponse(BaseModel):
    project_id: str
    node_id: str
    action: str
    status: str | None = None
    detail: str | None = None