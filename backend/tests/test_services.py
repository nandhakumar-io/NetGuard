from app.services import risk_engine, validation_engine, diff_engine


def test_risk_engine_low_risk():
    config = "interface Gi0/1\n ip address 10.2.2.1 255.255.255.0\n"
    result = risk_engine.analyze(config)
    assert result.risk_score <= 30
    assert result.classification == "Low Risk"


def test_risk_engine_critical_risk():
    config = "router bgp 65000\n no neighbor 10.0.0.1\n no router ospf 1\n shutdown\n"
    result = risk_engine.analyze(config)
    assert result.risk_score > 70
    assert result.classification == "Critical Risk"


def test_validation_engine_rejects_empty_config():
    result = validation_engine.validate_syntax("")
    assert result.passed is False
    assert "empty" in result.errors[0].lower()


def test_validation_engine_accepts_valid_config():
    config = "interface Gi0/1\n ip address 10.2.2.1 255.255.255.0\n"
    result = validation_engine.validate_syntax(config)
    assert result.passed is True


def test_diff_engine_produces_unified_diff():
    current = "interface Gi0/1\n ip address 10.1.1.1\n"
    proposed = "interface Gi0/1\n ip address 10.2.2.1\n"
    diff = diff_engine.generate_diff(current, proposed)
    assert "-  ip address 10.1.1.1".strip("-") in diff or "10.1.1.1" in diff
    assert "10.2.2.1" in diff
