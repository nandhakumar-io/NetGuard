"""Coverage for app.services.syslog_service parsing/correlation logic --
the pure-function parts that don't need a DB. Ingest/persistence/alert
dedup integration is exercised indirectly wherever alert_service itself
is tested; this file is scoped to "does the parser and rule table do the
right thing with real-shaped vendor syslog lines".
"""
from app.models.syslog_message import SyslogSeverity
from app.services import syslog_service


def test_parses_rfc3164_cisco_line():
    line = "<189>Aug  5 10:15:22 core-sw-1 %LINK-3-UPDOWN: Interface GigabitEthernet0/1, changed state to down"
    parsed = syslog_service.parse_syslog_line(line)

    # PRI 189 = facility 23 (local7) * 8 + severity 5 (notice)
    assert parsed.facility == 23
    assert parsed.severity == SyslogSeverity.NOTICE
    assert parsed.hostname == "core-sw-1"
    assert "UPDOWN" in (parsed.tag or "") or "UPDOWN" in parsed.message
    assert "GigabitEthernet0/1" in parsed.message
    assert parsed.device_reported_at is not None
    assert parsed.device_reported_at.month == 8 and parsed.device_reported_at.day == 5


def test_parses_rfc5424_line():
    line = (
        "<165>1 2026-08-05T10:15:22.003Z router1.example.com sshd 4123 ID47 - "
        "Failed password for invalid user admin from 203.0.113.7 port 51710 ssh2"
    )
    parsed = syslog_service.parse_syslog_line(line)

    assert parsed.facility == 20  # 165 // 8
    assert parsed.severity == SyslogSeverity.NOTICE  # 165 % 8 == 5
    assert parsed.hostname == "router1.example.com"
    assert parsed.tag == "sshd"
    assert "Failed password" in parsed.message
    assert parsed.device_reported_at is not None


def test_malformed_line_never_raises_and_is_still_captured():
    line = "this is not a valid syslog line at all"
    parsed = syslog_service.parse_syslog_line(line)

    assert parsed.message == line
    assert parsed.severity == SyslogSeverity.INFORMATIONAL
    assert parsed.hostname is None


def test_pri_only_line_extracts_facility_and_severity_without_full_structure():
    line = "<134>some free-form text an appliance emitted without a timestamp"
    parsed = syslog_service.parse_syslog_line(line)

    # 134 // 8 = 16 (local0), 134 % 8 = 6 (informational)
    assert parsed.facility == 16
    assert parsed.severity == SyslogSeverity.INFORMATIONAL
    assert "free-form text" in parsed.message


def test_correlation_rules_match_expected_categories():
    """Each rule fires on the vendor-realistic text it names in its own
    docstring -- pinned individually so a future regex tweak that breaks
    one vendor's phrasing doesn't silently stop matching without a test
    catching it, same rationale as the SNMP per-vendor regression file.
    """
    cases = [
        ("Auth Failure", "sshd: Failed password for invalid user root from 10.0.0.9"),
        ("Auth Failure", "%SEC_LOGIN-4-FAILED: Login failed [user: admin] [Source: 10.0.0.5]"),
        ("ACL Deny", "%SEC-6-IPACCESSLOGP: list OUTSIDE-IN denied tcp 203.0.113.5(1025) -> 10.0.0.1(443)"),
        ("Hardware Error", "%PLATFORM-1-FAULT: Power supply 1 has failed"),
        ("Interface Down", "%LINEPROTO-5-UPDOWN: Line protocol on Interface Gi0/1, changed state to down"),
        ("Routing Adjacency Change", "%BGP-5-ADJCHANGE: neighbor 10.0.0.2 Down BGP Notification sent"),
        ("Config Changed", "%SYS-5-CONFIG_I: Configured from console by admin on vty0"),
    ]
    for expected_category, text in cases:
        match = next(
            (category for category, _sev, pattern in syslog_service.CORRELATION_RULES if pattern.search(text)),
            None,
        )
        assert match == expected_category, f"expected {expected_category!r} for {text!r}, got {match!r}"


def test_routine_informational_text_matches_no_rule():
    """Plain routine traffic (the overwhelming majority of real syslog
    volume) must not spuriously match any correlation rule -- a rule
    table that's too eager would turn every syslog line into an alert,
    defeating the whole point of correlation."""
    text = "system: Interface counters cleared by admin"
    match = next(
        (category for category, _sev, pattern in syslog_service.CORRELATION_RULES if pattern.search(text)),
        None,
    )
    assert match is None
