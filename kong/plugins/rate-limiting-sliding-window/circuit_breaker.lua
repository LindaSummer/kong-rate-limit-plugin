local cjson = require "cjson.safe"
local resty_lock = require "resty.lock"
local _M = {}

-- One breaker per configured scope/Redis backend, shared by workers on this node.
-- closed: all requests use Redis; open: requests use the local limiter.
-- Half-open is represented by a leased probe ticket while mode stays "open".
-- A successful probe starts "ramp": 10%, 25%, 50%, then 100% Redis traffic,
-- each for one second, before returning to "closed". Other traffic stays local.
function _M.initial()
  return {mode = "closed", generation = 0, delay = 250}
end

-- Pure transitions accept a clock and random draw for deterministic tests.
-- Return true plus a ticket to attempt Redis, or false to use local enforcement.
function _M.choose(state, now, draw, lease_ms)
  if state.mode == "open" then
    -- until_ms is first the cooldown deadline, then the in-flight probe deadline.
    if now < state.until_ms then return false end
    -- Reserve one probe under the shared lock. If it never reports, lease expiry
    -- allows a replacement; the new generation invalidates the old probe ticket.
    state.generation = state.generation+1
    state.until_ms = now+lease_ms
    return true, {generation = state.generation, probe = true}
  end
  if state.mode == "ramp" then
    -- draw is uniform in 1..100, so these thresholds sample the Redis share.
    local stage = math.floor(math.max(0, now-state.started)/1000)+1
    if stage > 4 then
      state.mode, state.delay = "closed", 250
    elseif draw > ({10, 25, 50, 100})[stage] then
      return false
    end
  end
  return true, {generation = state.generation}
end

function _M.failed(state, ticket, now)
  -- Ignore results from requests selected before a newer failure/probe changed
  -- the generation; late completions must not overwrite the current recovery.
  if ticket.generation ~= state.generation then return end
  -- Only failed recovery probes double cooldown, capped at four seconds.
  if ticket.probe then state.delay = math.min(4000, state.delay*2) end
  -- Any current-generation Redis failure reopens the circuit and fences peers.
  state.generation = state.generation+1
  state.mode, state.until_ms = "open", now+state.delay
end

function _M.succeeded(state, ticket, now)
  if ticket.generation ~= state.generation then return end
  -- Ordinary successes leave state unchanged. Only the active recovery probe
  -- can move an open circuit into gradual recovery.
  if ticket.probe and state.mode == "open" then
    state.mode, state.started = "ramp", now
  end
end

function _M.update(key, fn)
  local dict = ngx.shared.sliding_ratelimit
  if not dict then return nil, "sliding_ratelimit dictionary missing" end
  local lock, err = resty_lock:new("kong_locks", {timeout = 0.2, exptime = 5})
  if not lock then return nil, err end
  local elapsed
  -- Serialize read/modify/write of the JSON state across workers. The caller's
  -- Redis operation runs after select() releases this lock, never inside it.
  elapsed, err = lock:lock("rlsw:breaker-lock:" .. key)
  if not elapsed then return nil, err end
  local ok, first, second = pcall(function()
    local raw = dict:get("breaker:" .. key)
    local state = raw and assert(cjson.decode(raw), "invalid breaker JSON") or _M.initial()
    local a, b = fn(state, math.floor(ngx.now()*1000))
    -- Expire idle breaker state after an hour. safe_set avoids evicting limiter
    -- counters from the shared dictionary if memory is exhausted.
    assert(dict:safe_set("breaker:" .. key, assert(cjson.encode(state)), 3600))
    return a, b
  end)
  -- Release the lock even if decoding, transition, or persistence raised an error.
  lock:unlock()
  if not ok then return nil, tostring(first) end
  return first, second
end

function _M.select(key, timeout)
  -- Selection errors return nil/error; the handler also uses local fallback then.
  return _M.update(key, function(state, now)
    -- Cover a cold connection, TLS/auth/SELECT and a NOSCRIPT round trip.
    return _M.choose(state, now, math.random(100), math.max(1000, timeout*8+500))
  end)
end

function _M.report(key, ticket, success)
  -- Report whether Redis accounting succeeded, not whether the request was
  -- admitted: a valid rate-limit denial still means Redis is healthy.
  return _M.update(key, function(state, now)
    local before = state.mode
    if success then _M.succeeded(state, ticket, now) else _M.failed(state, ticket, now) end
    if before ~= state.mode then
      kong.log.warn("window rate limiter circuit ", before, " -> ", state.mode)
    end
    return true
  end)
end

return _M
