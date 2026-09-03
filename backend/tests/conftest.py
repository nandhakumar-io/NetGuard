"""Shared pytest fixtures for the backend test suite."""
import pytest


@pytest.fixture(autouse=True)
def _reset_event_bus_publisher():
    """Backstop for app.services.event_bus's module-level `_publisher`
    singleton.

    `event_bus.publish_event()` is easy to leave unmocked -- it returns
    None and is never awaited, so a forgotten `@patch` doesn't show up as
    an obvious missing argument the way an unmocked async call usually
    does. But it isn't a no-op: the first call in the process lazily
    starts a persistent daemon thread that opens a real NATS connection
    (see event_bus._PublisherLoop._ensure_started), with infinite
    reconnect attempts. In any environment without a reachable NATS
    broker (CI, a laptop with the stack not running), that thread fails
    to connect but keeps retrying forever in the background -- and because
    it's a *module-level* singleton, it survives across every test in the
    session, not just the one that triggered it.

    This fixture doesn't replace mocking event_bus.publish_event in
    individual tests (do that too -- see tests/test_pipeline.py), but
    resets the singleton after every test so a test that forgets to mock
    it fails on its own (visibly, close to the cause) instead of leaking a
    background thread that goes on to intermittently break unrelated
    tests running later in the same session.
    """
    yield
    from app.services import event_bus

    event_bus.reset_publisher()
