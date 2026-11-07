## Rate-limiter benchmark: Kong baseline vs custom windows

Source revision: `a60172cb95a2c0b452d0cc0df91c62c8a04c605e` (dirty: `False`).
Executed: 2026-11-07T14:43:25.110804+00:00.

Kong 3.9.4, Redis 7.4, k6 1.3.0; three Kong nodes with two workers each.
Docker host: 32 logical CPUs, 123.4 GiB RAM, x86_64.
20 VUs; 3s unreported warmup then 15s measured per trial; 3 repeats. Keepalive: `False`. IPv4 DNS round robin across `gateway`; runs are serial and implementation order rotates per repeat.

| Workload | Plugin | Median req/s (min–max) | vs Kong | Median p95 ms | Median p99 ms | Measured 200 / 429 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| admitted | Kong bundled rate-limiting | 4,032 (2,677–4,179) | 1.00× | 9.869 | 14.570 | 167,326 / 0 |
| admitted | Custom fixed_window | 3,549 (2,930–3,910) | 0.88× | 10.414 | 15.173 | 165,498 / 0 |
| admitted | Custom sliding_window | 3,600 (2,932–3,927) | 0.89× | 10.621 | 15.043 | 165,169 / 0 |
| throttled | Kong bundled rate-limiting | 42,114 (42,063–42,564) | 1.00× | 0.571 | 1.229 | 0 / 1,901,325 |
| throttled | Custom fixed_window | 30,641 (30,468–30,878) | 0.73× | 1.420 | 3.260 | 0 / 1,379,963 |
| throttled | Custom sliding_window | 31,476 (31,344–31,661) | 0.75× | 1.350 | 3.069 | 0 / 1,417,367 |

Conditions and interpretation:

- One service-scoped Redis-backed daily limit on `X-Client-Id`: 1,000,000 for admitted traffic, one pre-consumed admission for throttled traffic. All measured responses must match the workload's expected status.
- Kong baseline: bundled `rate-limiting`, `policy=redis`, `sync_rate=-1`, `fault_tolerant=false`. Custom: one fixed or approximate sliding window, 86,400,000 ms, dimension `client_id`.
- The fixed-window run is the closest functional comparison. The sliding-window run measures a different algorithm; it is not evidence that the plugins enforce identical rolling quotas.
- Both configurations attach the same post-function header observer to identify each gateway, including rejected responses. Every trial must hit all three nodes; custom responses must report Redis mode. The bundled plugin has no Redis-mode diagnostic: its configured policy, limiter header, fail-closed behavior, and absence of request errors are checked instead.
- Values are medians across trials; p95/p99 columns are medians of per-trial percentiles, not pooled percentiles. Response latency excludes TCP connection setup. Raw reports include connection and iteration times, per-node distribution, and individual trials.
- Gateways, Redis, echo backend, and load generator share this Docker host without CPU pinning. Admitted requests include the Python echo backend; denied requests do not. These are end-to-end results for this host, not isolated plugin CPU measurements or production capacity guarantees.
- Policies are fresh per trial and removed afterward; existing demo routes and data are preserved. Warmup requests are excluded from reported metrics but consume the same trial budget.

Observed results:

- Throttled throughput is consistently lower for the custom plugin: roughly 27%
  below Kong for fixed windows and 25% below Kong for sliding windows.
- Admitted medians are lower for the custom plugin, but trial ranges overlap
  substantially. The shared host and Python backend limit what can be concluded
  about plugin-only overhead from these samples.
- All measured trials passed their checks: expected statuses only, all three
  gateways reached, and no custom-plugin local fallback.
- These fresh, same-day buckets do not cross a window boundary; the sliding
  previous-window count is empty. This is not a benchmark of window transitions,
  multiple simultaneous limits, async policies, or high-cardinality identities.

[Raw measured trials and environment metadata](2026-11-07-redis-baseline.json).
