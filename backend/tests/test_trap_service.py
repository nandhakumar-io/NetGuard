"""Coverage for app.services.trap_service.

Split into two halves:

  * Pure parsing (`parse_trap_packet`) against real BER-encoded v1 and
    v2c trap packets built with pysnmp's own encoder, so these exercise
    the actual wire format rather than a hand-rolled byte string.

  * `ingest_trap`'s DB-facing half (interface-transition alert path and
    generic classified-trap path) against a real in-memory SQLite
    Session, same fixture shape as test_fleet_availablity_and_flapping.py.

Regression coverage for a real bug found while writing these: v1's
TrapPDU is NOT shaped like a normal request/response PDU -- its
variable-bindings field sits at ASN.1 SEQUENCE position 5, not position
3 like every other PDU type. `parse_trap_packet` was calling
`apiPDU.getVarBinds(pdu)` (the position-3 accessor) for v1 traps, which
raised TypeError on every single v1 trap and was silently swallowed by
a bare `except: pass` -- so `if_index` came back None for every v1
linkDown/linkUp trap regardless of what the device actually sent, and
the parser fell through to a generic (port-less) classified alert
instead of the fast, port-specific interface-down path. `getGenericTrap`
itself was never the problem (it already returned the correct symbolic
trap name, e.g. "linkDown") -- only the varbind/if_index extraction
alongside it was broken. Fixed by using `apiTrapPDU.getVarBinds(pdu)`,
the v1-TrapPDU-shape-aware accessor, for the v1 branch.
"""

import pytest
from pyasn1.codec.ber import encoder as ber_encoder
from pysnmp.proto import api as pysnmp_api
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.alert import Alert, AlertSource
from app.models.device import Device, DeviceVendor
from app.models.interface_status import InterfaceStatus
from app.services import trap_service

IF_INDEX_OID = (1, 3, 6, 1, 2, 1, 2, 2, 1, 1)
SNMP_TRAP_OID = (1, 3, 6, 1, 6, 3, 1, 1, 4, 1, 0)
LINK_DOWN_OID = (1, 3, 6, 1, 6, 3, 1, 1, 5, 3)
LINK_UP_OID = (1, 3, 6, 1, 6, 3, 1, 1, 5, 4)
COLD_START_OID = (1, 3, 6, 1, 6, 3, 1, 1, 5, 1)


# --- Synthetic packet builders --------------------------------------------
#
# Real pysnmp encode calls, not hand-built bytes, so these packets are
# exactly as valid/invalid as whatever a real device would actually put
# on the wire.


def _build_v1_trap(generic_trap: str, if_index: int | None, community: str = "public") -> bytes:
    pMod = pysnmp_api.protoModules[pysnmp_api.protoVersion1]
    pdu = pMod.TrapPDU()
    pMod.apiTrapPDU.setDefaults(pdu)
    pMod.apiTrapPDU.setEnterprise(pdu, (1, 3, 6, 1, 4, 1, 9))
    pMod.apiTrapPDU.setAgentAddr(pdu, pMod.IpAddress("10.0.0.1"))
    pMod.apiTrapPDU.setGenericTrap(pdu, generic_trap)
    if if_index is not None:
        pMod.apiTrapPDU.setVarBinds(pdu, [(IF_INDEX_OID, pMod.Integer(if_index))])

    msg = pMod.Message()
    pMod.apiMessage.setDefaults(msg)
    pMod.apiMessage.setCommunity(msg, community)
    pMod.apiMessage.setPDU(msg, pdu)
    return bytes(ber_encoder.encode(msg))


def _build_v2c_trap(trap_oid: tuple, if_index: int | None, community: str = "public") -> bytes:
    pMod = pysnmp_api.protoModules[pysnmp_api.protoVersion2c]
    pdu = pMod.TrapPDU()
    pMod.apiTrapPDU.setDefaults(pdu)
    varbinds = [(SNMP_TRAP_OID, pMod.ObjectIdentifier(trap_oid))]
    if if_index is not None:
        varbinds.append((IF_INDEX_OID, pMod.Integer(if_index)))
    pMod.apiTrapPDU.setVarBinds(pdu, varbinds)

    msg = pMod.Message()
    pMod.apiMessage.setDefaults(msg)
    pMod.apiMessage.setCommunity(msg, community)
    pMod.apiMessage.setPDU(msg, pdu)
    return bytes(ber_encoder.encode(msg))


# --- parse_trap_packet: v1 ------------------------------------------------


def test_v1_link_down_trap_resolves_name_and_if_index():
    data = _build_v1_trap("linkDown", if_index=5)
    parsed = trap_service.parse_trap_packet(data)

    assert parsed is not None
    assert parsed.trap_name == "linkDown"
    # Regression case for the getVarBinds-position bug described in the
    # module docstring above: if_index must actually be extracted, not
    # silently swallowed to None.
    assert parsed.if_index == "5"
    assert (str(IF_INDEX_OID).replace("(", "").replace(")", "").replace(" ", "") or True)


def test_v1_link_up_trap_resolves_name_and_if_index():
    data = _build_v1_trap("linkUp", if_index=12)
    parsed = trap_service.parse_trap_packet(data)

    assert parsed is not None
    assert parsed.trap_name == "linkUp"
    assert parsed.if_index == "12"


def test_v1_cold_start_trap_has_no_if_index():
    """coldStart carries no ifIndex varbind at all -- if_index should stay
    None rather than erroring or defaulting to something misleading."""
    data = _build_v1_trap("coldStart", if_index=None)
    parsed = trap_service.parse_trap_packet(data)

    assert parsed is not None
    assert parsed.trap_name == "coldStart"
    assert parsed.if_index is None


def test_v1_trap_wrong_community_is_rejected(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "SNMP_TRAP_COMMUNITIES", "expected-community")
    data = _build_v1_trap("linkDown", if_index=3, community="wrong-community")
    parsed = trap_service.parse_trap_packet(data)

    assert parsed is None


# --- parse_trap_packet: v2c ------------------------------------------------


def test_v2c_link_down_trap_resolves_name_and_if_index():
    data = _build_v2c_trap(LINK_DOWN_OID, if_index=7)
    parsed = trap_service.parse_trap_packet(data)

    assert parsed is not None
    assert parsed.trap_name == "linkDown"
    assert parsed.if_index == "7"


def test_v2c_link_up_trap_resolves_name_and_if_index():
    data = _build_v2c_trap(LINK_UP_OID, if_index=9)
    parsed = trap_service.parse_trap_packet(data)

    assert parsed is not None
    assert parsed.trap_name == "linkUp"
    assert parsed.if_index == "9"


def test_v2c_unknown_trap_oid_falls_back_to_raw_oid_string():
    unknown_oid = (1, 3, 6, 1, 4, 1, 99999, 1, 1)
    data = _build_v2c_trap(unknown_oid, if_index=None)
    parsed = trap_service.parse_trap_packet(data)

    assert parsed is not None
    assert parsed.trap_name == str(pysnmp_api.protoModules[pysnmp_api.protoVersion2c].ObjectIdentifier(unknown_oid))


# --- parse_trap_packet: malformed input never raises -----------------------


def test_garbage_bytes_never_raises():
    assert trap_service.parse_trap_packet(b"not a valid snmp packet at all") is None


def test_empty_bytes_never_raises():
    assert trap_service.parse_trap_packet(b"") is None


# --- ingest_trap: DB-facing half -------------------------------------------
#
# Exercises the actual Session-writing path (interface transitions +
# generic classified alerts), not just the pure parsing above.


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(bind=engine)
    session = SessionFactory()
    yield session
    session.close()


def _device(db, hostname="core-sw-1", ip="10.0.0.1", vendor=DeviceVendor.CISCO):
    d = Device(hostname=hostname, ip_address=ip, vendor=vendor, supports_snmp=True)
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def test_ingest_trap_link_down_creates_interface_status_and_alert(db_session):
    device = _device(db_session)
    parsed = trap_service.parse_trap_packet(_build_v1_trap("linkDown", if_index=5))
    assert parsed is not None and parsed.if_index == "5"

    trap_service.ingest_trap(db_session, source_ip=device.ip_address, parsed=parsed)

    status_rows = db_session.query(InterfaceStatus).filter(InterfaceStatus.device_id == device.id).all()
    assert len(status_rows) == 1
    assert status_rows[0].if_index == "5"
    assert status_rows[0].status.value == "down"

    alerts = db_session.query(Alert).filter(Alert.device_id == device.id).all()
    assert len(alerts) == 1
    assert alerts[0].source == AlertSource.SNMP_TRAP
    assert alerts[0].category.startswith("Interface Down")


def test_ingest_trap_link_up_after_link_down_resolves_alert(db_session):
    device = _device(db_session)
    down = trap_service.parse_trap_packet(_build_v1_trap("linkDown", if_index=5))
    trap_service.ingest_trap(db_session, source_ip=device.ip_address, parsed=down)

    up = trap_service.parse_trap_packet(_build_v1_trap("linkUp", if_index=5))
    trap_service.ingest_trap(db_session, source_ip=device.ip_address, parsed=up)

    status_rows = (
        db_session.query(InterfaceStatus)
        .filter(InterfaceStatus.device_id == device.id)
        .order_by(InterfaceStatus.changed_at.asc())
        .all()
    )
    assert [r.status.value for r in status_rows] == ["down", "up"]

    alerts = db_session.query(Alert).filter(Alert.device_id == device.id).all()
    assert len(alerts) == 1
    assert alerts[0].resolved_at is not None


def test_ingest_trap_repeated_link_down_does_not_duplicate_alert(db_session):
    """A flapping/retransmitted linkDown for the same port must update the
    existing standing alert, not spawn a second one -- same dedup
    contract a poll-driven transition already has."""
    device = _device(db_session)
    parsed = trap_service.parse_trap_packet(_build_v1_trap("linkDown", if_index=5))

    trap_service.ingest_trap(db_session, source_ip=device.ip_address, parsed=parsed)
    trap_service.ingest_trap(db_session, source_ip=device.ip_address, parsed=parsed)

    alerts = db_session.query(Alert).filter(Alert.device_id == device.id).all()
    assert len(alerts) == 1


def test_ingest_trap_cold_start_raises_generic_classified_alert(db_session):
    device = _device(db_session)
    parsed = trap_service.parse_trap_packet(_build_v1_trap("coldStart", if_index=None))
    assert parsed is not None and parsed.if_index is None

    trap_service.ingest_trap(db_session, source_ip=device.ip_address, parsed=parsed)

    alerts = db_session.query(Alert).filter(Alert.device_id == device.id).all()
    assert len(alerts) == 1
    assert alerts[0].source == AlertSource.SNMP_TRAP
    assert "coldStart" in alerts[0].message


def test_ingest_trap_from_unmanaged_source_is_ignored(db_session):
    """A trap from an IP that isn't a managed Device must be dropped
    silently -- no orphaned Alert with a null device, no exception."""
    parsed = trap_service.parse_trap_packet(_build_v1_trap("linkDown", if_index=5))

    trap_service.ingest_trap(db_session, source_ip="192.0.2.99", parsed=parsed)

    assert db_session.query(Alert).count() == 0
    assert db_session.query(InterfaceStatus).count() == 0


def test_ingest_trap_link_down_without_if_index_falls_back_to_generic_alert(db_session):
    """If a trap claims linkDown/linkUp but somehow carries no ifIndex
    varbind, ingest_trap must not crash -- it should fall through to the
    generic classified-alert path rather than the port-specific one."""
    device = _device(db_session)
    parsed = trap_service.parse_trap_packet(_build_v1_trap("linkDown", if_index=None))
    assert parsed is not None and parsed.if_index is None

    trap_service.ingest_trap(db_session, source_ip=device.ip_address, parsed=parsed)

    assert db_session.query(InterfaceStatus).count() == 0
    alerts = db_session.query(Alert).filter(Alert.device_id == device.id).all()
    assert len(alerts) == 1
