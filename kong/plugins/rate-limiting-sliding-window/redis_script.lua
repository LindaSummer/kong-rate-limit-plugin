-- One bounded operation per bucket hash, pipelined across limits by Kong.
-- Redis maintains current/previous fields consistently; no request-wide lock.
return [[
local ttl = tonumber(ARGV[3])
assert(ttl and ttl == math.floor(ttl) and ttl > 0, 'invalid counter TTL')
local fields = redis.call('HMGET', KEYS[1], ARGV[1], ARGV[2])
local current, previous = 0, 0
if fields[1] then current = tonumber(fields[1]) end
if ARGV[2] ~= '' and fields[2] then previous = tonumber(fields[2]) end
assert(current and current >= 0 and current < 9007199250000000
       and current == math.floor(current), 'invalid current counter')
assert(previous and previous >= 0 and previous <= 9007199250000000
       and previous == math.floor(previous), 'invalid previous counter')
local count = redis.call('HINCRBY', KEYS[1], ARGV[1], 1)
redis.call('HPEXPIRE', KEYS[1], ttl, 'NX', 'FIELDS', 1, ARGV[1])
return {count, previous}
]]
