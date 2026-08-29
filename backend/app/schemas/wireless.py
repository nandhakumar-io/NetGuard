"""Pydantic schemas for wireless AP and SSID monitoring/CRUD."""
import datetime

from pydantic import BaseModel, ConfigDict, Field

WIRELESS_AP_VENDORS = ["cisco", "aruba", "ruckus", "tplink", "ubiquiti", "mikrotik", "other"]


class WirelessAPRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    controller_device_id: str | None
    ap_index: str | None
    ap_name: str | None
    ap_model: str | None
    ap_ip_address: str | None
    vendor: str
    mac_address: str | None
    management_ip: str | None
    site: str | None
    notes: str | None
    source: str
    oper_status: int | None
    # Derived label ("associated" | "disassociating" | "downloading" | "managed" | "unknown")
    oper_status_label: str
    client_count: int | None
    band_2g_clients: int | None
    band_5g_clients: int | None
    created_at: datetime.datetime
    polled_at: datetime.datetime


class WirelessAPCreate(BaseModel):
    """Manually add an AP not discovered via SNMP polling (e.g. a
    standalone TP-Link or Ruckus AP with no WLC)."""

    ap_name: str = Field(..., min_length=1)
    vendor: str = Field(default="other")
    ap_model: str | None = None
    ap_ip_address: str | None = None
    management_ip: str | None = None
    mac_address: str | None = None
    site: str | None = None
    notes: str | None = None
    client_count: int | None = None


class WirelessAPUpdate(BaseModel):
    ap_name: str | None = None
    vendor: str | None = None
    ap_model: str | None = None
    ap_ip_address: str | None = None
    management_ip: str | None = None
    mac_address: str | None = None
    site: str | None = None
    notes: str | None = None
    client_count: int | None = None


class WirelessSSIDRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    controller_device_id: str
    ssid_index: str
    ssid_name: str
    admin_status: int | None
    # True when admin_status == 1; False otherwise.
    enabled: bool
    mobile_station_count: int | None
    polled_at: datetime.datetime


class WirelessSummary(BaseModel):
    """Aggregated snapshot returned by GET /wireless/summary."""
    controller_device_id: str
    controller_hostname: str | None
    total_aps: int
    aps_up: int       # oper_status == 1
    aps_down: int     # oper_status != 1
    total_clients: int
    band_2g_clients: int
    band_5g_clients: int
    ssid_count: int
    polled_at: datetime.datetime | None
