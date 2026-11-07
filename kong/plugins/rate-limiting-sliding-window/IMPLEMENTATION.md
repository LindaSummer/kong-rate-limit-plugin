# Fixed and approximate sliding-window MVP

Updated: 2026-11-07. Status: pipelined hash-field MVP and pytest/uv suite validated.

## Accepted design

Implement `rate-limiting-sliding-window` as a standalone custom Kong plugin. Use the official
Kong 3.9.4 and Redis 7.4 images. Keep normal Kong plugin configuration in
PostgreSQL; no custom counters table, DAO, migrations, or monitoring exporter.
Async synchronization, exact sliding logs, and Redis Cluster remain deferred.

Kong handles HTTP parsing, route/service selection, and plugin precedence.
Attach a configuration to a service to group its routes; a more specific
configuration overrides it. Do not implement matching rules in the limiter.
Support global, service, route, and combined scopes; disable Consumer attachment.

## Public configuration

```json
{
  "name": "rate-limiting-sliding-window",
  "config": {
    "client_id_header": "X-Client-Id",
    "redis": {"host": "redis", "port": 6379, "timeout": 150},
    "limits": [
      {"algorithm": "fixed_window", "window_ms": 1000,
       "limit": 20, "dimensions": ["ip"]},
      {"algorithm": "sliding_window", "window_ms": 60000,
       "limit": 300, "dimensions": ["client_id"]}
    ]
  }
}
```

Allow 1–16 limits, integer thresholds 1–1,000,000, and integer windows
1–86,400,000 ms. Algorithm, window, threshold, and dimensions are required.
Dimensions are ip/client_id/path/method/route/service; canonicalize their order
and reject duplicates, including equivalent algorithm/window/dimension limits.
There are no customer-defined rule IDs, explicit versions, or async modes.
Use normal eventual Kong configuration propagation. Updating only a threshold
retains counts; changing algorithm/window/dimensions/scope selects another bucket.

Use normalized paths without queries and trusted-proxy-aware IP from the PDK.
Require a single nonempty ClientId header of at most 256 bytes only when needed.
Do not silently substitute IP for ClientId. Header identity is caller-supplied.

## Identity and Redis data layout

Use one binary hash key per bucket, with no timestamp suffix:

```text
"rlsw:v2:" [8] + service UUID [16] + route UUID [16]
              + limit signature [32] + dimension-value SHA-256 [32]
= 104 bytes
```

UUIDs are raw bytes, absent configured scope IDs are zero UUIDs. Scope IDs come
from plugin configuration rather than the request's matched route. Hash canonical
arrays to avoid delimiter ambiguity. Signature includes algorithm, window, sorted
dimensions but excludes the threshold. Recreating a plugin at the same scope can
reuse counts. Log binary keys as hex.

Hash fields are decimal epoch-millisecond window starts; values are integer
attempt counts. Redis stores the current and previous sliding windows in one
hash. Fixed windows use the same layout but do not read a previous count.

Kong computes the current/previous field names from one request timestamp.
HPEXPIRE NX assigns the current field a relative TTL through the end of its fixed
window, or through the following window for sliding counters. Repeated increments
preserve existing expirations. Redis expires old fields and removes an empty
hash. Only two logical windows are read; network/clock timing may briefly leave
older physical fields until their TTL expires. Redis 7.4+ is required.

This replaces v1 start/current/previous/last metadata hashes. The v2 namespace
avoids mixing schemas and starts fresh budgets on rollout. Do not delete v1 keys;
allow their existing TTLs to clean them up. Local shared dictionaries emulate
hash fields with scalar slot keys under the same v2 base identity.

## Request flow and accounting

1. Capture `floor(ngx.now()*1000)` in Kong once at access-phase entry.
2. Extract configured identity values and build bucket hashes/window field names.
3. Pipeline one small per-bucket EVALSHA call for every configured limit.
4. Each constant-size Lua operation reads current and previous fields, validates
   counters, HINCRBYs current, attaches HPEXPIRE, and returns both counts atomically.
5. Kong evaluates fixed/weighted sliding limits and returns allow/deny.

There is no Redis TIME call, multi-limit Lua loop, or WATCH/MULTI/EXEC transaction.
The gateway clocks must be synchronized; backward/skewed clocks can select a
different field. Redis still owns counter atomicity and expiry, but it does not
correct gateway time. All gateway/fallback calculations reuse the captured time.

Follow Kong's simpler attempt-accounting semantics: every attempted request is
counted, including denials. A request must satisfy every limit to reach upstream,
but a denial by one does not undo other increments. A later pipeline error can
leave earlier counters consumed. There is no request-wide all-or-none guarantee.

Sliding usage is `current + previous * (1 - elapsed/window_ms)` where current
already includes this attempt. Use bounded numeric inputs and scaled calculations
near admission boundaries. Retry-After predicts the next attempt without other
traffic and therefore accounts for the rejected attempt too. Sustained rejected
traffic can prolong sliding-window throttling. This is an accepted approximation,
not an exact rolling-window request cap.

Use EVALSHA with safe NOSCRIPT recovery: load the script then retry only failed
entries. Never replay successful increments or an ambiguous pipeline after a
transport failure. Connection pooling, authentication, and optional TLS remain.
Return 400 for required identity errors, 429 with JSON/no-store/rounded-up
Retry-After for quota exhaustion, and 503 only if the local limiter cannot maintain enforcement. Preserve upstream responses and Retry-After;
there are no refunds. Diagnostic headers expose instance, mode, request time,
and minimum whole remaining quota.

## Local fallback and circuit breaker

Local shared memory mirrors the same attempt counters and current/previous field
selection, with node-wide locking and safe writes that do not evict live budgets.
Partial local failures can retain earlier increments. Both Redis and local paths
use the same captured request time and calculation module.

Any Redis operation failure opens the node-shared circuit, including connection,
authentication, script, or counter-data errors. Cooldown starts at 250 ms,
failed probes double it up to four seconds, and an expiring lease allows one probe
per node/scope/backend. Generation checks fence late replies. Successful probes
ramp Redis traffic through 10/25/50/100 percent at one-second stages, then close.
Local consumption is not replayed into Redis. Outages, recovery, clock skew,
configuration propagation, node restarts, and Redis state loss do not promise a
strict global quota. Redis failures always activate local enforcement. Circuit-coordination failures
also attempt local enforcement; a failed circuit update does not discard a valid
Redis decision. Only local limiter failure returns 503; never proxy unmetered.

## Modules, operations, and verification

- handler/schema/identity: orchestration, config, one Kong timestamp, scope keys.
- algorithms: fixed/weighted sliding calculations entirely inside Kong.
- redis_script: bounded per-hash reads/increment/field-expiry, no loop over limits.
- policies/redis: batching, reply validation, partial cache-miss recovery, pooling.
- policies/local and circuit_breaker: shared fallback accounting and recovery.

Compose registers this plugin, keeps Redis on internal redis:6379 and host
127.0.0.1:6380, and supplies gateway DNS round-robin benchmarking with k6.
Use ./run.sh start, ./run.sh test, ./run.sh benchmark, ./run.sh stop. Restart all
three Kong instances after Lua changes. Never run suites/benchmarks concurrently.
Preserve persistent volumes and user-created demo policies.

Tests cover deterministic timestamps/window fields and weighting, next-attempt
retry delays, denied-attempt accounting, partial errors, mixed NOSCRIPT replies
without double counting, actual field expiry and empty-hash removal, concurrent
shared counters, scope/dimension isolation, schema validation, live updates,
local memory exhaustion, half-open probes, outages/recovery, upstream headers,
and restart preservation.

## Test implementation and verified state

The standalone repository contains this custom plugin and its tests under `test/`.
The optional root Dockerfile adds only the plugin to the official Kong image.
Compose runs official Kong images directly; a separate `test/Dockerfile` builds
only the Python test environment. The retired token-bucket experiment is removed.
Local Lua IDE settings, type bridges, runtime libraries, and agent notes remain
Git-ignored and excluded from Docker build context.

The test image uses Python 3.12 and uv 0.9.4. `test/pyproject.toml` declares pytest;
`test/uv.lock` currently locks pytest 8.4.2 and its transitive dependencies.
`uv sync --locked --no-dev` installs them at image build time into `/opt/test-venv`.
Python commands use `uv run --frozen --no-sync`; the `/test` source mount remains
read-only and pytest's writable cache lives under `/tmp`.

`./run.sh test` builds the test image once and runs these stages sequentially:

1. LuaJIT syntax, deterministic Lua unit checks including parameter sweeps, isolated real shared-memory
   checks, and real Redis pipeline/field-expiry checks.
2. Fourteen named pytest gateway cases in `test/test_integration.py`, including
   parameterized fixed/sliding budgets, outages, and upstream 429/503 responses.
3. `test/window_restart.py prepare` creates a Redis-backed sliding budget and
   consumes one admission. The host runner restarts `kong1` and waits for health.
4. Pytest explicitly selects the `restart`-marked verifier in
   `test/test_restart.py`; it verifies the remaining shared budget and cleans up.
5. `test/window_configure.py` reinstalls the default service-scoped policy.

`test/conftest.py` supplies a function-scoped policy factory. Each gateway case
tracks and removes its own plugins, routes, and services even if setup or an
assertion fails. The schema case creates its own policy rather than depending
on another test. Shared helpers live in `test/window_support.py` and
`test/common.py`. Default pytest selection excludes the restart verifier because
it requires the preceding preparation and host restart. Do not run cases in
parallel: outage tests pause the shared Redis instance.

Verified on 2026-11-07: Lua syntax and unit checks, real shared-memory and Redis
checks, pytest gateway scenarios, and restart verification passed. Parameter
sweeps repeat checks with different inputs; their iterations are not reported as
separate test cases. The uv image built, the updated startup command
passed, and default collection selected 14 of 15 Python cases. Lockfile consistency,
Python/shell syntax, Compose configuration, and documentation links passed.
The optional plugin deployment image and integration workflow were also validated
before the Python migration; their plugin behavior and workflow are unchanged.

GitHub Actions uses the same start/test/stop entry points. Benchmarks remain a
separate k6 command using round-robin DNS across all three gateways; correctness
CI does not run k6 or enforce a throughput target. See the
[test case and benchmark guide](../../../test/README.md) for exact assertions,
selection commands, benchmark modes, metrics, and coverage limits.
