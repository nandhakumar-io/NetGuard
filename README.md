# Device Gateway (Phase 3)

## What this is

A separate process (own container, own Docker network) that consumes
signed job requests from NATS, independently re-validates them against
Postgres, and is the only thing in the deployment with a network path to
managed devices. See `../../../docker-compose.yaml`'s `device-gateway`
service and the `netguard-execution` network for the isolation boundary.

```
API --(signed job over NATS jobs.request)--> Device Gateway --(SSH/NETCONF/RESTCONF)--> Device
     <--(result over NATS jobs.result.<id>)--
```

## Files

- `app/schemas/device_job.py` — the signed job envelope (shared by API and Gateway)
- `app/services/device_job_service.py` — API-side: builds, signs, publishes a job, awaits the result
- `app/device_gateway/validator.py` — **the trust boundary**: independently checks signature, expiry, replay, tenant match, approval state, self-approval, JIT state
- `app/device_gateway/executor.py` — maps a validated job to a `protocol_manager` call
- `app/device_gateway/main.py` — NATS consumer entrypoint (`python -m app.device_gateway.main`)
- `tests/test_device_gateway_validator.py` — 11 tests proving the validator actually rejects forged/expired/replayed/cross-tenant/self-approved/unapproved jobs

## What's migrated vs. what isn't (be honest about scope)

**Migrated (this change):** the job-dispatch pattern itself, end to end,
with a real, tested validator. `DeviceOperation.GET_RUNNING_CONFIG` /
`GET_STARTUP_CONFIG` / `DEPLOY_CONFIG` / `ROLLBACK_CONFIG` / `REBOOT` are
defined as the initial operation whitelist.

`GET /devices/{device_id}/config/running` (`app/api/config_management.py`)
is the first live call site: behind `settings.DEVICE_GATEWAY_ENABLED`
(default `False`), it calls `device_job_service.submit_job()` instead of
`ProtocolManager` directly. Flag defaults off so nothing changes until an
operator has actually deployed the `device-gateway` container and
switches it on deliberately. Covered by
`tests/test_config_management_gateway_migration.py` (asserts the legacy
path is what runs by default, and that the gateway path calls
`submit_job` with the right tenant/device/user and maps the result back
into the existing response schema).

**Not yet migrated — tracked follow-up work:**

1. **Only one endpoint migrated so far** (`GET /devices/{device_id}/config/running`).
   `app/api/deployments.py`, the rest of `config_management.py`
   (backup/restore/rollback/deploy), and others still call
   `protocol_manager` / `deployment_engine` directly. Swapping each
   remaining call site is mechanical, following the same pattern, but
   needs its own test per endpoint.

2. **Terminal (interactive SSH, `app/api/terminal.py`) is NOT migrated.**
   It's a streaming, stateful session, not a single request/response job
   — the envelope/validator pattern above doesn't map directly onto
   "keep a live PTY open for N minutes with keystroke passthrough".
   Section 14 of the hardening spec wants
   `Browser -> NetGuard -> Authorized terminal session -> Device Gateway -> Device`;
   doing this properly means the Gateway holding the actual SSH
   connection and the API proxying a WebSocket that only carries
   already-authorized terminal I/O — a separate design pass, not a
   trivial extension of this envelope.

3. **`api` container's Docker network membership hasn't been changed
   yet.** Adding `netguard-execution` as an `internal: true` network
   that only `device-gateway` sits on stops any *new* container from
   getting a path to devices through it, but it doesn't retroactively
   remove whatever reachability the `api`/worker containers already had
   through other means (their own outbound routing, if the real device
   VLAN is reachable from `netguard-internal` in a given deployment).
   Full enforcement requires the actual production network topology
   (host firewall rules / real VLAN wiring), which is deployment-
   specific and out of scope for this compose file alone — flagged as a
   residual risk, not silently assumed solved.

4. **JIT elevation is still fleet-wide, not device-scoped** (see Phase 1
   finding). The validator checks that a JIT grant is active and belongs
   to the requester, but cannot yet check "is this grant scoped to THIS
   device" — that requires a schema change to `JitElevation`
   (`app.models.jit_elevation`) that's out of this change's scope.

5. **Credentials are still resolved via `credential_service`/Fernet
   inside whatever process calls `protocol_manager`** — since
   `executor.py` reuses `protocol_manager` as-is, credential resolution
   now happens inside the Gateway process rather than the API process,
   which is real, meaningful progress (the API no longer needs
   `SECRET_ENCRYPTION_KEY` at all once every call site is migrated) —
   but OpenBao integration (Phase 4) hasn't happened yet, so credentials
   are still Fernet-encrypted DB fields, just decrypted in a smaller,
   more isolated process now instead of the internet-facing one.

## Security properties this delivers today

- A job the API publishes cannot be executed by the Gateway unless it's
  correctly signed with `DEVICE_JOB_SIGNING_KEY` — a secret the API
  process needs to hold to publish jobs, but which is *not* the JWT
  `SECRET_KEY`, *not* the Fernet `SECRET_ENCRYPTION_KEY`, and not a
  device credential. Compromising the API still means an attacker can
  ask the Gateway to do device operations, scoped to what a real user's
  JWT-derived request could ask for — this is a real reduction in blast
  radius versus today's in-process device connectivity, but it is *not*
  full isolation until items 1–3 above are also done.
- The Gateway independently re-derives tenant match, approval status,
  and self-approval — a bug in the API's own approval-gating logic does
  not automatically mean the Gateway will execute an unapproved or
  self-approved change.
- Replay of a captured job is rejected once its `job_id` has been seen,
  and any job past its (short) `expires_at` is rejected regardless of
  signature validity.