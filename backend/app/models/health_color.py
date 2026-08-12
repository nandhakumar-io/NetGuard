import enum


class HealthColor(str, enum.Enum):
    """Traffic-light classification for a device's SNMP health score.

    Previously a Postgres Enum column on the (now retired) DeviceMetric
    table; health_color now lives in VictoriaMetrics as a
    netguard_device_health_color info-metric label (see
    app.core.vm_client), so this is a plain shared value type rather than
    a SQLAlchemy model concern.
    """

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"
    # A poll that got a response (sysUpTime answered -- device is genuinely
    # reachable) but resolved *zero* of the actual health OIDs (CPU/mem/
    # temp/fan/power/interface-util all None) -- e.g. a lab/virtual image
    # that doesn't implement the hardware-sensor MIBs at all. Previously
    # this fell through to the "no readings -> 100/green" default, which
    # rendered as a confident, fully-green "100/100" for a device we
    # actually know nothing about -- worse than not showing a score.
    GRAY = "gray"
