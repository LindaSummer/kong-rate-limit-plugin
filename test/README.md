# Test cases and benchmarks

This directory contains the echo backend, policy setup, automated correctness
tests, and k6 benchmark for `rate-limiting-sliding-window`. Run all commands below
from the repository root with Docker Engine and Compose v2 available.

## Environment and full suite

The Compose environment uses three official `kong:3.9.4` gateways with two workers
each, PostgreSQL for configuration, Redis 7.4 for counters, and a Python echo
backend. Tests call all three gateways directly; benchmarks use their shared
`gateway` DNS alias.

```sh
./run.sh start
./run.sh test
./run.sh stop
```

`start` installs a service policy shared by `/window/a` and `/window/b`: five
attempts per fixed second and 30 per approximate sliding minute per ClientId.
`test` runs the stages below sequentially, restarts `kong1`, and reapplies that
default policy. The suite temporarily pauses Redis, flushes its Lua script cache,
and creates temporary Kong entities. Do not run tests or benchmarks concurrently
against the same Compose project. `stop` retains PostgreSQL and Redis volumes.

The current suite passed on 2026-11-07: Lua syntax and unit checks, shared-memory
and Redis checks, pytest gateway scenarios, and restart verification. Fixed/sliding algorithms and upstream 429/503
responses have separate parameterized cases. Default pytest selection runs the
14 gateway cases; the runner selects the restart case after restarting `kong1`.

## Files and execution order

| File | Purpose |
| --- | --- |
| [check_syntax.lua](check_syntax.lua) | Compile plugin and test Lua files with Kong's LuaJIT without executing them. |
| [window_unit.lua](window_unit.lua) | Deterministic calculations, identity encoding, mocked failure paths, and breaker transitions. |
| [window_shared.lua](window_shared.lua) | Real shared-memory locking, probe contention, and memory exhaustion in a separate `resty` process. |
| [window_redis.lua](window_redis.lua) | Real Redis pipelines, counters, concurrency, script-cache recovery, and field expiration. |
| [test_integration.py](test_integration.py) | HTTP behavior and live configuration across three gateways. |
| [window_restart.py](window_restart.py) | Prepare the budget before the host restarts a gateway. |
| [test_restart.py](test_restart.py) | Verify the Redis budget survives the restart and clean up. |
| [conftest.py](conftest.py) / [window_support.py](window_support.py) | Per-test cleanup fixture, policy factory, and HTTP scenario helpers. |
| [pyproject.toml](pyproject.toml) / [uv.lock](uv.lock) / [Dockerfile](Dockerfile) | Python 3.12 test project, locked dependencies, and uv test image. |
| [window_configure.py](window_configure.py) | Install the default policy and wait for all gateways to adopt it. |
| [common.py](common.py) | Standard-library HTTP, Admin API, Redis RESP, and setup helpers. |
| [echo.py](echo.py) | Echo requests and simulate upstream 429/503 responses. |
| [benchmark.js](benchmark.js) | k6 load generation, checks, gateway distribution, and JSON report. |

## Unit coverage

`window_unit.lua` supplies explicit timestamps. Storage and locking are mocked
where needed; algorithm and identity calculations use the actual plugin modules.
Some checks repeat the same scenario with different input parameters; the script
reports a pass for the unit suite without presenting those repetitions as
additional test cases.

| Area | Assertions |
| --- | --- |
| Fixed windows | Capacity, exact boundaries, time until reset, threshold increases/decreases retaining usage. |
| Sliding windows | Previous-window weighting, idle windows, retry delays within/across boundaries. A parameter sweep checks that the next attempt succeeds at the reported delay and fails one millisecond earlier. |
| Invalid state | Corrupt counters and unknown algorithms fail; counters exceeding configured limit bounds remain valid attempt counts. |
| Identity | Raw 16-byte UUIDs, zero UUIDs for absent scopes, 104-byte versioned Redis keys, canonical dimension order, threshold excluded from signatures, unambiguous tuple encoding, window isolation, hash fields and TTL descriptors, duplicate-key rejection. |
| Local accounting | Denials increment other limits; a later failure preserves earlier increments; locks are released on calculation/storage errors; full memory fails closed. |
| Redis transport | Mixed successful/NOSCRIPT replies retry only missing scripts; ambiguous pipeline timeouts are not replayed. |
| Circuit breaker | Initial Redis selection, cooldown, one probe lease, stale-response fencing, exponential backoff capped at four seconds, and 10/25/50/100 percent recovery stages. |
| Handler failures | Redis receives one Kong timestamp and fallback reuses it. Timeouts, corrupt Redis state, and authentication errors trigger local enforcement. A local enforcement failure returns 503. |

## Real shared-memory cases

`window_shared.lua` runs with private 1 MB `sliding_ratelimit` and `kong_locks`
dictionaries, so its exhaustion test does not fill a live gateway's dictionary.

1. Forty concurrent attempts against two limits of seven admit exactly seven;
   all 40 attempts remain counted.
2. Forty concurrent half-open selections obtain exactly one Redis probe lease.
3. Filling the dictionary causes a new budget allocation to fail while preserving
   the existing budget, without evicting it.

## Real Redis cases

| Group | Setup and expected result |
| --- | --- |
| Timestamp and attempts | Binary keys containing NUL bytes and an explicit timestamp different from Redis wall time. Fixed/sliding limits are pipelined; denied attempts increment both counters. |
| Partial failure | Corrupt the second counter. Evaluation fails, the first counter remains incremented, and corrupt data is not silently reset. |
| NOSCRIPT recovery | Flush Redis's script cache. Missing commands recover with exactly one increment per counter and a positive field TTL. |
| Sliding weighting | Reject corrupt previous/current fields; seed a previous count of ten in a 1,000 ms window. At timestamp 1,500, five attempts pass and the sixth fails; the next attempt succeeds at its reported 200 ms retry delay. |
| Concurrent accounting | For each algorithm, 30 concurrent attempts at one timestamp with limit seven admit exactly seven and store a count of 30. |
| Field expiry | An 80 ms sliding window retains current and previous fields in one hash. The previous field expires independently, then the final field expires and Redis removes the empty hash. |

## Gateway integration cases

The HTTP suite uses unique temporary services, routes, and callers. A pytest
fixture removes each test's entities even if setup or assertions fail. Policy
readiness is checked across all gateways with an allowance of 30 seconds. Most quota tests use a one-day window to avoid unrelated
boundary crossings. Bursts use 24 client threads and distribute requests across
the three gateways.

| pytest case | Scenario and expected result |
| --- | --- |
| `test_shared_budgets[fixed_window]`, `[sliding_window]` | For each algorithm, 60 attempts with limit 12 yield exactly 12 HTTP 200s; the rest are 429. All gateways participate. A second route on the same service shares the exhausted budget; another client is independent. Denials include numeric `Retry-After` and `Cache-Control: no-store`; missing required ClientId returns 400. |
| `test_composite_dimensions` | With limit one on `[client_id,path]`, query strings and `/./` normalization use the same bucket. Another path/client is independent. Duplicate and oversized ClientId headers return 400. |
| `test_method_route_service` | Limit one on `[client_id,method,route,service]`. A child path on the same route shares usage; POST and a second route receive separate buckets. |
| `test_simultaneous_limits` | A path limit of one plus a client limit of two count every attempt. A request denied by the path limit also consumes the broader client budget, blocking a different path. |
| `test_route_precedence` | A route limit of three overrides a service limit of one. The route admits three then denies; a sibling route uses the separate service budget. |
| `test_live_updates` | Increase a threshold from two to three after one attempt: only two further attempts pass. Change the window shape: new state is selected. |
| `test_schema_validation` | Reject zero limits, unknown algorithms, repeated dimensions, duplicate limits with reordered dimensions, windows above one day, and Consumer attachment. This case creates its own policy. |
| `test_ip_budget` | A fixed IP limit of four per second admits exactly four of 30 attempts across gateways. ClientId is unnecessary; a sibling route shares the IP budget. |
| `test_upstream_headers[429]`, `[503]` | Backend errors preserve status and `Retry-After`, both numeric (`7`) and HTTP-date values. |
| `test_redis_outage_recovery[fixed_window]`, `[sliding_window]` | For each algorithm, pause Redis for 2.2 seconds. Sixty attempts with limit four use local enforcement and admit exactly four per node across its workers. Redis traffic resumes through recovery and reaches stable Redis mode. |
| `test_redis_authentication_fallback` | Configure an incorrect Redis password. Requests use local budgets, admitting five per node from a 60-request burst; statuses are 200/429 rather than unavailable responses. |

`echo.py` normally returns 200. Only paths beginning with `/upstream429` or
`/upstream503` simulate upstream errors and set `Retry-After`; a path containing
`date` selects the HTTP-date form. This tests header passthrough separately from
the plugin's own rejection headers.

## Restart case

`window_restart.py prepare` creates a sliding limit of two per day and consumes
one admission. The runner restarts `kong1`, waits for health, then selects
`test_redis_budget_survives_gateway_restart` in `test_restart.py`.
Exactly one more admission is available on the restarted gateway; subsequent
requests on all three gateways return 429. The Redis budget survives the restart.
The pytest verification fixture removes the temporary entities and fixture key.
The restart test is marked `restart` and excluded from ordinary pytest runs.

## pytest and uv

The separate test image uses Python 3.12, pins uv 0.9.4, and installs pytest
8.4.2 and its transitive dependencies from `uv.lock` with `uv sync --locked --no-dev`. Its environment lives at `/opt/test-venv`, outside the
read-only `/test` source mount. Runtime commands use `uv run --frozen --no-sync`,
so dependency installation happens during the build rather than on every test run.
The host needs Docker/Compose; host uv is optional. `start` and `test` build the
test image once before running Python commands. Pytest writes its cache under
`/tmp/kong-window-pytest-cache`, keeping the source mount read-only.

The runner executes Lua checks first, then the 14 gateway cases, restart fixture
preparation, the host-controlled restart, the single restart verifier, and default
policy setup. GitHub Actions invokes the same runner and uploads its output and
container logs; it does not need a separate host Python or uv installation.

```sh
# Collect cases without contacting Kong or Redis.
docker compose run --build --rm --no-deps tests uv run --frozen --no-sync pytest --collect-only -q

# Run one named case (including both algorithm parameters).
docker compose run --build --rm --no-deps tests uv run --frozen --no-sync pytest -v -k shared_budgets

# Select exactly one algorithm case.
docker compose run --build --rm --no-deps tests uv run --frozen --no-sync pytest -v 'test_integration.py::test_shared_budgets[sliding_window]'

# Resolve/update dependencies after editing pyproject.toml (host uv and Python 3.12).
uv lock --project test
```

For local collection without containers, run
`uv run --directory test --locked pytest --collect-only -q`. Actual HTTP cases use
Compose DNS names by default; the container commands above supply that network.
Tests run serially; do not add pytest-xdist workers because outage cases pause
shared Redis. Default selection excludes `restart`; `./run.sh test` explicitly
selects it after preparation and the host-controlled restart.

## Running individual checks

Start the stack first. These commands use the same mounted files as the full suite:

```sh
docker compose exec -T kong1 /usr/local/openresty/luajit/bin/luajit /test/check_syntax.lua
docker compose exec -T kong1 /usr/local/openresty/bin/resty /test/window_unit.lua
docker compose exec -T kong1 /usr/local/openresty/bin/resty --shdict 'sliding_ratelimit 1m' --shdict 'kong_locks 1m' /test/window_shared.lua
docker compose exec -T kong1 /usr/local/openresty/bin/resty /test/window_redis.lua
docker compose run --build --rm --no-deps tests uv run --frozen --no-sync pytest -v
```

Use `./run.sh test` for the complete prepare/restart/verify sequence. After plugin
Lua edits, restart all gateways before HTTP testing; workers cache loaded modules.

## Benchmark scenarios

The benchmark uses k6 1.3.0 and GET requests with a five-second timeout. Defaults
are 20 virtual users for 30 seconds against `http://gateway:8000/window/a`. It
generates load continuously without a per-request sleep. Run `./run.sh start`
first: the benchmark command starts containers but does not install a policy.

| Case | Command | What it exercises |
| --- | --- | --- |
| Shared client, new connections | `./run.sh benchmark --vus 20 --duration 30s` | One fresh ClientId shared by every virtual user; small default quotas make this mainly a rejection-path load test. |
| Per-user clients, keepalive | `BENCH_KEEPALIVE=true BENCH_CLIENT_MODE=per-vu ./run.sh benchmark --vus 30 --duration 30s` | Independent client budgets per virtual user and reused TCP connections; each user's budget can still exhaust. |
| Unique client per request | `BENCH_CLIENT_MODE=unique ./run.sh benchmark --vus 5 --duration 10s` | Fresh ClientId for every iteration, exercising admission and upstream forwarding under the default policy. Creates many Redis buckets. |
| Existing target policy | `BENCH_URL=http://gateway:8000/my-route ./run.sh benchmark --vus 20 --duration 30s` | A policy already configured on another route. The script always supplies `X-Client-Id`; the target must return the expected diagnostics. |

`BENCH_CLIENT_ID` sets the base caller identity; otherwise each run generates a
new one. In `per-vu` mode the script appends the VU number, and in `unique` mode
it appends both VU and iteration numbers. `BENCH_VUS` and `BENCH_DURATION` supply
defaults; CLI `--vus` and `--duration` override them. Benchmarking does not modify
installed policies or reset counters. Reusing a base ClientId can reuse budgets.

### Compare against Kong's bundled plugin

The [recorded baseline report](benchmarks/2026-11-07-redis-baseline.md) contains
measured results and links to the raw trials for the recorded source revision.

```sh
./run.sh benchmark-compare --vus 20 --duration 15 --warmup 3 --repeats 3 \
  --output /tmp/kong-rate-limit-comparison
```

This command also requires host Python 3 (standard library only). It starts the
stack, builds the uv test image, and creates temporary service-scoped policies
for the bundled `rate-limiting` plugin and our fixed/sliding implementations.
It preserves existing demo policies and removes each trial's entities afterward.

All three use one daily limit on `X-Client-Id`, backed by the same Redis. The
bundled plugin uses `policy=redis`, `sync_rate=-1`, and `fault_tolerant=false`.
Our limit uses `[client_id]`, `window_ms=86400000`, and the selected algorithm.
The daily window avoids short reset boundaries during samples. Trials refuse
to start too close to the UTC day boundary.

Two workloads isolate response paths:

- **Admitted:** a shared caller with limit 1,000,000; every measured response must
  be 200. A shared caller keeps key cardinality comparable across plugins.
- **Throttled:** a shared caller with limit one, consumed before warmup; every
  measured response must be 429.

Each trial has a separate warmup followed by a measured sample. Fresh policies
and caller identities prevent carryover between trials. Runs are serial, and
implementation order rotates across repeats. Both plugins receive the same
`post-function` header observer to identify gateways on 200 and 429 responses.
All three gateways must receive traffic. Custom responses must report Redis
mode; the baseline's standard limit header is checked because the bundled
plugin does not expose an equivalent mode header. Baseline `redis_requests` is
`null`, rather than claiming an observed Redis-mode count.

The output directory contains raw warmup/measured JSON and stderr logs,
`report.json` with environment/image IDs, source revision, per-trial policies and
results, and `report.md` suitable for a PR comment. Warmups are excluded from
reported rates. Summary tables use median request rates with min/max ranges;
latency columns are medians of per-trial p95/p99 values, not pooled percentiles.
Use `--keepalive` for a separate connection-reuse experiment; defaults open a
new connection per request. Do not mix keepalive settings in one comparison.

The fixed-window comparison is the closest functional baseline. Sliding windows
perform different accounting, so their result measures that implementation's
cost rather than equivalent quota accuracy. All containers share the host without
CPU pinning, and admitted requests include Python echo processing. Interpret
results as end-to-end measurements for the recorded host and settings.

### DNS and connection behavior

All gateways register the `gateway` alias. k6 uses IPv4, `roundRobin` selection,
and an infinite DNS cache TTL to keep Docker's shuffled response order from
changing its address set during a run. Restart the benchmark after recreating
gateways. With the default `BENCH_KEEPALIVE=false`, each request opens a TCP
connection. With keepalive enabled, DNS selection happens when a new connection
is needed; individual requests need not rotate evenly.

### Pass conditions and reported metrics

HTTP 200 and 429 are both expected. A run passes only when every response has a
recognized `X-Kong-Instance`, Redis enforcement is active, no HTTP failure or
failed check occurs, every gateway receives traffic, and local-mode count is zero.
There is no throughput or latency target and no exact one-third distribution
assertion. This benchmark is not an outage test; fallback deliberately fails its
Redis-mode checks.

The JSON report includes:

- Target, identity mode, keepalive/DNS settings, duration, and virtual users.
- Total requests and requests/second; admitted 200, throttled 429, unexpected,
  Redis-mode, and local-mode counts.
- Request count and percentage for each gateway.
- Average, median, p95, p99, and maximum response duration, connection time, and
  iteration duration. `latency_ms` excludes connection setup; `iteration_ms`
  includes it.
- Failed check count and whether all thresholds passed.

```sh
./run.sh benchmark --quiet --vus 20 --duration 30s > /tmp/kong-benchmark.json
```

Preserve the report with the source revision, policy, host resources, and load
settings when comparing runs. Results include gateway processing, Redis, network
costs, all applicable plugins, and the echo backend for admitted requests. A
mostly-429 run does not measure admitted-request throughput. This README defines
the cases and report format. `benchmark-compare` produces a measured baseline
for a particular host and run; it is not a production capacity guarantee.

## Coverage limits

Sliding-window checks validate the two-counter approximation, not an exact rolling
window. The suite does not establish a strict global quota during Redis outages,
recovery, clock skew, or configuration propagation: fallback budgets are per node.
It does not cover async synchronization, Redis Cluster, or production-scale
capacity. GitHub Actions runs the correctness suite; k6 benchmarks are separate.
