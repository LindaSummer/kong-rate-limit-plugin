"""Service-scoped demo: /window/a and /window/b share a client budget."""
import argparse
import json
from common import bootstrap

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--benchmark", action="store_true", help="Install the separate Go-backed benchmark demo")
args = parser.parse_args()
prefix = "/benchmark" if args.benchmark else "/window"
plugin = bootstrap("http://benchmark-upstream:8080", "benchmark-demo", prefix,
                   redis_timeout_ms=1000) if args.benchmark else bootstrap()
print(json.dumps({"plugin_id": plugin["id"], "service_id": plugin["service"]["id"],
                  "paths": [prefix + "/a", prefix + "/b"]}, indent=2))
