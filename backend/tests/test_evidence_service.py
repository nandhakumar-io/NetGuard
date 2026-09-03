"""Evidence service unit tests.

Tests the deterministic SHA-256 canonical-JSON hashing contract that the
entire tamper-detection story (Section 24) depends on.  These tests run
entirely in-process with no DB, no Fabric, and no mocking.
"""
import uuid

from app.services.evidence_service import (
    build_evidence,
    hash_config,
    hash_evidence,
    verify_evidence,
)

# ---------------------------------------------------------------------------
# hash_config
# ---------------------------------------------------------------------------


def test_hash_config_is_deterministic():
    config = "interface GigabitEthernet0/1\n ip address 10.0.0.1 255.255.255.0"
    assert hash_config(config) == hash_config(config)


def test_hash_config_changes_on_any_mutation():
    config = "interface GigabitEthernet0/1\n ip address 10.0.0.1 255.255.255.0"
    modified = config + "\n!"
    assert hash_config(config) != hash_config(modified)


def test_hash_config_has_sha256_prefix():
    h = hash_config("hostname router1")
    assert h.startswith("sha256:")
    assert len(h) == len("sha256:") + 64


def test_hash_config_empty_string_stable():
    """An empty config should hash to sha256 of empty string, not raise."""
    h = hash_config("")
    assert h.startswith("sha256:")


def test_hash_config_whitespace_both_ends_stripped():
    """Leading/trailing whitespace is NOT stripped by hash_config (it's raw).
    Confirm that adding a newline DOES change the hash so callers don't
    silently normalize before passing in."""
    base = "hostname router1\n!"
    with_extra = base + "\n"
    # hash_config hashes raw bytes, so these differ
    assert hash_config(base) != hash_config(with_extra)


# ---------------------------------------------------------------------------
# build_evidence + hash_evidence
# ---------------------------------------------------------------------------


def _ev(**overrides):
    """Build a minimal evidence body dict."""
    defaults = dict(
        evidence_type="CHANGE_VALIDATION",
        evidence_id=str(uuid.uuid4()),
        change_request_id="cr-1",
        device_id="dev-1",
        timestamp="2026-09-02T13:00:00+00:00",
        actor_subject="admin@example.com",
        fields={"result": "pass"},
    )
    defaults.update(overrides)
    return build_evidence(**defaults)


def test_build_evidence_returns_dict():
    body = _ev()
    assert isinstance(body, dict)


def test_same_inputs_same_hash():
    eid = str(uuid.uuid4())
    body1 = _ev(evidence_id=eid)
    body2 = _ev(evidence_id=eid)
    assert hash_evidence(body1) == hash_evidence(body2)


def test_key_order_does_not_affect_hash():
    """sort_keys=True contract: dict insert order must not matter."""
    eid = str(uuid.uuid4())
    body1 = _ev(evidence_id=eid, fields={"a": 1, "b": 2})
    body2 = _ev(evidence_id=eid, fields={"b": 2, "a": 1})
    assert hash_evidence(body1) == hash_evidence(body2)


def test_changed_field_changes_hash():
    eid = str(uuid.uuid4())
    body1 = _ev(evidence_id=eid, fields={"result": "pass"})
    body2 = _ev(evidence_id=eid, fields={"result": "block"})
    assert hash_evidence(body1) != hash_evidence(body2)


def test_tampered_change_request_id_changes_hash():
    eid = str(uuid.uuid4())
    body1 = _ev(evidence_id=eid, change_request_id="cr-1")
    body2 = _ev(evidence_id=eid, change_request_id="cr-2")
    assert hash_evidence(body1) != hash_evidence(body2)


def test_hash_evidence_has_sha256_prefix():
    body = _ev()
    h = hash_evidence(body)
    assert h.startswith("sha256:")


# ---------------------------------------------------------------------------
# verify_evidence
# ---------------------------------------------------------------------------


def test_verify_evidence_match():
    eid = str(uuid.uuid4())
    body = _ev(evidence_id=eid)
    h = hash_evidence(body)
    verified, calculated = verify_evidence(body, h)
    assert verified is True
    assert calculated == h


def test_verify_evidence_mismatch_on_tampered_body():
    eid = str(uuid.uuid4())
    body = _ev(evidence_id=eid, fields={"result": "pass"})
    h = hash_evidence(body)
    # Tamper the body in-place
    tampered = {**body, "result": "block"}
    verified, _ = verify_evidence(tampered, h)
    assert verified is False


def test_verify_evidence_mismatch_on_wrong_stored_hash():
    body = _ev()
    wrong_hash = "sha256:" + "a" * 64
    verified, calculated = verify_evidence(body, wrong_hash)
    assert verified is False
    # calculated should be a valid sha256: prefixed string
    assert calculated.startswith("sha256:")


def test_verify_evidence_returns_calculated_hash_on_match():
    """The returned calculated hash must equal the stored hash on a match
    (useful for the verification API to surface the re-computed value)."""
    eid = str(uuid.uuid4())
    body = _ev(evidence_id=eid)
    h = hash_evidence(body)
    verified, calculated = verify_evidence(body, h)
    assert verified is True
    assert calculated == h
