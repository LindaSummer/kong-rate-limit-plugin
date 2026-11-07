"""Create only temporary benchmark entities; emit their IDs for cleanup."""
import argparse
import json
import sys
import time
import uuid

from common import PROXIES, admin, http


def cleanup(entities):
    errors = []
    for kind in ("plugins", "routes", "services"):
        for entity_id in reversed(entities.get(kind, [])):
            try:
                admin(f"/{kind}/{entity_id}", "DELETE")
            except Exception as exc:
                errors.append(str(exc))
    if errors:
        raise AssertionError("Benchmark cleanup failed: " + "; ".join(errors))


def prepare(implementation, workload, sync_rate=0.1):
    entities = {"plugins": [], "routes": [], "services": []}
    name = "benchmark-" + uuid.uuid4().hex
    path = "/" + name
    threshold = 1 if workload == "throttled" else 1000000
    client = name + "-measured"
    if implementation == "direct":
        assert workload == "admitted", "Direct backend has no throttled workload"
        assert http("http://echo:8080" + path)[0] == 200
        return {"entities": entities, "path": path, "client": client,
                "plugin": None, "config": {}, "threshold": None}
    if implementation == "no_plugin":
        assert workload == "admitted", "Unrestricted Kong has no throttled workload"
        plugin_name, config = None, {}
    elif implementation.startswith("kong_"):
        plugin_name = "rate-limiting"
        config = {"day": threshold, "limit_by": "header",
                  "header_name": "X-Client-Id", "policy": "redis",
                  "sync_rate": -1 if implementation == "kong_sync" else sync_rate, "fault_tolerant": False,
                  "redis": {"host": "redis", "port": 6379, "timeout": 150}}
    else:
        plugin_name = "rate-limiting-sliding-window"
        config = {"client_id_header": "X-Client-Id",
                  "redis": {"host": "redis", "port": 6379, "timeout": 150},
                  "limits": [{"algorithm": implementation, "window_ms": 86400000,
                              "limit": threshold, "dimensions": ["client_id"]}]}
    try:
        service = admin("/services", "POST", {"name": name, "url": "http://echo:8080"})
        entities["services"].append(service["id"])
        route = admin("/routes", "POST", {"paths": [path], "strip_path": False,
                                          "service": {"id": service["id"]}})
        entities["routes"].append(route["id"])
        if plugin_name:
            plugin = admin(f'/services/{service["id"]}/plugins', "POST",
                           {"name": plugin_name, "config": config})
            entities["plugins"].append(plugin["id"])
        started = time.monotonic()
        stable = 0
        while time.monotonic() - started < 30:
            results = [http(url + path, headers={"X-Client-Id": uuid.uuid4().hex}) for url in PROXIES]
            def ready(result):
                status, headers, _ = result
                if implementation == "no_plugin":
                    return status == 200 and "X-RateLimit-Mode" not in headers and "X-RateLimit-Limit-Day" not in headers
                selected = (headers.get("X-RateLimit-Limit-Day") == str(threshold)
                            if implementation.startswith("kong_") else
                            headers.get("X-RateLimit-Mode") == "redis" and
                            headers.get("X-RateLimit-Remaining") == str(threshold - 1))
                return status == 200 and selected
            if all(ready(r) for r in results):
                stable += 1
                if stable >= 15 and time.monotonic() - started >= 4:
                    break
            else:
                stable = 0
            time.sleep(0.1)
        else:
            raise AssertionError(f"Benchmark policy did not propagate: {results}")
        if workload == "throttled":
            # A daily window keeps the rejection sample away from short resets.
            assert http(PROXIES[0] + path, headers={"X-Client-Id": client})[0] == 200
            # Async caches are per worker. Let every worker learn the exhausted
            # budget before timing the rejection path; transient priming admissions
            # are deliberately outside the measured sample.
            started = time.monotonic()
            stable = 0
            while time.monotonic() - started < 30:
                responses = [http(url + path, headers={"X-Client-Id": client}) for url in PROXIES]
                stable = stable + 1 if all(r[0] == 429 for r in responses) else 0
                if stable >= 15 and time.monotonic() - started >= 2:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError(f"Throttled policy did not settle: {responses}")
        return {"entities": entities, "path": path, "client": client,
                "plugin": plugin_name, "config": config, "threshold": threshold}
    except BaseException:
        cleanup(entities)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "cleanup"])
    parser.add_argument("--implementation", choices=["direct", "no_plugin", "kong_sync", "kong_async", "fixed_window", "sliding_window"])
    parser.add_argument("--workload", choices=["admitted", "throttled"])
    parser.add_argument("--sync-rate", type=float, default=0.1)
    args = parser.parse_args()
    if args.sync_rate < 0.02:
        parser.error("Async sync-rate must be at least 0.02 seconds")
    if args.action == "cleanup":
        cleanup(json.load(sys.stdin))
    else:
        if not args.implementation or not args.workload:
            parser.error("prepare requires --implementation and --workload")
        print(json.dumps(prepare(args.implementation, args.workload, args.sync_rate)))
