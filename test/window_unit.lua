local root = "kong.plugins.rate-limiting-sliding-window."
local algorithms = require(root .. "algorithms")
local calculate = algorithms.calculate
local identity = require(root .. "identity")
local cases = 0
local function check(value, message)
  assert(value, message)
  cases = cases+1
end
local function rule(algorithm, limit, window)
  return {algorithm=algorithm or "fixed_window", limit=limit or 3,
          window_ms=window or 1000, dimensions={"client_id"}}
end

local config, state = rule(), nil
for count=1,5 do
  check(calculate(count, 0, config, 100).allowed == (count <= 3), "fixed capacity including attempts")
end
check(calculate(4, 0, config, 999).retry_ms == 1, "before boundary")
check(algorithms.window(config, 1000) == 1000, "exact boundary")
check(not calculate(3, 0, rule(nil,2), 200).allowed, "threshold decrease retains usage")
check(calculate(3, 0, rule(nil,4), 200).allowed, "threshold increase retains usage")

config = rule("sliding_window", 10)
local weighted = calculate(1, 10, config, 1500)
check(weighted.allowed and weighted.remaining == 4, "half-weight previous window")
check(calculate(1, 10, config, 1000).retry_ms == 200, "retry includes denied attempt")
check(calculate(11, 10, config, 1500).retry_ms == 682, "retry across next boundary")
check(calculate(1, 0, config, 4000).allowed, "idle window starts empty")

-- Evaluate the NEXT attempt against counts which already include a denial.
local function next_attempt(current, previous, config, elapsed, delta)
  local future = elapsed+delta
  local windows = math.floor(future/config.window_ms)
  if windows == 0 then return calculate(current+1, previous, config, future) end
  return calculate(1, windows == 1 and current or 0, config, future)
end
for current=1,8 do
  for previous=0,8 do
    for threshold=1,6 do
      for elapsed=0,19 do
        local r = rule("sliding_window", threshold, 20)
        local result = calculate(current, previous, r, elapsed)
        if not result.allowed then
          check(result.retry_ms >= 1 and next_attempt(current, previous, r, elapsed, result.retry_ms).allowed,
                "retry eventually allows")
          check(not next_attempt(current, previous, r, elapsed, result.retry_ms-1).allowed, "minimal retry")
        end
      end
    end
  end
end
check(calculate(1000001, 0, rule(), 0).retry_ms == 1000, "denial counters may exceed configured limit bounds")
check(not pcall(calculate, "bad", 0, config, 1), "corrupt counter")
check(not pcall(calculate, 1, 0, rule("unknown"), 0), "invalid algorithm")

local conf = {service_id="550e8400-e29b-41d4-a716-446655440000"}
check(#identity.uuid(conf.service_id) == 16, "raw UUID length")
check(identity.hex(identity.uuid(conf.service_id)) == "550e8400e29b41d4a716446655440000", "UUID roundtrip")
check(identity.uuid(nil) == string.rep("\0", 16), "absent UUID")
local a = rule()
a.dimensions = {"client_id", "path"}
local key = assert(identity.key(conf, a, {client_id="alice\0bob", path="/orders"}))
check(#key == 104 and key:sub(1,8) == "rlsw:v2:", "versioned binary base key")
check(key:sub(25,40) == string.rep("\0",16), "service scope has no route")
check(#identity.window_key(key,1000) == 112, "internal local timestamp suffix")
check(identity.hex(identity.window_key("",1000)) == "00000000000003e8", "big-endian timestamp")
local b = rule(nil,999)
b.dimensions = {"path", "client_id"}
check(identity.signature(a) == identity.signature(b), "canonical dimensions; threshold excluded")
check(key ~= identity.key(conf, a, {client_id="alice",path="bob\0/orders"}), "tuple encoding isolation")
b.window_ms = 2000
check(identity.signature(a) ~= identity.signature(b), "window isolates state")
check(identity.window_key(key,0) ~= identity.window_key(key,1000), "window boundary selects different key")
check(identity.window_key(key,1000) == identity.window_key(key,1000), "same supplied timestamp gives same key")
local windows = identity.counters({key}, {rule("sliding_window")}, 1500)
check(windows[1].key == key and windows[1].current_field == "1000" and windows[1].previous_field == "0", "current and previous hash fields")
check(windows[1].ttl_ms == 1500, "field remains through the following window")
check(not pcall(identity.counters, {key,key}, {rule(),rule()}, 100), "duplicate keys rejected before commands")

-- Mock storage/locking, not calculations. Every attempt counts across limits.
local unlocked = 0
package.loaded["resty.lock"] = {new=function()
  return {lock=function() return 0 end, unlock=function() unlocked=unlocked+1; return true end}
end}
local local_policy = require(root .. "policies.local")
local breaker = require(root .. "circuit_breaker")
local memory = {}
local dict = {
  get=function(_, k) return memory[k] end,
  safe_set=function(_, k, v) memory[k]=v; return true end,
}
check(local_policy.admit("scope", {"a", "b"}, {rule(nil,1), rule()}, dict, function() return 100 end)[1] == "allow", "local first")
check(local_policy.admit("scope", {"a", "b"}, {rule(nil,1), rule()}, dict, function() return 100 end)[1] == "deny", "local deny")
check(memory[identity.window_key("b",0)] == 2, "denied attempt consumes other limit too")
memory[identity.window_key("b",0)] = "invalid count"
check(not local_policy.admit("scope", {"a", "b"}, {rule(),rule()}, dict, function() return 100 end), "corrupt local state")
check(memory[identity.window_key("a",0)] == 3, "partial accounting is conservative on later failure")
check(unlocked == 3, "lock released on calculation failure")
dict.safe_set = function() return nil, "no memory" end
check(not local_policy.admit("scope", {"fresh"}, {rule()}, dict, function() return 0 end), "full memory fails closed")
check(unlocked == 4, "lock released on storage failure")

-- Mixed NOSCRIPT replies retry only the failed entries, never successful INCRs.
local redis_policy = require(root .. "policies.redis")
local script = require(root .. "redis_script")
local digest = identity.hex(ngx.sha1_bin(script))
local batches, batch, loads = {}, {}, 0
local fake = {
  init_pipeline=function() batch={} end,
  evalsha=function(_, _, _, key) batch[#batch+1]=key end,
  script=function(_, operation)
    check(operation == "load", "load missing script")
    loads=loads+1; return digest
  end,
  commit_pipeline=function()
    batches[#batches+1]=batch
    if #batches == 1 then return {{1,0},{false,"NOSCRIPT missing"}} end
    return {{1,0}}
  end,
}
local result = assert(redis_policy.evaluate(fake, {"a","b"}, {rule(),rule()},100))
check(result[1] == "allow" and loads == 1 and #batches == 2, "mixed cache miss recovered")
check(#batches[2] == 1 and batches[2][1] == "b", "only missing increment is retried")
fake.commit_pipeline=function() return nil,"timeout" end
check(not redis_policy.evaluate(fake, {"a","b"}, {rule(),rule()},100), "ambiguous pipeline is not replayed")
check(#batches == 2, "no extra retry batch after timeout")

state = breaker.initial()
local selected, ticket = breaker.choose(state, 0, 100, 1000)
check(selected, "closed circuit selects Redis")
breaker.failed(state, ticket, 0)
check(not breaker.choose(state, 249, 1, 1000), "cooldown")
local probe
selected, probe = breaker.choose(state, 250, 1, 1000)
check(selected and probe.probe, "half-open probe")
check(not breaker.choose(state, 251, 1, 1000), "single probe lease")
breaker.succeeded(state, ticket, 300)
check(state.mode == "open", "late response ignored")
breaker.failed(state, probe, 300)
check(state.delay == 500, "exponential backoff")
selected, probe = breaker.choose(state, 800, 1, 1000)
breaker.succeeded(state, probe, 800)
for i, fraction in ipairs({10,25,50,100}) do
  check(breaker.choose(state, 800+(i-1)*1000, fraction, 1000), "ramp selected")
  if fraction < 100 then
    check(not breaker.choose(state, 800+(i-1)*1000, fraction+1, 1000), "ramp local traffic")
  end
end
check(breaker.choose(state, 4800, 100, 1000) and state.mode == "closed", "recovery complete")
state = breaker.initial()
local ignored
ignored, ticket = breaker.choose(state, 0, 1, 1000)
breaker.failed(state, ticket, 0)
ignored, probe = breaker.choose(state, 250, 1, 1000)
local _, replacement = breaker.choose(state, 1250, 1, 1000)
breaker.succeeded(state, probe, 1251)
check(state.mode == "open" and replacement.generation ~= probe.generation, "expired probe fenced")
for i=1,8 do
  local now = state.until_ms
  ignored, probe = breaker.choose(state, now, 1, 1000)
  breaker.failed(state, probe, now)
end
check(state.delay == 4000, "probe backoff is bounded")

-- Request orchestration: a lost Redis reply can follow a completed deduction.
local remote_deductions, local_calls, report_success = 0, 0, nil
local transport_error = "timeout"
local local_error
package.loaded[root .. "policies.redis"] = {
  backend=function() return "backend" end,
  admit=function(_, _, _, now)
    check(now == 123456, "Redis receives Kong timestamp")
    remote_deductions = remote_deductions+1
    return nil, transport_error
  end,
}
package.loaded[root .. "policies.local"] = {
  admit=function(_, _, _, _, _, now)
    check(now == 123456, "fallback receives same timestamp")
    local_calls=local_calls+1
    if local_error then return nil, local_error end
    return {"allow",0,100,1}
  end,
}
package.loaded[root .. "circuit_breaker"] = {
  select=function() return true, {} end,
  report=function(_, _, success) report_success=success; return true end,
}
local headers = {}
rawset(_G, "kong", {
  client={get_forwarded_ip=function() return "127.0.0.1" end},
  router={get_route=function() return {id="route"} end, get_service=function() return {id="service"} end},
  request={get_path=function() return "/a" end, get_method=function() return "GET" end},
  response={set_header=function(k,v) headers[k]=v end, exit=function(status) return status end},
  log={err=function() end,warn=function() end},
})
local handler = require(root .. "handler")
setfenv(handler.access, setmetatable({ngx={var={hostname="unit-test"},now=function() return 123.456 end}}, {__index=getfenv(handler.access)}))
local request_conf = {redis={timeout=150},limits={{algorithm="fixed_window",window_ms=1000,limit=2,dimensions={"ip"}}}}
handler:access(request_conf)
check(remote_deductions == 1 and local_calls == 1 and report_success == false,
      "ambiguous transport reply falls back without replay")
check(headers["X-RateLimit-Mode"] == "local", "fallback diagnostics")
transport_error = "invalid counter state"
handler:access(request_conf)
check(local_calls == 2 and headers["X-RateLimit-Mode"] == "local", "Redis state errors still enforce local budgets")
transport_error = "authentication: invalid password"
handler:access(request_conf)
check(local_calls == 3 and report_success == false, "authentication errors fall back and open circuit")
local_error = "no memory"
check(handler:access(request_conf) == 503, "503 only when local enforcement also fails")
print("PASS window unit assertions: " .. cases)
