-- Run in a separate resty process with private shared dictionaries, never fill
-- or flush the live gateway's shared memory for a test.
local root = "kong.plugins.rate-limiting-sliding-window."
local breaker = require(root .. "circuit_breaker")
local local_policy = require(root .. "policies.local")
local dict = ngx.shared.sliding_ratelimit
local identity = require(root .. "identity")
local now = math.floor(ngx.now()*1000)
local rule = {algorithm="fixed_window", window_ms=86400000, limit=7}
local keys = {"rlsw:shared-test\0a", "rlsw:shared-test\0b"}
local counters = identity.counters(keys, {rule,rule}, now)
local threads, allowed = {}, 0
for i=1,40 do
  threads[i] = ngx.thread.spawn(function()
    ngx.sleep(0)
    return assert(local_policy.admit("scope", keys, {rule,rule}, nil, nil, now))[1]
  end)
end
for _, thread in ipairs(threads) do
  local ok, result = ngx.thread.wait(thread)
  assert(ok, result)
  if result == "allow" then allowed=allowed+1 end
end
assert(allowed == 7, "concurrent local budget")
local saved = dict:get(counters[1].local_key)
assert(saved == 40, "all attempts count, including denials")
print("PASS real shared-memory locks and concurrent local admission")

local key = "single-probe"
assert(breaker.update(key, function(state, now)
  state.mode, state.until_ms = "open", now-1
  return true
end))
threads = {}
for i=1,40 do
  threads[i] = ngx.thread.spawn(function()
    ngx.sleep(0)
    local selected, ticket = breaker.select(key, 150)
    assert(selected ~= nil, ticket)
    return selected and ticket.probe
  end)
end
local probes = 0
for _, thread in ipairs(threads) do
  local ok, probe = ngx.thread.wait(thread)
  assert(ok, probe)
  if probe then probes=probes+1 end
end
assert(probes == 1, "only one shared recovery probe")
print("PASS concurrent half-open selections share a single probe lease")

-- Fill only this process's private dictionary using safe_set. A live budget
-- must survive; a new admission must fail instead of evicting it.
local i = 0
while true do
  i = i+1
  local ok, err = dict:safe_set("fill:" .. i, string.rep("x", 1024))
  if not ok then assert(err == "no memory", err); break end
end
while dict:safe_set("small:" .. i, string.rep("x", 64)) do i=i+1 end
while dict:safe_set("tiny:" .. i, 0) do i=i+1 end
local result, err = local_policy.admit("fresh-scope", {"new-budget"}, {rule})
assert(not result and err, "full shared memory must fail closed")
assert(dict:get(counters[1].local_key) == saved, "live budget was not evicted")
print("PASS actual shared-memory exhaustion fails closed without eviction")
