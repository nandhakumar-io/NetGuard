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
    ap_up_time: str | None
    ap_software_version: str | None
    ap_serial_number: str | None
    channel_2g: int | None
    channel_5g: int | None
    tx_power_2g: int | None
    tx_power_5g: int | None
    noise_2g: int | None
    noise_5g: int | None
    channel_util_2g: int | None
    channel_util_5g: int | None
    created_at: datetime.datetime
    polled_at: datetime.datetime

    # Derived, not stored on the row -- computed per-request by
    # app.api.wireless from LLDP neighbor / Device-inventory correlation
    # (see wireless_service.find_switchports_for_aps /
    # find_matching_device_for_ap). None when no correlation was found.
    switch_device_id: str | None = None
    switch_hostname: str | None = None
    switch_port: str | None = None
    matched_device_id: str | None = None
    matched_device_hostname: str | None = None


class UnregisteredApRead(BaseModel):
    """One switchport whose LLDP neighbor looks like an access point but
    doesn't match any AP already tracked on the Wireless page -- see
    wireless_service.find_unregistered_aps."""

    neighbor_name: str | None
    mac_address: str | None
    sys_desc: str | None
    switch_device_id: str
    switch_hostname: str | None
    port: str | None
    discovered_at: datetime.datetime


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
