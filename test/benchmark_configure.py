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


def prepare(implementation, workload):
    entities = {"plugins": [], "routes": [], "services": []}
    name = "benchmark-" + uuid.uuid4().hex
    path = "/" + name
    threshold = 1 if workload == "throttled" else 1000000
    client = name + "-measured"
    if implementation == "kong":
        plugin_name = "rate-limiting"
        config = {"day": threshold, "limit_by": "header",
                  "header_name": "X-Client-Id", "policy": "redis",
                  "sync_rate": -1, "fault_tolerant": False,
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
        plugin = admin(f'/services/{service["id"]}/plugins', "POST",
                       {"name": plugin_name, "config": config})
        entities["plugins"].append(plugin["id"])
        # Identical observation overhead for both plugins, including their 429s.
        observer = admin(f'/services/{service["id"]}/plugins', "POST", {
            "name": "post-function", "config": {"header_filter": [
                'kong.response.set_header("X-Benchmark-Instance", kong.node.get_hostname())']}})
        entities["plugins"].append(observer["id"])
        started = time.monotonic()
        stable = 0
        while time.monotonic() - started < 30:
            results = [http(url + path, headers={"X-Client-Id": uuid.uuid4().hex}) for url in PROXIES]
            def ready(result):
                status, headers, _ = result
                observed = headers.get("X-Benchmark-Instance") in ("kong1", "kong2", "kong3")
                selected = (headers.get("X-RateLimit-Limit-Day") == str(threshold)
                            if implementation == "kong" else
                            headers.get("X-RateLimit-Mode") == "redis" and
                            headers.get("X-RateLimit-Remaining") == str(threshold - 1))
                return status == 200 and observed and selected
            if all(ready(r) for r in results):
                stable += 1
                if stable >= 5 and time.monotonic() - started >= 1.5:
                    break
            else:
                stable = 0
            time.sleep(0.1)
        else:
            raise AssertionError(f"Benchmark policy did not propagate: {results}")
        if workload == "throttled":
            # A daily window keeps the rejection sample away from short resets.
            assert http(PROXIES[0] + path, headers={"X-Client-Id": client})[0] == 200
            assert all(http(url + path, headers={"X-Client-Id": client})[0] == 429 for url in PROXIES)
        return {"entities": entities, "path": path, "client": client,
                "plugin": plugin_name, "config": config, "threshold": threshold}
    except BaseException:
        cleanup(entities)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "cleanup"])
    parser.add_argument("--implementation", choices=["kong", "fixed_window", "sliding_window"])
    parser.add_argument("--workload", choices=["admitted", "throttled"])
    args = parser.parse_args()
    if args.action == "cleanup":
        cleanup(json.load(sys.stdin))
    else:
        if not args.implementation or not args.workload:
            parser.error("prepare requires --implementation and --workload")
        print(json.dumps(prepare(args.implementation, args.workload)))
