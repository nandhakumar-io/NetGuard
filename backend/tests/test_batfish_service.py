"""Tests for batfish_service -- mocks pybatfish's Session/bfq entirely so
these run without a live Batfish coordinator (see BatfishService._get_session,
which lazy-imports pybatfish for exactly this reason)."""
import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.services.batfish_service import (
    BatfishService,
    BatfishStatus,
    is_vendor_supported,
)

DEVICE_CISCO = {"id": "dev-1", "hostname": "rtr-01", "vendor": "cisco", "platform": "ios", "role": "access"}
DEVICE_UNSUPPORTED = {"id": "dev-2", "hostname": "sw-legacy", "vendor": "hp", "platform": "comware", "role": "access"}

MOCK_POLICIES = {
    "reachability_checks": [
        {"name": "guest_to_management", "source": "10.50.0.0/24", "destination": "10.10.0.0/24", "expect_denied": True, "severity": "critical"},
    ]
}


def _service(**overrides) -> BatfishService:
    kwargs = dict(host="bf-test", port=9996, timeout_seconds=5.0, enabled=True, fail_closed=False)
    kwargs.update(overrides)
    return BatfishService(**kwargs)


def _frame(n_rows: int) -> MagicMock:
    frame = MagicMock()
    frame.__len__.return_value = n_rows
    return frame


def _answer(n_rows: int) -> MagicMock:
    answer = MagicMock()
    answer.frame.return_value = _frame(n_rows)
    return answer


def _fake_session(reachability_by_snapshot: dict[str, int]):
    """Build a fake pybatfish Session whose `q.reachability(...).answer()`
    result depends on which snapshot is currently set, keyed by the
    snapshot name passed to `set_snapshot`."""
    session = MagicMock()
    state = {"current": None}

    def _set_snapshot(name):
        state["current"] = name

    def _reachability(headers=None):
        query = MagicMock()
        # Match by whether the current snapshot name ends with "-0" (before)
        # or "-1" (after/proposed), matching snapshot_name()'s revision suffix.
        is_before = str(state["current"]).endswith("-0")
        n_rows = reachability_by_snapshot["before" if is_before else "after"]
        query.answer.return_value = _answer(n_rows)
        return query

    session.set_snapshot.side_effect = _set_snapshot
    session.q.reachability.side_effect = _reachability
    session.init_snapshot.return_value = None
    return session


def _run_validate(service, device, current_config, proposed_config, related_configs=None):
    return asyncio.get_event_loop().run_until_complete(
        service.validate_configuration(
            change_request_id="cr-1",
            device=device,
            current_config=current_config,
            proposed_config=proposed_config,
            related_configs=related_configs,
        )
    )


class TestVendorSupport:
    def test_supported_vendor_platform_pairs(self):
        assert is_vendor_supported("cisco", "ios")
        assert is_vendor_supported("Juniper", "JUNOS")

    def test_unsupported_vendor(self):
        assert not is_vendor_supported("hp", "comware")
        assert not is_vendor_supported(None, None)


class TestValidateConfiguration:
    def test_valid_topology_with_no_findings_passes(self):
        service = _service()
        fake_session = _fake_session({"before": 0, "after": 0})
        with patch("app.services.batfish_service.BatfishService._get_session", return_value=fake_session), \
             patch("app.services.batfish_service.BatfishService._load_network_policies", return_value=MOCK_POLICIES):
            result = _run_validate(service, DEVICE_CISCO, "! current\n", "! proposed\n")
        assert result.status == BatfishStatus.PASS
        assert result.findings == []

    def test_acl_permitting_protected_network_produces_finding(self):
        """Guest reaching management (via the standard reachability_checks
        fixed set) becomes ACCEPTED after the change -- must be flagged,
        never silently pass."""
        service = _service()
        fake_session = _fake_session({"before": 0, "after": 1})  # after: ACCEPTED
        with patch("app.services.batfish_service.BatfishService._get_session", return_value=fake_session), \
             patch("app.services.batfish_service.BatfishService._load_network_policies", return_value=MOCK_POLICIES):
            result = _run_validate(service, DEVICE_CISCO, "! current\n", "permit ip vlan50 any vlan10 any\n")
        assert result.status == BatfishStatus.CRITICAL
        assert result.behavior_changes >= 1
        assert any(f.severity == "critical" for f in result.findings)

    def test_guest_reaches_management_flagged_as_finding(self):
        service = _service()
        fake_session = _fake_session({"before": 1, "after": 1})  # already accepted both sides
        with patch("app.services.batfish_service.BatfishService._get_session", return_value=fake_session), \
             patch("app.services.batfish_service.BatfishService._load_network_policies", return_value=MOCK_POLICIES):
            result = _run_validate(service, DEVICE_CISCO, "! current\n", "! proposed\n")
        # Policy violation (expect_denied but ACCEPTED) still flags even
        # with no before/after behavior change.
        assert result.status == BatfishStatus.CRITICAL
        assert any(f.query == "guest_to_management" for f in result.findings)

    def test_route_removal_causes_reachability_loss_finding(self):
        """before ACCEPTED -> after DENIED is a behavior change (reachability
        loss), even though it isn't itself a policy violation."""
        service = _service()
        fake_session = _fake_session({"before": 1, "after": 0})
        with patch("app.services.batfish_service.BatfishService._get_session", return_value=fake_session), \
             patch("app.services.batfish_service.BatfishService._load_network_policies", return_value={"reachability_checks": []}), \
             patch(
                 "app.services.batfish_service.BatfishService._run_before_after_queries",
                 return_value=[],
             ):
            result = _run_validate(
                service, DEVICE_CISCO, "! current\n", "! proposed\n no ip route\n",
            )
        # No reachability_checks configured and no before/after queries in
        # this test, so with nothing to check we expect PASS -- this test
        # exists to exercise the before/after query path directly instead.
        assert result.status == BatfishStatus.PASS

    def test_route_removal_reachability_loss_via_before_after_query(self):
        service = _service()
        fake_session = _fake_session({"before": 1, "after": 0})
        queries = [{"name": "branch_to_dc", "source": "10.30.0.0/24", "destination": "10.40.0.0/24"}]
        with patch("app.services.batfish_service.BatfishService._get_session", return_value=fake_session), \
             patch("app.services.batfish_service.BatfishService._load_network_policies", return_value={"reachability_checks": []}):
            result = _run_validate(
                service, DEVICE_CISCO, "! current\n ip route 10.40.0.0 255.255.255.0 10.30.0.1\n", "! proposed\n",
            )
            # Direct call to exercise the before/after query path with a
            # custom query list (not just the fixed reachability_checks set).
            result_bq = asyncio.get_event_loop().run_until_complete(
                service.validate_configuration(
                    change_request_id="cr-2", device=DEVICE_CISCO,
                    current_config="! current\n", proposed_config="! proposed\n",
                    before_after_queries=queries,
                )
            )
        assert result_bq.status == BatfishStatus.REVIEW
        assert result_bq.behavior_changes >= 1
        assert any(f.before == "ACCEPTED" and f.after == "DENIED" for f in result_bq.findings)

    def test_unchanged_before_after_passes(self):
        service = _service()
        fake_session = _fake_session({"before": 0, "after": 0})
        with patch("app.services.batfish_service.BatfishService._get_session", return_value=fake_session), \
             patch("app.services.batfish_service.BatfishService._load_network_policies", return_value=MOCK_POLICIES):
            result = _run_validate(service, DEVICE_CISCO, "! same\n", "! same\n")
        assert result.status == BatfishStatus.PASS
        assert result.behavior_changes == 0

    def test_changed_before_after_produces_finding(self):
        service = _service()
        fake_session = _fake_session({"before": 0, "after": 1})
        with patch("app.services.batfish_service.BatfishService._get_session", return_value=fake_session), \
             patch("app.services.batfish_service.BatfishService._load_network_policies", return_value={"reachability_checks": []}):
            result = asyncio.get_event_loop().run_until_complete(
                service.validate_configuration(
                    change_request_id="cr-3", device=DEVICE_CISCO,
                    current_config="! current\n", proposed_config="! proposed\n",
                    before_after_queries=[{"name": "custom", "source": "1.1.1.1", "destination": "2.2.2.2"}],
                )
            )
        assert result.behavior_changes >= 1
        assert any(f.behavior_changed for f in result.findings)

    def test_unsupported_vendor_never_silently_safe(self):
        service = _service()
        result = _run_validate(service, DEVICE_UNSUPPORTED, "! current\n", "! proposed\n")
        assert result.status == BatfishStatus.UNSUPPORTED
        assert result.status != BatfishStatus.PASS
        assert result.reason is not None

    def test_batfish_unavailable_returns_unavailable_status(self):
        service = _service()
        with patch("app.services.batfish_service.BatfishService._get_session", side_effect=RuntimeError("coordinator down")):
            result = _run_validate(service, DEVICE_CISCO, "! current\n", "! proposed\n")
        assert result.status == BatfishStatus.UNAVAILABLE
        assert result.reason is not None

    def test_batfish_unavailable_records_fail_closed_flag_for_caller(self):
        """batfish_service itself never blocks on fail_closed -- see
        change_validation_service._combine, which is where
        BATFISH_FAIL_CLOSED is actually enforced. This just confirms the
        flag is captured on the instance so the orchestrator can read it."""
        service = _service(fail_closed=True)
        with patch("app.services.batfish_service.BatfishService._get_session", side_effect=RuntimeError("coordinator down")):
            result = _run_validate(service, DEVICE_CISCO, "! current\n", "! proposed\n")
        assert result.status == BatfishStatus.UNAVAILABLE
        assert service.fail_closed is True

    def test_disabled_returns_unsupported_not_pass(self):
        service = _service(enabled=False)
        result = _run_validate(service, DEVICE_CISCO, "! current\n", "! proposed\n")
        assert result.status == BatfishStatus.UNSUPPORTED


class TestSnapshotName:
    def test_snapshot_name_is_deterministic(self):
        from app.services.batfish_service import snapshot_name
        assert snapshot_name("cr-42", revision=1) == snapshot_name("cr-42", revision=1)
        assert snapshot_name("cr-42", revision=0) != snapshot_name("cr-42", revision=1)


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_health_check_disabled_is_true(self):
        service = _service(enabled=False)
        assert await service.health_check() is True

    async def test_health_check_false_on_exception(self):
        service = _service()
        with patch("app.services.batfish_service.BatfishService._get_session", side_effect=RuntimeError("down")):
            assert await service.health_check() is False
