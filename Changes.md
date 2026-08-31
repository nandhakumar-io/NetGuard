# NetGuard hardening audit — fixes applied

## Packaging/extraction bugs (not your code — the uploaded zip itself)
1. `backend/app/device_gateway/` was silently dropped on extraction: the zip
   contains a stray empty *file* entry named `backend/app/device_gateway`
   alongside the real directory of the same name. Recovered all five files
   (`main.py`, `executor.py`, `validator.py`, `terminal_executor.py`,
   `__init__.py`) directly from the archive. Re-zip your source without that
   stray entry to avoid this happening again on a clean checkout.

## Real bugs fixed
2. `docker-compose.yaml` mounted `./nats/nats-server.conf`, a path that does
   not exist — the real file is `backend/app/nats/nats-server.conf`. This
   would have made NATS boot with no auth/ACLs despite the hardening config
   existing. Fixed the mount to point at the real path (no file duplication).
3. `app/device_gateway/main.py`, `app/core/config.py`: added
   `NATS_GATEWAY_USER`/`NATS_GATEWAY_PASSWORD` settings and passed them to
   the Gateway's own `nats.connect()` call, so it authenticates as its own
   GATEWAY account rather than implicitly. (Not previously broken — compose
   already embeds creds in `NATS_URL` via interpolation — but explicit is
   safer than implicit for the one process holding OpenBao device-credential
   access.) Same pattern added for the API tier's four `nats.connect()` call
   sites (`device_job_service.py`, `terminal.py`, `event_bus.py` x2) for
   consistency, though verified these were not actually broken.

## Real security gap fixed: 4 unconditional in-process device connections
Found via precise grep (import-level, not string-match) across every file
flagged in the Phase 1 doc — most flagged files turned out to be false
positives (comments/docstrings, or already correctly gated behind
`settings.DEVICE_GATEWAY_ENABLED` with a documented legacy fallback). Four
were real and had **no gate at all**:

- `app/services/runbook_execution_service.py` — automated alert-triggered
  remediation called `ProtocolManager.deploy_config()` directly: a device
  **write**, unconditional, no Gateway independent validation. Highest-risk
  finding of the audit. Now routes through
  `device_job_service.submit_job_sync(..., DeviceOperation.DEPLOY_CONFIG)`
  when `DEVICE_GATEWAY_ENABLED` (default), with the old path preserved as
  explicit fallback.
- `app/services/backup_service.py` (`run_device_config_backup`) — read.
  Now routes GET_RUNNING_CONFIG + best-effort GET_STARTUP_CONFIG through
  the Gateway.
- `app/api/devices.py` (`POST /{device_id}/ssh-credentials/test`) — read.
  Now routes through the Gateway.
- `app/api/change_requests.py` (`_resolve_current_config`, used by change
  creation + risk-scoring) — read. Now routes through the Gateway, falling
  back to snapshot on Gateway timeout/rejection same as before.

All four preserve the existing legacy in-process path behind
`if not settings.DEVICE_GATEWAY_ENABLED:` for parity with the rest of the
codebase's migration pattern (`pipeline_service.py`, `rollback_service.py`,
`drift_service.py`, `config_management.py`).

## Confirmed NOT bugs (raised, then corrected after checking real context)
- `terminal.py` "asyncssh" hit was a comment, not an import — already fully
  migrated.
- `deployment_engine.py`'s netmiko `ConnectHandler` is only reached via the
  Device Gateway/legacy-fallback paths, not directly from the API.
- `firmware_upgrade_service.py` doesn't touch devices at all yet — it's an
  explicitly-documented state-machine prototype.
- Container hardening (`read_only`/`cap_drop`/`no-new-privileges`) is
  already applied to all 21 compose services except `grafana`, which has a
  documented, deliberate exception (Grafana 11.x boot failure without
  additional tmpfs tuning).

## Not yet done / needs your review
- `app/services/protocol_manager.py`'s `_credentials()`, `gnmi_service.py`,
  `metrics_service.py` (SNMP) still resolve credentials in-process
  unconditionally. These are lower-severity (SNMP read-only telemetry,
  gNMI streaming) but you asked for everything migrated — I stopped here to
  keep this batch reviewable rather than touching the polling hot path
  blind. Want me to continue with these next?
- Self-approval prevention on `ChangeRequest`, JIT expiry enforcement at
  execution time, and audit tamper-resistance are still unverified against
  running code (only migration files were confirmed to exist).