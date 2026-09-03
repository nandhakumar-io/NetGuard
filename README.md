# NetGuard6 test-suite fixes

Drop these files into the matching paths under `backend/`. Verified with
`python3 -m pytest -q` from `backend/`: 291 passed, 1 pre-existing
unrelated failure (`test_device_credential_key_scoping.py`, not touched
by these changes).

## Files

- `backend/requirements.txt` — added `respx==0.23.1` (was imported by
  `tests/test_opa_service.py` but never declared; caused a pytest
  *collection* error that aborted the whole run and cascaded into every
  later Jenkins stage being skipped).

- `backend/tests/test_pipeline.py` — `test_pipeline_success_path`'s mock
  for `device_job_service.submit_job_sync` only stubbed 2 Gateway calls,
  but the code path now makes 3 (Section 13's mandatory post-deployment
  verification re-read was added later and the test wasn't updated) —
  caused `StopIteration`. Also mocks `event_bus.publish_event` in all 4
  tests (previously unmocked anywhere in this file — see event_bus.py
  below for why that matters).

- `backend/tests/test_batfish_service.py` — replaced the deprecated
  `asyncio.get_event_loop().run_until_complete(...)` pattern (3 call
  sites) with `asyncio.run(...)`. The old pattern depends on there being
  a "current" event loop already set for the thread, which becomes
  unreliable depending on what other test modules ran earlier in the
  same session — was intermittently raising
  `RuntimeError: There is no current event loop in thread 'MainThread'`.

- `backend/tests/conftest.py` — **new file**. Autouse fixture that resets
  `event_bus`'s background NATS-publisher singleton after every test, so
  a test that forgets to mock `event_bus.publish_event` fails on its own
  instead of leaking a daemon thread that keeps retrying a real NATS
  connection in the background for the rest of the pytest session and
  intermittently breaking unrelated, later-running tests.

- `backend/app/services/event_bus.py` — added `_PublisherLoop.reset()` /
  module-level `reset_publisher()`, used by the new conftest fixture.
  Gracefully drains any in-flight fire-and-forget publish before tearing
  down the loop/thread/connection.

## Known follow-up (not fixed here, out of scope of what was asked)

- `test_device_credential_key_scoping.py::test_stale_unscoped_rotation_module_is_not_importable_by_the_app`
  fails on a clean run of the full suite — pre-existing, unrelated to any
  of the above.
- A couple of other test files call the real (unmocked) `event_bus.publish_event`
  and occasionally print a harmless `Task was destroyed but it is pending`
  warning during teardown. Doesn't fail anything; worth an audit later to
  mock `event_bus` consistently wherever `pipeline_service`/similar are
  exercised, the same way `test_pipeline.py` now does.