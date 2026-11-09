"""Measure window-boundary bursts at an upstream, then plot actual arrivals.

Run from the repository root:
uv run --no-project --with matplotlib==3.10.8 --with pyarrow==22.0.0 python test/illustrate_windows.py
Generated data and plots stay outside Git. All Docker resources are isolated.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from common import PROXIES, admin, http, probe_gateways

ROOT = Path(__file__).resolve().parent.parent
ALGORITHMS = ("fixed_window", "sliding_window")


def wait_until(target_ms):
    while True:
        remaining = target_ms / 1000 - time.time()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.05))


def measure(args):
    """Container-side worker; JSON on stdout, progress on stderr."""
    run = uuid.uuid4().hex
    entities = {"plugins": [], "routes": [], "services": []}
    paths = {}
    try:
        for algorithm in ALGORITHMS:
            name = f"illustration-{run}-{algorithm}"
            service = admin("/services", "POST", {"name": name, "url": "http://window-observer:8080"})
            entities["services"].append(service["id"])
            path = "/" + name
            route = admin("/routes", "POST", {"paths": [path], "strip_path": False,
                                              "service": {"id": service["id"]}})
            entities["routes"].append(route["id"])
            plugin = admin(f'/services/{service["id"]}/plugins', "POST", {
                "name": "rate-limiting-sliding-window", "config": {
                    "redis": {"host": "redis", "timeout": 1000},
                    "limits": [{"algorithm": algorithm, "window_ms": args.window_ms,
                                "limit": args.limit, "dimensions": ["client_id"]}]}})
            entities["plugins"].append(plugin["id"])
            paths[algorithm] = path

        started = time.monotonic()
        stable = 0
        while time.monotonic() - started < 30:
            results = [r for path in paths.values() for r in probe_gateways(path)]
            ready = all(status == 200 and headers.get("X-RateLimit-Mode") == "redis"
                        and headers.get("X-RateLimit-Remaining") == str(args.limit - 1)
                        for status, headers, _ in results)
            stable = stable + 1 if ready else 0
            if stable >= 5 and time.monotonic() - started >= 4:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("Experiment policies did not converge on all gateways")

        def burst(case, phase, count, target_ms, pool, node_offset=0):
            wait_until(target_ms)
            print(f"Sending {case}/{phase}: {count} attempts per algorithm", file=sys.stderr, flush=True)

            def request(job):
                algorithm, index = job
                request_id = uuid.uuid4().hex
                sent_ns = time.time_ns()
                status, headers, _ = http(PROXIES[(index + node_offset) % len(PROXIES)] + paths[algorithm], headers={
                    "X-Client-Id": f"{run}-{case}", "X-Experiment-Request-Id": request_id})
                response_ns = time.time_ns()
                assert status in (200, 429), (algorithm, status)
                assert headers.get("X-RateLimit-Mode") == "redis", headers
                return {"algorithm": algorithm, "phase": phase, "request_id": request_id,
                        "sent_ns": sent_ns, "response_ns": response_ns, "request_path": paths[algorithm],
                        "scheduled_ms": target_ms,
                        "kong_ms": int(headers["X-RateLimit-Time-Ms"]),
                        "status": status, "retry_after_seconds": int(headers["Retry-After"]) if status == 429 else None,
                        "gateway": headers["X-Kong-Instance"]}

            # Interleave policies, use all three gateways, and reuse worker threads.
            jobs = [(algorithm, i) for i in range(count) for algorithm in ALGORITHMS]
            return list(pool.map(request, jobs))

        def retry_client(algorithm, behavior, start_ms, end_ms):
            # Each behavior has an independent budget and the same initial overload.
            wait_until(start_ms)
            records = []
            next_send_ns = time.time_ns()
            while time.time_ns() < end_ms * 1_000_000:
                wait_until(next_send_ns / 1_000_000)
                if time.time_ns() >= end_ms * 1_000_000:
                    break
                index = len(records)
                request_id = uuid.uuid4().hex
                sent_ns = time.time_ns()
                status, headers, _ = http(PROXIES[index % len(PROXIES)] + paths[algorithm], headers={
                    "X-Client-Id": f"{run}-{behavior}", "X-Experiment-Request-Id": request_id})
                response_ns = time.time_ns()
                assert status in (200, 429) and headers.get("X-RateLimit-Mode") == "redis", headers
                retry_after = int(headers["Retry-After"]) if status == 429 else None
                if retry_after is not None:
                    assert retry_after >= 1
                records.append({"algorithm": algorithm, "scenario": behavior,
                                "phase": "retry-client", "request_id": request_id,
                                "sent_ns": sent_ns, "response_ns": response_ns,
                                "request_path": paths[algorithm], "scheduled_ms": next_send_ns // 1_000_000,
                                "kong_ms": int(headers["X-RateLimit-Time-Ms"]), "status": status,
                                "retry_after_seconds": retry_after, "gateway": headers["X-Kong-Instance"]})
                # Initial overload is identical: limit+1 immediate attempts. Thereafter 5/s,
                # except the cooperative client waits from receipt of each 429 response.
                delay_ns = 0 if index < args.limit else 200_000_000
                if behavior == "retry-follow" and status == 429:
                    delay_ns = retry_after * 1_000_000_000
                next_send_ns = response_ns + delay_ns
            if behavior == "retry-follow":
                for previous, current in zip(records, records[1:]):
                    if previous["status"] == 429:
                        assert current["sent_ns"] >= previous["response_ns"] + previous["retry_after_seconds"] * 1_000_000_000
            assert any(r["status"] == 429 for r in records)
            return records

        boundary_ms = (int(time.time() * 1000) // args.window_ms + 1) * args.window_ms
        if boundary_ms - time.time() * 1000 < 3000:
            boundary_ms += args.window_ms
        with ThreadPoolExecutor(max_workers=4) as clients, ThreadPoolExecutor(max_workers=24) as pool:
            retry_jobs = [clients.submit(retry_client, algorithm, behavior, boundary_ms + 4000,
                                         boundary_ms + args.duration_seconds * 1000)
                          for algorithm in ALGORITHMS for behavior in ("retry-follow", "retry-ignore")]
            before = burst("boundary", "before", args.limit, boundary_ms - 1000, pool)
            after = burst("boundary", "after", args.limit, boundary_ms + 1000, pool)
            recovery = []
            for second in range(2, args.duration_seconds):
                if second == 4:
                    # A different identity still demonstrates the absence of pacing.
                    fresh = burst("fresh", "idle-burst", 2 * args.limit, boundary_ms + 4000, pool)
                # Deliberately client-paced input, not traffic shaped by the limiter.
                recovery += burst("boundary", "recovery", 1, boundary_ms + second * 1000 + 250,
                                  pool, node_offset=second)

        status, _, arrivals = http("http://window-observer:8080/events")
        assert status == 200
        retry_attempts = [record for job in retry_jobs for record in job.result()]
        attempts = before + after + recovery + fresh + retry_attempts
        admitted_ids = {a["request_id"] for a in attempts if a["status"] == 200}
        assert len(arrivals) == len(admitted_ids)
        assert {a["request_id"] for a in arrivals} == admitted_ids, "Upstream arrivals differ from admissions"
        received = {a["request_id"]: a["received_ns"] for a in arrivals}
        for group, expected_start in ((before, boundary_ms - args.window_ms), (after, boundary_ms), (fresh, boundary_ms)):
            assert all(a["kong_ms"] // args.window_ms * args.window_ms == expected_start for a in group), \
                "Burst crossed an unintended window boundary; this run cannot illustrate the intended case"
        assert all(a["kong_ms"] // args.window_ms == a["scheduled_ms"] // args.window_ms for a in recovery), \
            "A recovery request missed its intended window"
        for algorithm in ALGORITHMS:
            for group in (before, after, fresh):
                assert {a["gateway"] for a in group if a["algorithm"] == algorithm} == {"kong1", "kong2", "kong3"}
            assert sum(a["status"] == 200 for a in before if a["algorithm"] == algorithm) == args.limit
            assert sum(a["status"] == 200 for a in fresh if a["algorithm"] == algorithm) == args.limit
            assert {a["gateway"] for a in recovery if a["algorithm"] == algorithm} == {"kong1", "kong2", "kong3"}
        fixed_after = sum(a["status"] == 200 for a in after if a["algorithm"] == "fixed_window")
        sliding_after = sum(a["status"] == 200 for a in after if a["algorithm"] == "sliding_window")
        assert fixed_after == args.limit and 0 <= sliding_after < fixed_after
        assert all(received[a["request_id"]] < boundary_ms * 1_000_000 for a in before)
        return {"limit": args.limit, "window_ms": args.window_ms, "boundary_ms": boundary_ms,
                "duration_seconds": args.duration_seconds, "recovery_rate_per_second": 1,
                "recovery_start_offset_ms": 2250,
                "retry_start_ms": boundary_ms + 4000, "retry_rate_per_second": 5,
                "fresh_burst_ms": boundary_ms + 4000, "attempts": attempts, "upstream_arrivals": arrivals,
                "note": "Actual upstream timestamps; denied attempts count. These limiters do not queue or pace requests."}
    finally:
        for kind in ("plugins", "routes", "services"):
            for entity_id in reversed(entities[kind]):
                admin(f"/{kind}/{entity_id}", "DELETE")


def run_host(args):
    output = args.output or Path(tempfile.mkdtemp(prefix="kong-window-illustration-"))
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    project = "kong-window-illustration-" + uuid.uuid4().hex[:10]
    env = dict(os.environ, COMPOSE_PROJECT_NAME=project, COMPOSE_PROFILES="",
               COMPOSE_FILE=f"{ROOT / 'compose.yaml'}:{ROOT / 'test/compose.illustration.yaml'}")
    compose = ["docker", "compose"]

    def command(command, name, capture=False):
        print(f"{name}: {project}", flush=True)
        with (output / (name + ".log")).open("w") as log:
            return subprocess.run(command, cwd=ROOT, env=env, check=True, text=True,
                                  stdout=subprocess.PIPE if capture else log, stderr=log).stdout

    config = json.loads(subprocess.check_output(compose + ["--profile", "*", "config", "--format", "json"],
                                              cwd=ROOT, env=env, text=True))
    assert config["name"] == project
    for kind in ("networks", "volumes"):
        assert all(not v.get("external") and v["name"] == project + "_" + k
                   for k, v in config[kind].items()), "Resources must be project-scoped"
    assert all(not s.get("ports") and not s.get("container_name") and not s.get("network_mode")
               for s in config["services"].values())
    (output / "compose.json").write_text(json.dumps(config, indent=2) + "\n")

    try:
        command(["./run.sh", "start"], "start")
        command(compose + ["up", "-d", "--wait", "window-observer"], "observer")
        ids = subprocess.check_output(compose + ["ps", "--all", "-q"], cwd=ROOT, env=env, text=True).split()
        actual = json.loads(subprocess.check_output(["docker", "inspect", *ids], text=True))
        for container in actual:
            assert container["Config"]["Labels"]["com.docker.compose.project"] == project
            assert not container["HostConfig"].get("PortBindings")
            assert all(n == project + "_default" for n in container["NetworkSettings"]["Networks"])
            for mount in container["Mounts"]:
                if mount["Type"] == "volume":
                    assert mount["Name"] in {project + "_postgres-data", project + "_redis-data"}
                elif mount["Type"] == "bind":
                    assert mount["Source"].startswith(str(ROOT) + "/") and not mount["RW"]
        data = json.loads(command(compose + ["run", "--rm", "--no-deps", "-T", "tests", "uv", "run",
                                           "--frozen", "--no-sync", "python", "illustrate_windows.py", "--worker",
                                           "--limit", str(args.limit), "--window-ms", str(args.window_ms),
                                           "--duration-seconds", str(args.duration_seconds)], "measure", True))
        data["project"] = project
        data["revision"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        data["source_dirty"] = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
        data["source_sha256"] = {name: hashlib.sha256((ROOT / "test" / name).read_bytes()).hexdigest()
                                 for name in ("illustrate_windows.py", "window_observer.py", "export_window_parquet.py",
                                              "plot_window_illustration.py", "compose.illustration.yaml")}
        info = json.loads(subprocess.check_output(["docker", "info", "--format", "{{json .}}"], text=True))
        data["docker_host"] = {"cpus": info["NCPU"], "memory_bytes": info["MemTotal"],
                               "architecture": info["Architecture"], "docker_version": info["ServerVersion"]}
        data["containers"] = [{"service": c["Config"]["Labels"]["com.docker.compose.service"], "image": c["Image"],
                               "nano_cpus": c["HostConfig"]["NanoCpus"], "memory_bytes": c["HostConfig"]["Memory"]} for c in actual]
        target = output / "arrivals.json"
        target.write_text(json.dumps(data, indent=2) + "\n")
        parquet = output / "requests.parquet"
        command([sys.executable, str(ROOT / "test/export_window_parquet.py"), str(target), "--output", str(parquet)], "parquet")
        command([sys.executable, str(ROOT / "test/plot_window_illustration.py"), str(parquet)], "plot")
        print(f"Results: {output}", flush=True)
    finally:
        # Only this fresh project's containers/network and exact named volumes.
        command(["./run.sh", "stop"], "stop")
        subprocess.run(["docker", "volume", "rm", project + "_postgres-data", project + "_redis-data"],
                       check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--window-ms", type=int, default=10000)
    parser.add_argument("--duration-seconds", type=int, default=60,
                        help="Recovery recording length after the boundary; default covers six 10s windows")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 3 <= args.limit <= 100 or not 10000 <= args.window_ms <= 60000:
        parser.error("limit must be 3..100 and window-ms 10000..60000")
    if not 6 <= args.duration_seconds <= 600:
        parser.error("duration-seconds must be 6..600")
    if args.worker:
        print(json.dumps(measure(args)))
    else:
        run_host(args)
