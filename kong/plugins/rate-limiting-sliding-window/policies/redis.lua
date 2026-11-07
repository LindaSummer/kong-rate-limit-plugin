local redis = require "resty.redis"
local cjson = require "cjson.safe"
local identity = require "kong.plugins.rate-limiting-sliding-window.identity"
local script = require "kong.plugins.rate-limiting-sliding-window.redis_script"
local algorithms = require "kong.plugins.rate-limiting-sliding-window.algorithms"
local _M = {}
local sha = identity.hex(ngx.sha1_bin(script))

function _M.backend(conf)
  local fields = {}
  for _, name in ipairs({"host", "port", "database", "username", "password", "ssl", "ssl_verify", "server_name"}) do
    fields[#fields+1] = {name, tostring(conf[name])}
  end
  return identity.hex(identity.digest(assert(cjson.encode(fields))))
end

function _M.connect(conf)
  local red = redis:new()
  red:set_timeout(conf.timeout)
  local ok, err = red:connect(conf.host, conf.port, {
    pool = "rlsw:" .. _M.backend(conf), ssl = conf.ssl, ssl_verify = conf.ssl_verify,
    server_name = conf.server_name ~= ngx.null and conf.server_name or nil,
  })
  if not ok then red:close(); return nil, err end
  local reused
  reused, err = red:get_reused_times()
  if not reused then red:close(); return nil, err end
  if reused == 0 then
    if conf.password and conf.password ~= ngx.null then
      if conf.username and conf.username ~= ngx.null then
        ok, err = red:auth(conf.username, conf.password)
      else
        ok, err = red:auth(conf.password)
      end
      if not ok then red:close(); return nil, "authentication: " .. tostring(err) end
    end
    if conf.database ~= 0 then
      ok, err = red:select(conf.database)
      if not ok then red:close(); return nil, err end
    end
  end
  return red
end

local function server_error(reply)
  return type(reply) == "table" and reply[1] == false and reply[2] or nil
end

local function read_counts(reply)
  assert(type(reply) == "table" and #reply == 2, "invalid hash counter reply")
  local current, previous = algorithms.counter(reply[1]), algorithms.counter(reply[2])
  assert(current >= 1, "invalid current counter")
  return current, previous
end

local function enqueue(red, counter)
  red:evalsha(sha, 1, counter.key, counter.current_field, counter.previous_field, counter.ttl_ms)
end

function _M.evaluate(red, keys, rules, now)
  local ok, result, err = pcall(function()
    -- Kong supplies the request time and field names; Redis reads/increments
    -- each hash atomically, without a transaction across different limits.
    local counters = identity.counters(keys, rules, now)
    red:init_pipeline()
    for _, counter in ipairs(counters) do enqueue(red,counter) end
    local replies, pipeline_err = red:commit_pipeline()
    if not replies then return nil, pipeline_err end
    assert(#replies == #counters, "incomplete counter pipeline")
    local missing = {}
    for i, counter in ipairs(counters) do
      local reply_err = server_error(replies[i])
      if reply_err then
        if reply_err:find("NOSCRIPT", 1, true) then
          missing[#missing+1] = i
        else
          return nil, reply_err
        end
      else
        counter.current, counter.previous = read_counts(replies[i])
      end
    end
    if #missing > 0 then
      local loaded, load_err = red:script("load",script)
      if not loaded then return nil, load_err end
      assert(loaded == sha, "unexpected script digest")
      red:init_pipeline()
      for _, i in ipairs(missing) do enqueue(red,counters[i]) end
      replies, pipeline_err = red:commit_pipeline()
      if not replies then return nil, pipeline_err end
      assert(#replies == #missing, "incomplete script retry pipeline")
      -- Only NOSCRIPT entries are retried, never successful increments or an
      -- ambiguous pipeline after a timeout/lost response.
      for j, i in ipairs(missing) do
        local reply_err = server_error(replies[j])
        if reply_err then return nil, reply_err end
        counters[i].current, counters[i].previous = read_counts(replies[j])
      end
    end
    local retry, remaining = 0, 1000000
    for _, counter in ipairs(counters) do
      local value = algorithms.calculate(counter.current,counter.previous,counter.rule,now)
      retry = math.max(retry,value.retry_ms)
      remaining = math.min(remaining,value.remaining)
    end
    return {retry == 0 and "allow" or "deny",retry,now,retry == 0 and remaining or 0}
  end)
  if not ok then return nil, tostring(result) end
  return result, err
end

function _M.admit(conf, keys, rules, now)
  local red, err = _M.connect(conf)
  if not red then return nil, err end
  local result
  result, err = _M.evaluate(red, keys, rules, now)
  if result then red:set_keepalive(10000, 100) else red:close() end
  return result, err
end

return _M
