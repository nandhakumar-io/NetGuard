# OPA + Batfish Pre-Deployment Validation

This document covers the policy-as-code (OPA) and network-behavior-simulation
(Batfish) layers added on top of NetGuard's existing configuration validator,
risk engine, and impact simulation. It's meant for anyone extending a policy,
debugging a validation result, or trying to understand why a change was
blocked.

## Where this fits

NetGuard already answers two questions before a change is approved:

- **Is the configuration structurally valid?** (`validation_engine.py` —
  syntax, vendor-aware command checks, VLAN/ACL/interface references)
- **How risky is it, and what's the blast radius?** (`risk_engine.py`,
  `impact_simulation_service.py`)

OPA and Batfish add two more, answered in this order:

| Layer | Question | Module |
|---|---|---|
| Syntax | Is the configuration structurally valid? | `validation_engine.py` (unchanged) |
| OPA | Is this change *allowed* by policy? | `opa_service.py` |
| Batfish | What will the network *actually do* if applied? | `batfish_service.py` |
| Risk | How risky is it? | `risk_engine.py` (unchanged) |
| Impact | What's the blast radius? | `impact_simulation_service.py` (unchanged) |

`change_validation_service.py` is the orchestrator that runs all of these in
order and combines them into one `ChangeValidationResult` — this is the only
new module the rest of the app should call for the combined answer.

```
Change Request
     │
     ▼
validation_engine (syntax)     ── FAIL → BLOCK
     │
     ▼
opa_service (policy)           ── DENY → BLOCK, REVIEW → flagged
     │
     ▼
batfish_service (behavior)     ── critical finding → BLOCK, risky → flagged
     │
     ▼
risk_engine + impact_simulation_service (unchanged)
     │
     ▼
change_validation_service combines everything → decision: pass | review | block
```

A lower-severity result never overrides a higher-severity one — see
"Decision precedence" below.

## Where each engine's code lives

```
backend/app/services/opa_service.py               # OPA HTTP client + input builder
backend/app/services/batfish_service.py            # pybatfish wrapper, snapshot lifecycle
backend/app/services/change_validation_service.py  # orchestrator / combined decision
opa/policies/*.rego                                # the actual policies
opa/data/network_policies.json                     # tunable data (VLANs, subnets, thresholds)
opa/tests/*.rego                                   # rego unit tests (run with `opa test opa/`)
backend/tests/test_opa_service.py
backend/tests/test_batfish_service.py
backend/tests/test_change_validation_service.py
backend/tests/test_pipeline.py                     # pre-deploy gate integration test
```

API surface: `POST /api/v1/change-requests/{id}/validate` (run/re-run) and
`GET /api/v1/change-requests/{id}/validation` (read last result) in
`backend/app/api/change_requests.py`. Both return the same
`CombinedValidationReport` shape. The frontend renders it via
`frontend/src/components/ValidationPipelinePanel.tsx` on the Change Request
detail page.

## How OPA evaluation works

`opa_service.build_opa_input()` builds a normalized JSON document from the
device, change request, proposed/current config, topology, risk, and blast
radius — **never credentials or secrets**. This is POSTed to
`OPA_URL + OPA_POLICY_PATH` (default `http://opa:8181/v1/data/netguard`).

The response is parsed into an `OpaResult` with `passed`, `decision`
(`allow` | `review` | `deny`), `violations`, `warnings`, and
`matched_policies`. If OPA is unreachable:

- `OPA_FAIL_CLOSED=true` (default) → treated as `deny`, blocking the change.
- `OPA_FAIL_CLOSED=false` → treated as a warning, not a block.

### How to add a new OPA policy

1. Pick (or create) a `.rego` file under `opa/policies/` — one package per
   concern (`network_security`, `segmentation`, `access_control`,
   `change_management`, `routing`). Package name is `netguard.<concern>`.
2. Add a `violation contains v if { ... }` rule (hard block/critical) or a
   `review contains v if { ... }` rule (needs a human look), each producing
   `{"policy": "<concern>.<short_name>", "severity": ..., "message": ...,
   "details": {...}}`.
3. If the rule needs tunable data (a VLAN ID, a subnet, a threshold), put it
   in `opa/data/network_policies.json` instead of hardcoding it in the rego,
   and reference it as `data.network_policies.<path>`.

   **Data-path wiring, read this before touching the OPA volume mount:**
   `opa/data/network_policies.json`'s own contents are *unwrapped* — top
   level keys are `vlans`, `protected_subnets`, `routing`, `thresholds`,
   etc. (this is also the exact shape `batfish_service._load_network_policies()`
   reads directly off disk). For the rego files' `data.network_policies.*`
   references to resolve, the file must be mounted into the OPA container
   one directory level *below* the data root — `docker-compose.yaml` does
   this with `./opa/data:/data/network_policies:ro` (a `network_policies`
   subdirectory in the container, not a flat `/data`) while `opa run`
   loads `/data` as the root. OPA's directory-based data loading derives
   the `data` path from directory names, not filenames, so a flat
   `./opa/data:/data:ro` mount lands the file's keys at `data.vlans`,
   `data.thresholds`, etc. with `data.network_policies` never populated —
   every policy that reads `data.network_policies.*` (segmentation,
   access_control's protected-subnet check, routing's authorized-route
   and critical-network checks, change_management's blast-radius
   threshold) then silently evaluates its guard clauses to `undefined`
   and never fires, with no error anywhere. `opa/tests/*_test.rego` won't
   catch this class of bug either, since each test explicitly overrides
   `with data.network_policies as <mock>` — that's correct for testing
   rule *logic* but bypasses the real wiring entirely. To sanity-check the
   wiring itself after touching the mount or the file's location, run OPA
   the same way `docker-compose` does and query it directly:

   ```bash
   opa run --server --addr=127.0.0.1:8181 opa/policies opa/data:opa-mount
   # (or, matching compose exactly, mount opa/data one level under a
   # network_policies/ directory first, then load that as the data root)
   curl -s http://127.0.0.1:8181/v1/data/network_policies/vlans/guest
   # must return {"result": [...]}, not {}
   ```
4. Wire the new rule into the aggregator in `opa/policies/policy.rego` — add
   it to `all_violations` or `all_reviews` as appropriate. This file is the
   only thing `opa_service.py` queries (`/v1/data/netguard`), so a policy
   that isn't folded in here has no effect.
5. Add a test in `opa/tests/` (same package, `_test.rego` suffix) with a
   sample `input` and an assertion the violation/review fires. Run
   `opa test opa/` to check it. Remember this only tests rule logic against
   mocked data (see the wiring note above) — it does not verify the volume
   mount.
6. Add a corresponding case to `backend/tests/test_opa_service.py` if the
   new policy changes how `opa_service` parses or surfaces the result.

Policies are data, not code that ships with a backend deploy — bump
`OPA_POLICY_DATA_PATH` or the bundle/version you track in
`policy_version` if you want old validation results to be distinguishable
from ones evaluated under a newer policy set.

## How Batfish validation works

`batfish_service.py` isolates all `pybatfish`-specific logic and lazy-imports
`pybatfish` so the rest of the backend can import this module (dataclasses,
`is_vendor_supported`, tests) without Batfish installed or reachable.

For each validation:

1. `is_vendor_supported(vendor, platform)` checks whether Batfish has
   meaningful support for this device. Cisco IOS/IOS-XE, Arista EOS, and
   Juniper Junos are supported; anything else returns
   `BatfishStatus.UNSUPPORTED` and skips simulation entirely — validation
   still proceeds through OPA + risk + impact, it just doesn't get a
   behavioral opinion.
2. `create_snapshot(...)` builds an isolated snapshot
   (`{BATFISH_SNAPSHOT_NAME_PREFIX}-cr-{change_request_id}-{revision}`) from
   the current inventory plus the proposed configuration. **Production
   device configuration is never touched** — this only feeds Batfish's
   in-memory model.
3. `validate_configuration(...)` runs the standard checks
   (`_run_standard_checks`) plus before/after behavioral comparisons
   (`_run_before_after_queries`, `_check_reachability`) — guest↔management
   reachability, ACL permit/deny changes, routing changes — and returns a
   `BatfishResult` with a list of `BehaviorFinding`s.
4. Each `BehaviorFinding` records `source`, `destination`,
   `protocol`/`port`, `before`, `after`, `behavior_changed`, `severity`, and
   `explanation` — the same shape shown in the UI's "Network behavior
   changes" list.
5. `cleanup_snapshots(...)` retains the most recent `BATFISH_SNAPSHOT_RETENTION`
   snapshots per change request and discards older ones.

If Batfish is unreachable, `_unavailable(reason, started)` returns
`BatfishStatus.UNAVAILABLE` — **never silently converted to a passing
result**. `BATFISH_FAIL_CLOSED=false` (default) means an unavailable Batfish
degrades a change to `review` rather than `block` for high-risk changes, and
is a warning (not a blocker) otherwise; set `BATFISH_FAIL_CLOSED=true` to
block instead.

## Decision precedence

`change_validation_service._combine()` never lets a lower-severity result
override a higher one:

```
syntax FAIL              → BLOCK
OPA deny                 → BLOCK
Batfish critical finding → BLOCK
high-risk behavior change → REVIEW
OPA review                → REVIEW
Batfish unavailable        → REVIEW or WARNING, per BATFISH_FAIL_CLOSED
everything passes          → PASS
```

## Configuration

See `.env.example` for the full list (`OPA_*`, `BATFISH_*`). Key ones:

- `OPA_ENABLED` / `BATFISH_ENABLED` — turn either engine off entirely
  (validation still runs the other layers).
- `OPA_FAIL_CLOSED` (default `true`) / `BATFISH_FAIL_CLOSED` (default
  `false`) — what happens when the engine can't be reached.
- `BATFISH_SNAPSHOT_RETENTION` — how many per-CR Batfish snapshots to keep.

## Testing

```bash
# Python-side unit + integration tests
cd backend
python3 -m pytest tests/test_opa_service.py tests/test_batfish_service.py \
  tests/test_change_validation_service.py tests/test_pipeline.py -v

# Rego policy tests (requires the `opa` CLI)
opa test opa/ -v

# Runtime wiring smoke test -- confirms data.network_policies.* actually
# resolves the way docker-compose mounts it (see the wiring note above);
# `opa test` alone won't catch a broken mount since its tests mock this
# data directly.
mkdir -p /tmp/opa-mount-check/network_policies
cp opa/data/network_policies.json /tmp/opa-mount-check/network_policies/
opa run --server --addr=127.0.0.1:8181 opa/policies /tmp/opa-mount-check &
sleep 2
curl -s http://127.0.0.1:8181/v1/data/network_policies/vlans/guest   # expect {"result": ["50"]}, not {}
kill %1
```

`test_pipeline.py` covers the end-to-end guarantee that matters most: a
`BLOCK` decision from this gate can never reach deployment, because
`pipeline_service.py`'s pre-deploy step re-runs `change_validation_service
.validate_change(...)` against a fresh device read immediately before
pushing, and refuses to submit the deploy job if that gate returns `BLOCK`.