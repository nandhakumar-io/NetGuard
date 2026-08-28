"""Pydantic read schemas for wireless AP and SSID monitoring."""
import datetime

from pydantic import BaseModel, ConfigDict


class WirelessAPRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    controller_device_id: str
    ap_index: str
    ap_name: str | None
    ap_model: str | None
    ap_ip_address: str | None
    oper_status: int | None
    # Derived label ("associated" | "disassociating" | "downloading" | "unknown")
    oper_status_label: str
    client_count: int | None
    band_2g_clients: int | None
    band_5g_clients: int | None
    polled_at: datetime.datetime


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
