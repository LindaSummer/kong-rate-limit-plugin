"""Run serial, alternating k6 comparisons using temporary Kong policies."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import statistics
import subprocess
import time

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ["docker", "compose"]
IMPLEMENTATIONS = ["direct", "no_plugin", "kong_sync", "kong_async", "fixed_window", "sliding_window"]


def command(args, *, stdin=None):
    result = subprocess.run(args, cwd=ROOT, text=True, input=stdin, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {args}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def configure(*args, stdin=None):
    return command(COMPOSE + ["run", "--rm", "--no-deps", "-T", "tests", "uv", "run",
                              "--frozen", "--no-sync", "python", "benchmark_configure.py", *args], stdin=stdin)


def run_k6(fixture, implementation, workload, vus, duration, keepalive, output, peers):
    args = COMPOSE + ["run", "--rm", "--no-deps", "-T"]
    kind = {"direct": "direct", "no_plugin": "none"}.get(implementation,
            "kong" if implementation.startswith("kong_") else "custom")
    env = {"BENCH_URL": ("http://benchmark-upstream:8080" if implementation == "direct" else "http://gateway:8000") + fixture["path"],
           "BENCH_CLIENT_MODE": "shared", "BENCH_CLIENT_ID": fixture["client"],
           "BENCH_KEEPALIVE": str(keepalive).lower(),
           "BENCH_IMPLEMENTATION": kind, "BENCH_GATEWAY_IPS": json.dumps(peers),
           "BENCH_EXPECTED_STATUS": "200" if workload == "admitted" else "429",
           "BENCH_EXPECTED_LIMIT": str(fixture["threshold"])}
    for name, value in env.items():
        args.extend(["-e", f"{name}={value}"])
    args += ["benchmark", "run", "--quiet", "--vus", str(vus), "--duration", f"{duration}s", "/test/benchmark.js"]
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True)
    output.write_text(result.stdout)
    output.with_suffix(".log").write_text(result.stderr)
    if result.returncode:
        raise RuntimeError(f"k6 failed; see {output} and {output.with_suffix('.log')}")
    report = json.loads(result.stdout)
    assert report["thresholds_passed"] and not report["unexpected"] and not report["failed_checks"], report
    return report


def resources():
    """Record effective Compose caps and actual persistent container settings."""
    config = json.loads(command(COMPOSE + ["--profile", "*", "config", "--format", "json"]))
    settings = {}
    for service, conf in config["services"].items():
        if service not in ("kong1", "kong2", "kong3", "redis", "postgres", "echo", "benchmark-upstream", "benchmark", "tests", "migrations"):
            continue
        settings[service] = {"cpus": float(conf.get("cpus", 0)),
                             "memory_bytes": int(conf.get("mem_limit", 0)),
                             "cpuset": conf.get("cpuset", "")}
        if service in ("kong1", "kong2", "kong3", "redis", "postgres", "echo", "benchmark-upstream"):
            ids = command(COMPOSE + ["ps", "--quiet", service]).split()
            if ids:
                container = json.loads(command(["docker", "inspect", ids[0]]))[0]
                settings[service]["image"] = container["Image"]
                actual = container["HostConfig"]
                settings[service]["addresses"] = [network["IPAddress"] for network in
                    container["NetworkSettings"]["Networks"].values() if network.get("IPAddress")]
                settings[service]["observed"] = {
                    "nano_cpus": actual["NanoCpus"], "cpu_quota": actual["CpuQuota"],
                    "cpu_period": actual["CpuPeriod"], "memory_bytes": actual["Memory"],
                    "cpuset": actual["CpusetCpus"]}
    return settings


def markdown(report):
    env = report["environment"]
    lines = ["## Rate-limiter benchmark: Kong baseline vs custom windows", "",
             f"Source revision: `{report['revision']}` (dirty: `{report['dirty']}`).",
             f"Executed: {report['timestamp_utc']}.", "",
             "Kong 3.9.4, Redis 7.4, k6 1.3.0; three Kong nodes with two workers each.",
             f"Docker host: {env['cpus']} logical CPUs, {env['memory_bytes'] / 2**30:.1f} GiB RAM, {env['architecture']}.",
             f"{report['vus']} VUs; {report['warmup_seconds']}s unreported warmup then {report['duration_seconds']}s measured per trial; "
             f"{report['repeats']} repeats. Keepalive: `{report['keepalive']}`. "
             "Gateway traffic uses IPv4 DNS round robin; the direct control bypasses Kong. Runs are serial and implementation order rotates per repeat.",
             f"Kong async sync_rate: {report['kong_async_sync_rate']} seconds. Our implementations use synchronous Redis accounting.", "",
             "Upstream: Go net/http, constant 512-byte JSON, HTTP/1.1 keepalive, no per-request logging.",
             f"Redis timeout for every limiter: {report['redis_timeout_ms']} ms.", "",
             "### CPU and memory settings", "",
             "CPU values are per-container quotas, not dedicated cores; zero means no explicit cap. All containers share the host.", "",
             "| Service | CPU quota | Memory limit | CPU pinning |",
             "| --- | ---: | ---: | --- |"]
    for service, setting in report["resources"].items():
        cpus = str(setting["cpus"]) if setting["cpus"] else "Uncapped"
        memory = f"{setting['memory_bytes'] / 2**20:.0f} MiB" if setting["memory_bytes"] else "Uncapped"
        lines.append(f"| {service} | {cpus} | {memory} | {setting['cpuset'] or 'None'} |")
    lines += ["", "### Results", "",
             "| Workload | Plugin | Median req/s (min–max) | vs Kong sync | Median p95 ms | Median p99 ms | Measured 200 / 429 |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for workload in ("admitted", "throttled"):
        baseline = statistics.median(r["report"]["requests_per_second"] for r in report["runs"]
                                     if r["workload"] == workload and r["implementation"] == "kong_sync")
        for implementation in IMPLEMENTATIONS:
            if workload == "throttled" and implementation in ("direct", "no_plugin"):
                continue
            runs = [r["report"] for r in report["runs"] if r["workload"] == workload and r["implementation"] == implementation]
            rates = [r["requests_per_second"] for r in runs]
            median = statistics.median(rates)
            p95 = statistics.median(r["latency_ms"]["p(95)"] for r in runs)
            p99 = statistics.median(r["latency_ms"]["p(99)"] for r in runs)
            label = {"direct": "Direct Go backend", "no_plugin": "Kong without plugins", "kong_sync": "Kong sync", "kong_async": "Kong async"}.get(implementation, "Custom " + implementation)
            lines.append(f"| {workload} | {label} | {median:,.0f} ({min(rates):,.0f}–{max(rates):,.0f}) | "
                         f"{median / baseline:.2f}× | {p95:.3f} | {p99:.3f} | "
                         f"{sum(r['admitted_200'] for r in runs):,} / {sum(r['throttled_429'] for r in runs):,} |")
    lines += ["", "Conditions and interpretation:", "",
              "- Limiter modes use one service-scoped Redis-backed daily limit on `X-Client-Id`: 1,000,000 for admitted traffic, "
              "one pre-consumed admission for throttled traffic. All measured responses must match the workload's expected status.",
              "- Kong baselines: bundled `rate-limiting`, `policy=redis`, `fault_tolerant=false`; "
              "sync uses `sync_rate=-1`, async uses the interval listed above. Ratios use Kong sync as the denominator. "
              "Custom: one fixed or approximate sliding window, 86,400,000 ms, dimension `client_id`.",
              "- Async uses per-worker cached usage and periodic Redis synchronization. Its throughput has different consistency guarantees. "
              "The throttled caller is primed until worker responses settle; this is a rejection-path benchmark, not an async overshoot or quota-accuracy test.",
              "- The fixed-window run is the closest functional comparison. The sliding-window run measures a different algorithm; "
              "it is not evidence that the plugins enforce identical rolling quotas.",
              "- Direct Go traffic bypasses Kong; the no-plugin Kong control attaches no plugins. These controls have admitted traffic only. "
              "All gateway samples use peer IPs mapped to Docker containers for attribution, with no observer plugin. "
              "Every gateway trial must hit all three nodes; custom responses must report Redis mode. "
              "The bundled plugin has no Redis-mode diagnostic: its configured policy, limiter header, fail-closed behavior, "
              "and absence of request errors are checked instead.",
              "- Values are medians across trials; p95/p99 columns are medians of per-trial percentiles, not pooled percentiles. "
              "Response latency excludes TCP connection setup. Raw reports include connection and iteration times, per-node distribution, and individual trials.",
              "- Gateways, Redis, Go backend, and load generator share this Docker host without CPU pinning. "
              "Admitted requests include the constant-response Go backend; denied requests do not. These are end-to-end results for this host, "
              "not isolated plugin CPU measurements or production capacity guarantees. "
              "The echo service in the resource table is the separate Python integration/demo backend and is not a benchmark target.",
              "- Policies are fresh per trial and removed afterward; existing demo routes and data are preserved. "
              "Warmup requests are excluded from reported metrics but consume the same trial budget. "
              "Fresh same-day buckets have no previous-window traffic; window transitions are not measured.", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vus", type=int, default=20)
    parser.add_argument("--duration", type=int, default=15, help="Measured seconds per trial")
    parser.add_argument("--warmup", type=int, default=3, help="Warmup seconds per trial")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--keepalive", action=argparse.BooleanOptionalAction, default=True,
                        help="Reuse HTTP connections (default); disable for connection-churn experiments")
    parser.add_argument("--kong-async-sync-rate", type=float, default=0.1, help="Async Redis sync interval in seconds")
    parser.add_argument("--redis-timeout-ms", type=int, default=1000, help="Same Redis timeout for all limiter modes")
    parser.add_argument("--output", type=Path, default=Path("/tmp/kong-rate-limit-comparison"))
    args = parser.parse_args()
    if min(args.vus, args.duration, args.warmup, args.repeats) < 1:
        parser.error("vus, duration, warmup and repeats must be positive")
    if not 0.02 <= args.kong_async_sync_rate <= 5:
        parser.error("kong-async-sync-rate must be between 0.02 and 5 seconds")
    if not 1 <= args.redis_timeout_ms <= 60000:
        parser.error("redis-timeout-ms must be between 1 and 60000")
    if args.duration + args.warmup > 120:
        parser.error("Each trial must be at most 120 seconds to keep the admitted budget practical")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    # The benchmark profile is not started by compose up. A fresh runner has
    # never pulled k6, but image provenance is collected before its first run.
    command(COMPOSE + ["pull", "benchmark"])
    info = json.loads(command(["docker", "info", "--format", "{{json .}}"]))
    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "revision": command(["git", "rev-parse", "HEAD"]).strip(),
              "dirty": bool(command(["git", "status", "--porcelain"]).strip()),
              "vus": args.vus, "duration_seconds": args.duration, "warmup_seconds": args.warmup,
              "repeats": args.repeats, "keepalive": args.keepalive,
              "kong_async_sync_rate": args.kong_async_sync_rate,
              "redis_timeout_ms": args.redis_timeout_ms,
              "environment": {"cpus": info["NCPU"], "memory_bytes": info["MemTotal"],
                              "architecture": info["Architecture"], "kernel": platform.release(),
                              "docker_version": info["ServerVersion"]}, "resources": resources(), "images": {}, "runs": []}
    report["upstream"] = {"url": "http://benchmark-upstream:8080", "implementation": "Go net/http",
                          "response_bytes": 512, "per_request_logging": False}
    report["images"]["benchmark-upstream"] = report["resources"]["benchmark-upstream"]["image"]
    for tag in ("kong:3.9.4", "redis:7.4-alpine", "grafana/k6:1.3.0"):
        report["images"][tag] = command(["docker", "image", "inspect", tag, "--format", "{{.Id}}"]).strip()
    peers = {address: service for service, setting in report["resources"].items()
             if service in ("kong1", "kong2", "kong3") for address in setting.get("addresses", [])}
    assert len(peers) >= 3, "Gateway peer addresses unavailable"
    for repeat in range(args.repeats):
        for workload in ("admitted", "throttled"):
            eligible = IMPLEMENTATIONS if workload == "admitted" else IMPLEMENTATIONS[2:]
            offset = (repeat * 2 if workload == "admitted" else repeat) % len(eligible)
            order = eligible[offset:] + eligible[:offset]
            for implementation in order:
                # Daily windows must not reset during a warmup/measured sample.
                if 86400 - time.time() % 86400 < args.duration + args.warmup + 45:
                    raise RuntimeError("Too close to UTC day boundary; rerun after the boundary")
                label = f"{repeat + 1}-{workload}-{implementation}"
                print(f"Starting {label}", flush=True)
                fixture = json.loads(configure("prepare", "--implementation", implementation, "--workload", workload,
                                               "--sync-rate", str(args.kong_async_sync_rate),
                                               "--redis-timeout-ms", str(args.redis_timeout_ms)))
                try:
                    run_k6(fixture, implementation, workload, args.vus, args.warmup, args.keepalive, output / (label + "-warmup.json"), peers)
                    result = run_k6(fixture, implementation, workload, args.vus, args.duration, args.keepalive, output / (label + ".json"), peers)
                    report["runs"].append({"repeat": repeat + 1, "workload": workload,
                                           "implementation": implementation, "plugin": fixture["plugin"],
                                           "config": fixture["config"], "report": result})
                    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
                    print(f"Finished {label}: {result['requests_per_second']:,.0f} req/s", flush=True)
                finally:
                    configure("cleanup", stdin=json.dumps(fixture["entities"]))
    (output / "report.md").write_text(markdown(report))
    print(f"Reports: {output / 'report.json'} and {output / 'report.md'}", flush=True)


if __name__ == "__main__":
    main()
