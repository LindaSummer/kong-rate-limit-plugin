local root = "kong.plugins.rate-limiting-sliding-window."
local policy = require(root .. "policies.redis")
local identity = require(root .. "identity")
local conf = {host="redis",port=6379,database=0,timeout=1000,ssl=false,ssl_verify=false}
local red = assert(policy.connect(conf))
local prefix = "rlsw:test:" .. ngx.now() .. ":" .. ngx.worker.pid()
local keys = {prefix .. "\0a", prefix .. "\0b"}
local rules = {{algorithm="fixed_window",window_ms=86400000,limit=1},
               {algorithm="sliding_window",window_ms=86400000,limit=3}}
-- Deliberately different from Redis's wall clock: this timestamp comes from Kong.
local now = 1700000000123
local counters = identity.counters(keys, rules, now)
local function attempt(k, r, at)
  local result, err = policy.evaluate(red, k or keys, r or rules, at or now)
  return assert(result, err)
end
local result = attempt()
assert(result[1] == "allow" and result[3] == now)
assert(attempt()[1] == "deny")
assert(red:hget(counters[1].key,counters[1].current_field) == "2" and red:hget(counters[2].key,counters[2].current_field) == "2")
assert(attempt()[1] == "deny" and red:hget(counters[2].key,counters[2].current_field) == "3")
print("PASS pipelined binary window keys use Kong time and count denied attempts")

for _, c in ipairs(counters) do red:del(c.key) end
red:hset(counters[2].key,counters[2].current_field,"invalid count")
result = policy.evaluate(red, keys, rules, now)
assert(not result and red:hget(counters[1].key,counters[1].current_field) == "1")
assert(red:hget(counters[2].key,counters[2].current_field) == "invalid count")
red:del(counters[2].key)
print("PASS later counter failure returns error without rollback of earlier increments")

red:script("flush")
assert(attempt()[1] == "deny")
assert(red:hget(counters[1].key,counters[1].current_field) == "2" and red:hget(counters[2].key,counters[2].current_field) == "1")
assert(tonumber(red:hpttl(counters[1].key,"FIELDS",1,counters[1].current_field)[1]) > 0)
print("PASS pipeline NOSCRIPT recovery increments each counter exactly once with TTL")
for _, c in ipairs(counters) do red:del(c.key) end

local sliding = {{algorithm="sliding_window",window_ms=1000,limit=10}}
local slots = identity.counters({keys[1]},sliding,1500)
red:hset(slots[1].key,slots[1].previous_field,"invalid count")
assert(not policy.evaluate(red,{keys[1]},sliding,1500))
assert(red:hexists(slots[1].key,slots[1].current_field) == 0, "invalid previous field cannot increment current")
red:del(slots[1].key)
red:hset(slots[1].key,slots[1].current_field,-1)
assert(not policy.evaluate(red,{keys[1]},sliding,1500))
assert(red:hget(slots[1].key,slots[1].current_field) == "-1", "invalid current field must not reset")
red:del(slots[1].key)
red:hset(slots[1].key,slots[1].previous_field,10)
red:hpexpire(slots[1].key,3000,"FIELDS",1,slots[1].previous_field)
for i=1,6 do
  result = attempt({keys[1]},sliding,1500)
  assert(result[1] == (i <= 5 and "allow" or "deny"))
end
assert(result[2] == 200 and red:hget(slots[1].key,slots[1].current_field) == "6")
-- Next request at the supplied retry time includes one more increment.
assert(attempt({keys[1]},sliding,1700)[1] == "allow")
red:del(slots[1].key)
print("PASS previous-window weighting and Retry-After include rejected attempts")

for _, algorithm in ipairs({"fixed_window","sliding_window"}) do
  local r = {{algorithm=algorithm,window_ms=60000,limit=7}}
  local at = math.floor(ngx.now()*1000)
  local descriptors = identity.counters({keys[1]},r,at)
  local threads, allowed = {}, 0
  for i=1,30 do
    threads[i] = ngx.thread.spawn(function()
      local connection = assert(policy.connect(conf))
      local response, err = policy.evaluate(connection,{keys[1]},r,at)
      connection:close()
      return assert(response,err)[1]
    end)
  end
  for _, thread in ipairs(threads) do
    local ok, response = ngx.thread.wait(thread)
    assert(ok,response)
    if response == "allow" then allowed=allowed+1 end
  end
  assert(allowed == 7 and red:hget(descriptors[1].key,descriptors[1].current_field) == "30")
  red:del(descriptors[1].key)
end
print("PASS concurrent per-counter increments preserve budgets and account all attempts")

-- Hash fields expire independently; the whole hash disappears when empty.
local short = {{algorithm="sliding_window",window_ms=80,limit=10}}
attempt({keys[1]},short,0)
ngx.sleep(0.06)
attempt({keys[1]},short,60)
ngx.sleep(0.03)
assert(attempt({keys[1]},short,90)[1] == "allow")
assert(red:hlen(keys[1]) == 2, "current and previous fields share one hash")
assert(tonumber(red:hpttl(keys[1],"FIELDS",1,"80")[1]) > 0)
ngx.sleep(0.08)
assert(red:hexists(keys[1],"0") == 0 and red:hexists(keys[1],"80") == 1)
ngx.sleep(0.10)
-- Access the expired field so the check does not depend on Redis's active
-- expiry scheduler having swept this hash already.
assert(red:hexists(keys[1],"80") == 0)
assert(red:exists(keys[1]) == 0)
print("PASS HPEXPIRE expires previous fields independently and deletes empty hashes")
red:close()
