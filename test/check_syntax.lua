-- Compile plugin/test chunks using Kong's LuaJIT, without executing them.
local files = assert(io.popen("find /usr/local/share/lua/5.1/kong/plugins/rate-limiting-sliding-window /test -type f -name '*.lua'"))
local count = 0
for path in files:lines() do
  local chunk, err = loadfile(path)
  if not chunk then
    io.stderr:write(err, "\n")
    os.exit(1)
  end
  count = count + 1
end
assert(files:close(), "could not enumerate Lua files")
assert(count > 0, "no Lua files found")
print("PASS LuaJIT syntax: " .. count .. " Lua files")
