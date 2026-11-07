"""Service-scoped demo: /window/a and /window/b share a client budget."""
import json
from common import bootstrap

plugin = bootstrap()
print(json.dumps({"plugin_id": plugin["id"], "service_id": plugin["service"]["id"],
                  "paths": ["/window/a", "/window/b"]}, indent=2))
