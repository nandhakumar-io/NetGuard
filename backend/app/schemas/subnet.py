import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SubnetCreate(BaseModel):
    cidr: str = Field(description="e.g. '10.20.30.0/24'")
    name: str | None = None
    vlan_id: int | None = None
    site: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    auto_rescan_enabled: bool = False
    rescan_interval_hours: int | None = None

    @field_validator("cidr")
    @classmethod
    def _validate_cidr(cls, v: str) -> str:
        import ipaddress

        try:
            net = ipaddress.ip_network(v, strict=False)
        except ValueError as exc:
            raise ValueError(f"Invalid CIDR: {exc}")
        if net.version != 4:
            raise ValueError("Only IPv4 subnets are supported")
        return str(net)  # normalized network form, e.g. "10.20.30.0/24"


class SubnetUpdate(BaseModel):
    name: str | None = None
    vlan_id: int | None = None
    site: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    auto_rescan_enabled: bool | None = None
    rescan_interval_hours: int | None = None


class SubnetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    cidr: str
    name: str | None = None
    vlan_id: int | None = None
    site: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime.datetime | None = None
    updated_at: datetime.datetime | None = None

    # Computed, not columns -- see services.ipam_service.subnet_utilization.
    total_addresses: int = 0
    usable_addresses: int = 0
    used_count: int = 0
    free_count: int = 0
    utilization_pct: float = 0.0
    last_scanned_at: datetime.datetime | None = None
    scanned_only_count: int = 0
    auto_rescan_enabled: bool = False
    rescan_interval_hours: int | None = None


class IPReservationCreate(BaseModel):
    ip_address: str
    state: str = "reserved"  # reserved | gateway | broadcast | network
    note: str | None = None


class IPReservationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    subnet_id: uuid.UUID
    ip_address: str
    state: str
    note: str | None = None
    created_at: datetime.datetime | None = None


class StaleReservation(BaseModel):
    """A RESERVED IPReservation discovery scans haven't confirmed a live
    host for -- see services.ipam_service.stale_reservations."""

    reservation_id: uuid.UUID
    subnet_id: uuid.UUID
    subnet_cidr: str
    ip_address: str
    note: str | None = None
    reserved_at: datetime.datetime | None = None
    coverage: str = Field(description="'never_scanned' (no discovery scan has covered this address) or "
                           "'scanned_no_response' (a scan covered it and found nothing)")
    last_scan_at: datetime.datetime | None = None


class SubnetAddressEntry(BaseModel):
    """One row in the "show every address" view for a subnet -- used by
    the free-IP finder and the subnet detail page's address table alike.
    """

    ip_address: str
    state: str  # free | assigned | reserved | gateway | broadcast | network
    device_id: uuid.UUID | None = None
    hostname: str | None = None
    note: str | None = None
    # Only ever populated for "scanned" rows, and only after a
    # fingerprinting pass (not a plain ping-sweep) has been run -- see
    # services.ipam_service.fingerprint_subnet.
    os_guess: str | None = None
    os_accuracy: int | None = None
    device_type: str | None = None
    mac_vendor: str | None = None
    fingerprinted_at: datetime.datetime | None = None


class FreeIPResult(BaseModel):
    subnet_id: uuid.UUID
    cidr: str
    free_ip: str | None = None
    message: str | None = None


class IPConflict(BaseModel):
    ip_address: str
    device_ids: list[uuid.UUID]
    hostnames: list[str]


class SubnetScanResult(BaseModel):
    subnet_id: uuid.UUID
    scanned_at: datetime.datetime
    hosts_found: int
    addresses_scanned: int


class SubnetFingerprintResult(BaseModel):
    subnet_id: uuid.UUID
    fingerprinted_at: datetime.datetime
    hosts_fingerprinted: int
    addresses_scanned: int


class ConflictReport(BaseModel):
    conflicts: list[IPConflict] = Field(default_factory=list)
