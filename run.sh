#!/bin/sh
set -eu
cd "$(dirname "$0")"

compose() { docker compose "$@"; }

start() {
  compose up -d --wait
}

start_benchmark() {
  start >&2
  compose build benchmark-upstream tests >&2
  compose up -d --wait benchmark-upstream >&2
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
  benchmark-compare)
    start_benchmark
    shift
    python3 test/compare_benchmarks.py "$@"
    ;;
  benchmark)
    start_benchmark
    compose run --rm --no-deps tests uv run --frozen --no-sync python window_configure.py --benchmark >&2
    shift
    compose run --rm --no-deps benchmark run "$@" /test/benchmark.js
    ;;
  stop) compose down ;;
  *) echo "Usage: $0 [start|test|stop|benchmark [k6 run options]|benchmark-compare [options]]" >&2; exit 2 ;;
esac
