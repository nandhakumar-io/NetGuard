"""Pydantic schemas for the Network Discovery API."""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class DiscoveryScanCreate(BaseModel):
    cidr: str = Field(..., description="CIDR range to sweep, e.g. '10.0.4.0/24'")
    snmp_community: str | None = Field(
        None, description="Optional SNMP v2c community used to identify responsive hosts (sysName/sysDescr)."
    )
    ports: list[int] | None = Field(
        None, description="TCP ports to probe. Defaults to 22,23,80,443,161,3389 if omitted."
    )


class DiscoveryScanRead(BaseModel):
    id: uuid.UUID
    cidr: str
    status: str
    error: str | None = None
    total_hosts: int
    responsive_hosts: int
    new_hosts: int
    started_by: str | None = None
    started_at: datetime
    completed_at: datetime | None = None

    class Config:
        from_attributes = True


class DiscoveredHostRead(BaseModel):
    id: uuid.UUID
    scan_id: uuid.UUID
    ip_address: str
    hostname: str | None = None
    mac_address: str | None = None
    open_ports: str | None = None
    snmp_sys_name: str | None = None
    snmp_sys_descr: str | None = None
    vendor_guess: str | None = None
    response_time_ms: float | None = None
    matched_device_id: uuid.UUID | None = None
    ipam_status: str = Field(
        "unmanaged",
        description="unmanaged (no IPAM subnet covers this IP) / expected (IPAM has a reservation, "
        "not yet provisioned) / rogue (IPAM manages this subnet, no reservation, no matching device) "
        "/ assigned (already a known device).",
    )
    ipam_reservation_note: str | None = Field(
        None, description="Note copied from the matching IPReservation when ipam_status == 'expected'."
    )
    imported: bool
    imported_device_id: uuid.UUID | None = None
    ignored: bool
    discovered_at: datetime

    class Config:
        from_attributes = True


class DiscoveredHostImport(BaseModel):
    hostname: str = Field(..., description="Hostname to give the new Device row.")
    vendor: str | None = Field(None, description="cisco/juniper/arista/linux -- defaults to a best guess.")
    site: str | None = None
    device_type: str | None = None
    device_role: str | None = None
    # Optional credential *pointers* (legacy env-var ref names / usernames /
    # SNMP dialect), never raw secrets -- typically pre-filled from
    # GET /discovery/hosts/{id}/suggested-credentials and left editable by
    # the operator. The actual secret (if any) still has to be set
    # afterwards via POST /devices/{id}/ssh-credentials or
    # /snmp-credentials, same as any other device -- this only saves
    # re-typing the *ref*/username/version an existing fleet already
    # agreed on.
    ssh_credential_ref: str | None = None
    ssh_username: str | None = None
    snmp_community_ref: str | None = None
    snmp_username: str | None = None
    snmp_version: str | None = Field(None, description="v1/v2c/v3")


class CredentialSuggestion(BaseModel):
    """Metadata-only credential profile suggestion for a discovered host's
    guessed vendor -- see services.credential_service.suggest_credentials_for_vendor
    for why this never includes decrypted secret material."""

    vendor: str
    sample_size: int
    total_vendor_devices: int
    ssh_credential_ref: str | None = None
    ssh_username: str | None = None
    snmp_community_ref: str | None = None
    snmp_username: str | None = None
    snmp_version: str | None = None
    snmp_security_level: str | None = None


class DiscoveredHostReserve(BaseModel):
    note: str | None = Field(
        None, description="Optional note for the created IPReservation, e.g. a ticket/rollout reference."
    )


class DiscoveryScheduleCreate(BaseModel):
    name: str
    cidr: str
    snmp_community: str | None = None
    ports: list[int] | None = None
    interval_minutes: int = Field(1440, ge=5, description="Minimum 5 minutes between sweeps.")
    enabled: bool = True


class DiscoveryScheduleUpdate(BaseModel):
    name: str | None = None
    cidr: str | None = None
    snmp_community: str | None = None
    ports: list[int] | None = None
    interval_minutes: int | None = Field(None, ge=5)
    enabled: bool | None = None


class DiscoveryScheduleRead(BaseModel):
    id: uuid.UUID
    name: str
    cidr: str
    ports: str | None = None
    interval_minutes: int
    enabled: bool
    created_by: str | None = None
    created_at: datetime
    last_run_at: datetime | None = None
    last_scan_id: uuid.UUID | None = None

    class Config:
        from_attributes = True
