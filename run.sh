#!/bin/sh
set -eu
cd "$(dirname "$0")"

compose() { docker compose -f compose.yaml "$@"; }

start() {
  compose up -d --wait
}

case "${1:-start}" in
  start)
    start
    compose build tests
    compose run --rm --no-deps tests uv run --frozen --no-sync python window_configure.py
    ;;
  test)
    start
    compose build tests
    compose exec -T kong1 /usr/local/openresty/luajit/bin/luajit /test/check_syntax.lua
    compose exec -T kong1 /usr/local/openresty/bin/resty /test/window_unit.lua
    compose exec -T kong1 /usr/local/openresty/bin/resty --shdict 'sliding_ratelimit 1m' --shdict 'kong_locks 1m' /test/window_shared.lua
    compose exec -T kong1 /usr/local/openresty/bin/resty /test/window_redis.lua
    compose run --rm --no-deps tests
    compose run --rm --no-deps tests uv run --frozen --no-sync python window_restart.py prepare
    compose restart kong1
    compose up -d --wait
    compose run --rm --no-deps tests uv run --frozen --no-sync pytest -v -m restart test_restart.py
    compose run --rm --no-deps tests uv run --frozen --no-sync python window_configure.py
    ;;
  benchmark)
    start
    shift
    compose run --rm --no-deps benchmark run "$@" /test/benchmark.js
    ;;
  stop) compose down ;;
  *) echo "Usage: $0 [start|test|stop|benchmark [k6 run options]]" >&2; exit 2 ;;
esac
