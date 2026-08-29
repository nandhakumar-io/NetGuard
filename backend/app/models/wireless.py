"""SQLAlchemy models for wireless AP and SSID monitoring.

One WirelessAP row per physical AP per controller device, upserted on
each SNMP poll by app.services.wireless_service.poll_wireless_controller.
One WirelessSSID row per SSID (ESS) profile per controller, likewise.

Both tables store the *most-recent* snapshot only (see migration 0104) --
the intent is \"what does the wireless environment look like right now\",
not a full time-series.  Historical trending of client counts, if ever
wanted, should push to VictoriaMetrics instead of growing these tables.
"""
import uuid

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class WirelessAP(Base):
    """One access point managed by a Cisco AireOS WLC (or compatible SNMP
    controller).  Keyed by (controller_device_id, ap_index) so an AP that
    disappears from the WLC's table simply stops being refreshed rather
    than accumulating stale rows.

    oper_status maps to AIRESPACE-WIRELESS-MIB bsnAPOperationStatus:
      1  associated   -- AP is up and serving clients
      2  disassociating -- AP is in the process of leaving the controller
      3  downloading  -- AP is downloading firmware / booting

    Anything that maps to 1 is \"online\" for the health badge; anything
    else is \"offline\" / \"degraded\" depending on context.
    """

    __tablename__ = "wireless_aps"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # The WLC (or other SNMP controller) that reported this AP. Null for
    # manually-added APs (source="manual") that have no controller.
    controller_device_id = Column(
        UUID(as_uuid=True),
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # SNMP table index for this AP within the WLC's bsnAPTable. Null for
    # manually-added APs.
    ap_index = Column(String, nullable=True)

    ap_name = Column(String, nullable=True)   # bsnAPName
    ap_model = Column(String, nullable=True)  # bsnAPModel
    ap_ip_address = Column(String, nullable=True)  # bsnApIpAddress

    # "cisco" | "aruba" | "ruckus" | "tplink" | "ubiquiti" | "mikrotik" | "other"
    vendor = Column(String, nullable=False, default="cisco")
    mac_address = Column(String, nullable=True)
    # Management address for a manually-added AP; distinct from
    # ap_ip_address (which is populated from SNMP polling).
    management_ip = Column(String, nullable=True)
    site = Column(String, nullable=True)
    notes = Column(String, nullable=True)
    # "polled" (came from poll_wireless_controller) or "manual" (added
    # through the CRUD API). Polled rows get overwritten on every poll
    # cycle; manual rows never are.
    source = Column(String, nullable=False, default="polled")

    oper_status = Column(Integer, nullable=True)   # bsnAPOperationStatus
    client_count = Column(Integer, nullable=True)  # bsnApNumOfUsers (all radios)
    band_2g_clients = Column(Integer, nullable=True)  # per-radio breakdown, best-effort
    band_5g_clients = Column(Integer, nullable=True)

    # Extra troubleshooting telemetry, all best-effort/nullable -- a
    # controller or firmware that doesn't expose one of these just leaves
    # it None rather than failing the rest of the poll (see
    # wireless_service.poll_wireless_controller).
    ap_up_time = Column(String, nullable=True)      # bsnAPUpTime, TimeTicks rendered as-reported (mixed formats across firmware, so kept as text rather than parsed into a duration)
    ap_software_version = Column(String, nullable=True)  # bsnAPSoftwareVersion -- lets Ops spot an AP running a stale image at a glance
    ap_serial_number = Column(String, nullable=True)     # bsnAPDot3MacAddress-adjacent bsnAPSerialNumber, for warranty/RMA lookups without a site visit
    channel_2g = Column(Integer, nullable=True)     # bsnAPIfPhyChannelNumber, 802.11b/g/n radio
    channel_5g = Column(Integer, nullable=True)     # bsnAPIfPhyChannelNumber, 802.11a/n/ac radio
    tx_power_2g = Column(Integer, nullable=True)    # bsnAPIfPhyTxPowerLevel (controller power index, not dBm)
    tx_power_5g = Column(Integer, nullable=True)
    noise_2g = Column(Integer, nullable=True)       # bsnAPIfNoiseNow, dBm -- helps distinguish "no clients" from "RF is unusable here"
    noise_5g = Column(Integer, nullable=True)
    channel_util_2g = Column(Integer, nullable=True)  # bsnAPIfLoadChannelUtilization, percent
    channel_util_5g = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    polled_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("controller_device_id", "ap_index", name="uq_wireless_ap_controller_index"),
    )

    WIRELESS_AP_VENDORS = ["cisco", "aruba", "ruckus", "tplink", "ubiquiti", "mikrotik", "other"]

    def oper_status_label(self) -> str:
        if self.source == "manual" and self.oper_status is None:
            # Manually-added APs aren't SNMP-polled by default, so there's
            # no bsnAPOperationStatus to report -- show them as "managed"
            # rather than the misleading "unknown" a polled AP would get.
            return "managed"
        return {1: "associated", 2: "disassociating", 3: "downloading"}.get(
            self.oper_status, "unknown"
        )


class WirelessClientSession(Base):
    """Tracks which AP each wireless client is currently associated to,
    upserted on every poll from bsnMobileStationAPMacAddr (the same walk
    wireless_service.poll_wireless_controller already does for the
    per-band client split). One row per (controller, client MAC);
    ap_index/ap_mac_address get overwritten in place when a client
    roams, and first_seen_on_ap resets to the current poll time when
    that happens -- so "how long has this client been stuck on its
    current AP" is a single column read, not a scan of poll history.

    Purely a "most-recent state" table like WirelessAP itself -- no
    intent to keep roam history beyond what's needed to compute current
    dwell time; a client that stops appearing in a poll (walked out of
    range, powered off) just stops being refreshed rather than being
    explicitly deleted, and ages out of relevance on its own.
    """

    __tablename__ = "wireless_client_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    controller_device_id = Column(
        UUID(as_uuid=True),
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    client_mac = Column(String, nullable=False)
    ap_index = Column(String, nullable=True)   # current AP's bsnAPTable index, for joining to WirelessAP
    ap_mac_address = Column(String, nullable=True)
    band = Column(String, nullable=True)  # "2g" | "5g"

    first_seen_on_ap = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("controller_device_id", "client_mac", name="uq_client_session_controller_mac"),
    )


class WirelessSSID(Base):
    """One SSID (ESS / service set) profile configured on the WLC.

    admin_status maps to bsnDot11EssAdminStatus (1=enabled, 0=disabled).
    mobile_station_count is bsnDot11EssNumberOfMobileStations: the
    total number of clients currently associated to this SSID across
    all APs under the controller.
    """

    __tablename__ = "wireless_ssids"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    controller_device_id = Column(
        UUID(as_uuid=True),
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ssid_index = Column(String, nullable=False)

    ssid_name = Column(String, nullable=False)   # bsnDot11EssSsid
    admin_status = Column(Integer, nullable=True)  # 1 = enabled
    mobile_station_count = Column(Integer, nullable=True)  # bsnDot11EssNumberOfMobileStations

    polled_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("controller_device_id", "ssid_index", name="uq_wireless_ssid_controller_index"),
    )
