"""Per-vendor regression fixtures for app.services.snmp_service.poll_health.

Today's Juniper bug (CPU/memory/temperature all reading 0%) happened because
jnxOperatingTable holds every hardware component -- chassis, PSUs, fan trays,
FPCs/PICs, AND the Routing Engine -- as rows in one table, and the old code
picked the lowest numeric row index instead of the row whose description
actually says "Routing Engine". Nothing here is vendor-neutral: a fixture
that only exercises the single-row/GET-based Cisco path would never have
caught it. Each vendor gets its own fixture below so a future MIB change to
one vendor's poll path can't silently reintroduce a bug for another vendor.

Everything is driven off `_walk` / `_get_via_pysnmp`, monkeypatched to return
canned per-OID tables shaped like real device output (row index -> value),
rather than hitting the network -- these are the same two seams poll_health
itself calls through for every vendor.
"""


from app.services import snmp_service
from app.services.snmp_service import SnmpAuthConfig, poll_health


class _FakeAgent:
    """Canned per-OID responses for one simulated device.

    `gets` answers scalar OIDs (sysUpTime, sysDescr, ...).
    `walks` answers table-column base OIDs with an {index: value} dict,
    matching whatever _walk(base_oid) would return off a real agent.
    """

    def __init__(self, gets: dict[str, str], walks: dict[str, dict[str, str]]):
        self.gets = gets
        self.walks = walks

    def get(self, ip_address, auth, oid, timeout):
        return self.gets.get(oid)

    def walk(self, ip_address, auth, base_oid, timeout):
        return self.walks.get(base_oid, {})


def _patch_agent(monkeypatch, agent: _FakeAgent):
    monkeypatch.setattr(snmp_service, "_get_via_pysnmp", agent.get)
    monkeypatch.setattr(snmp_service, "_walk", agent.walk)
    # No interfaces in these fixtures -- CPU/mem/temp/fan/power is what's
    # under test here, not the interface table.
    monkeypatch.setattr(
        snmp_service,
        "walk_interface_stats",
        lambda ip_address, auth, timeout=3.0: {
            "errors": None, "octets_total": None, "speed_bps": None, "interface_count": None,
            "per_interface": [],
        },
    )


_AUTH = SnmpAuthConfig(version="v2c", community="public", port=161)


def test_juniper_mx_picks_routing_engine_row_not_lowest_index(monkeypatch):
    """Reproduces today's bug on MX/SRX-class Juniper gear: row "0"
    (chassis/backplane) is numerically lowest but correctly reports 0 for
    everything, since CPU/buffer/temp don't apply to it. The Routing
    Engine is row "2". Matching by description must return row "2"'s
    values, not row "0"'s.
    """
    descr = snmp_service.JUNIPER_OIDS["descr"]
    cpu = snmp_service.JUNIPER_OIDS["cpu"]
    buffer_ = snmp_service.JUNIPER_OIDS["buffer"]
    temp = snmp_service.JUNIPER_OIDS["temperature"]
    state = snmp_service.JUNIPER_OIDS["state"]

    agent = _FakeAgent(
        gets={snmp_service.OIDS["sysUpTime"]: "12345600"},
        walks={
            descr: {"0": "Backplane", "1": "Power Supply 0", "2": "Routing Engine 0", "3": "Fan Tray 0"},
            cpu: {"0": "0", "1": "0", "2": "37", "3": "0"},
            buffer_: {"0": "0", "1": "0", "2": "62", "3": "0"},
            temp: {"0": "0", "1": "0", "2": "41", "3": "0"},
            state: {"0": "2", "1": "2", "2": "2", "3": "2"},
        },
    )
    _patch_agent(monkeypatch, agent)

    result = poll_health("10.0.0.1", _AUTH, vendor="juniper")

    assert result.cpu_utilization_pct == 37
    assert result.memory_utilization_pct == 62
    assert result.temperature_celsius == 41


def test_juniper_ex_series_matches_pem_not_just_power_supply(monkeypatch):
    """Reproduces the EX3300/EX3400 power_supply_status bug: those
    platforms label the row "PEM 0" instead of "Power Supply 0", so
    matching only on "Power Supply" must fall through to the "PEM"
    keyword rather than returning unknown.
    """
    descr = snmp_service.JUNIPER_OIDS["descr"]
    cpu = snmp_service.JUNIPER_OIDS["cpu"]
    buffer_ = snmp_service.JUNIPER_OIDS["buffer"]
    temp = snmp_service.JUNIPER_OIDS["temperature"]
    state = snmp_service.JUNIPER_OIDS["state"]

    agent = _FakeAgent(
        gets={snmp_service.OIDS["sysUpTime"]: "8640000"},
        walks={
            descr: {"0": "Routing Engine 0", "1": "PEM 0", "2": "Fan 0"},
            cpu: {"0": "12", "1": "0", "2": "0"},
            buffer_: {"0": "48", "1": "0", "2": "0"},
            temp: {"0": "33", "1": "0", "2": "0"},
            state: {"0": "2", "1": "2", "2": "2"},
        },
    )
    _patch_agent(monkeypatch, agent)

    result = poll_health("10.0.0.2", _AUTH, vendor="juniper")

    assert result.cpu_utilization_pct == 12
    assert result.power_supply_status == "ok"


def test_juniper_falls_back_to_lowest_index_when_no_re_row_labeled(monkeypatch):
    """A platform that never says "Routing Engine" verbatim (single-RE box
    with an odd label) must still resolve something via the lowest-index
    fallback, rather than coming back with no CPU/memory reading at all.
    """
    descr = snmp_service.JUNIPER_OIDS["descr"]
    cpu = snmp_service.JUNIPER_OIDS["cpu"]
    buffer_ = snmp_service.JUNIPER_OIDS["buffer"]
    temp = snmp_service.JUNIPER_OIDS["temperature"]
    state = snmp_service.JUNIPER_OIDS["state"]

    agent = _FakeAgent(
        gets={snmp_service.OIDS["sysUpTime"]: "100"},
        walks={
            descr: {"0": "srxRE"},
            cpu: {"0": "5"},
            buffer_: {"0": "20"},
            temp: {"0": "30"},
            state: {"0": "2"},
        },
    )
    _patch_agent(monkeypatch, agent)

    result = poll_health("10.0.0.3", _AUTH, vendor="juniper")

    assert result.cpu_utilization_pct == 5
    assert result.memory_utilization_pct == 20


def test_cisco_uses_lowest_table_index_directly(monkeypatch):
    """Cisco (non-Juniper) path has no per-component table to disambiguate
    -- cpmCPUTotalTable etc. are keyed only by CPU/pool index, so the
    lowest-index row is the correct (and only sane) choice. This fixture
    pins that behavior so a future refactor that tries to generalize the
    Juniper description-matching path doesn't accidentally change Cisco's
    simpler, correct behavior.
    """
    agent = _FakeAgent(
        gets={snmp_service.OIDS["sysUpTime"]: "5000000"},
        walks={
            snmp_service.OIDS["cpu_5min"]: {"7": "22"},  # real device: only row is index 7, not 1
            snmp_service.OIDS["mem_used"]: {"1": "400000"},
            snmp_service.OIDS["mem_free"]: {"1": "600000"},
            snmp_service.OIDS["temperature"]: {"1": "45"},
            snmp_service.OIDS["fan_state"]: {"1": "1"},
            snmp_service.OIDS["power_supply_state"]: {"1": "1"},
        },
    )
    _patch_agent(monkeypatch, agent)

    result = poll_health("10.0.0.4", _AUTH, vendor="cisco")

    assert result.cpu_utilization_pct == 22
    assert result.memory_utilization_pct == 40.0  # 400000 / (400000+600000) * 100


def test_cisco_falls_back_to_vendor_neutral_cpu_oid_when_primary_missing(monkeypatch):
    """Older Cisco images without cpmCPUTotal5minRev must fall through to
    CPU_FALLBACK_OIDS rather than reporting no CPU reading at all.
    """
    scalar_fallback = next(oid for oid in snmp_service.CPU_FALLBACK_OIDS if oid.endswith(".0"))

    agent = _FakeAgent(
        gets={
            snmp_service.OIDS["sysUpTime"]: "1000",
            scalar_fallback: "17",
        },
        walks={
            snmp_service.OIDS["cpu_5min"]: {},  # not implemented on this image
            snmp_service.OIDS["mem_used"]: {"1": "100"},
            snmp_service.OIDS["mem_free"]: {"1": "900"},
        },
    )
    _patch_agent(monkeypatch, agent)

    result = poll_health("10.0.0.5", _AUTH, vendor="cisco")

    assert result.cpu_utilization_pct == 17


def test_cisco_stack_default_still_uses_lowest_index(monkeypatch):
    """stack_aware defaults to False -- a stacked switch's CPU/memory
    table should still pick the lowest-index row unless the caller (a
    Device with snmp_stack_aware=True) explicitly opts in. Guards against
    the new stack_aware branch accidentally becoming the default and
    silently changing every existing Cisco device's health reading.
    """
    agent = _FakeAgent(
        gets={snmp_service.OIDS["sysUpTime"]: "1000"},
        walks={
            snmp_service.OIDS["cpu_5min"]: {"1": "10", "2": "95"},  # member 1 idle, member 2 pegged
            snmp_service.OIDS["mem_used"]: {"1": "100", "2": "950"},
            snmp_service.OIDS["mem_free"]: {"1": "900", "2": "50"},
        },
    )
    _patch_agent(monkeypatch, agent)

    result = poll_health("10.0.0.6", _AUTH, vendor="cisco")  # stack_aware not passed -> False

    assert result.cpu_utilization_pct == 10
    assert result.memory_utilization_pct == 10.0


def test_cisco_stack_aware_reports_worst_member_not_lowest_index(monkeypatch):
    """snmp_stack_aware=True: a stacked switch's per-member CPU/memory
    table should surface the WORST member, not whichever has the lowest
    stack-member index -- see Device.snmp_stack_aware docstring. Member 2
    here is pegged at 95% CPU / 95% memory while member 1 (lowest index)
    sits idle; the old unconditional lowest-index behavior would report
    member 1's idle numbers and hide the real problem.
    """
    agent = _FakeAgent(
        gets={snmp_service.OIDS["sysUpTime"]: "1000"},
        walks={
            snmp_service.OIDS["cpu_5min"]: {"1": "10", "2": "95"},
            snmp_service.OIDS["mem_used"]: {"1": "100", "2": "950"},
            snmp_service.OIDS["mem_free"]: {"1": "900", "2": "50"},
        },
    )
    _patch_agent(monkeypatch, agent)

    result = poll_health("10.0.0.6", _AUTH, vendor="cisco", stack_aware=True)

    assert result.cpu_utilization_pct == 95
    assert result.memory_utilization_pct == 95.0


def test_cisco_stack_aware_pairs_used_free_by_matching_index(monkeypatch):
    """_get_worst_mem_pct must compute each row's percentage from ITS OWN
    used+free pair before comparing rows -- not max(used) and max(free)
    taken independently, which would pair two different stack members'
    numbers into a meaningless percentage. Here member 1 has the highest
    raw `used` value but a low percentage (huge pool); member 2 has a
    smaller pool that's nearly full -- member 2's percentage must win.
    """
    agent = _FakeAgent(
        gets={snmp_service.OIDS["sysUpTime"]: "1000"},
        walks={
            snmp_service.OIDS["cpu_5min"]: {"1": "5"},
            snmp_service.OIDS["mem_used"]: {"1": "5000", "2": "90"},   # member 1: 5000 used
            snmp_service.OIDS["mem_free"]: {"1": "95000", "2": "10"},  # member 1: 5% used; member 2: 90% used
        },
    )
    _patch_agent(monkeypatch, agent)

    result = poll_health("10.0.0.7", _AUTH, vendor="cisco", stack_aware=True)

    assert result.memory_utilization_pct == 90.0
    assert result.memory_utilization_pct != 50.0  # would be max(5000,90)/(max+max) style nonsense
