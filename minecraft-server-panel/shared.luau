--!nonstrict
-- Runs client.py, which does the ssh work and caches the status. Shared by
-- the bar widget and the panel, which run in separate VMs.

local M = {}

local instance = require("./instance.luau")

-- A setting, or its default from instance.luau when Noctalia has none for it
-- (a plugin's settings are only read when Noctalia starts).
local function setting(key)
  local v = noctalia.getConfig(key)
  if v == nil then
    v = instance[key]
  end
  return v ~= nil and tostring(v):match("^%s*(.-)%s*$") or ""
end

-- The plugin settings. host and folder end up in a shell line (ssh, the
-- console button), so anything but plain host/folder characters is dropped.
function M.config()
  local host = setting("host")
  local folder = setting("folder")
  local first, last = setting("ports"):match("^(%d+)%s*%-%s*(%d+)$")
  first, last = tonumber(first) or 25565, tonumber(last) or 25575
  return {
    host = host:match("^[%w%._@%-]+$") and host or "",
    folder = folder:match("^[%w%._%-]+$") and folder or "minecraft-server-panel",
    first = math.min(first, last),
    last = math.max(first, last),
    title = setting("title") ~= "" and setting("title") or noctalia.tr("ui.title"),
  }
end

local function argv(args)
  local c = M.config()
  local out = { "env", "SERVER_PANEL_HOST=" .. c.host, "SERVER_PANEL_DIR=" .. c.folder, "SERVER_PANEL_TITLE=" .. c.title,
                "python3", "-I", noctalia.pluginDir() .. "/client.py" }
  for _, a in ipairs(args) do
    out[#out + 1] = a
  end
  return out
end

-- Runs `client.py <args>` and hands the decoded JSON output (or nil) to cb.
function M.run(args, cb)
  local queued = noctalia.runAsync(argv(args), function(result)
    local data = nil
    if type(result) == "table" and not result.timedOut then
      data = noctalia.json.decode(result.stdout or "")
    end
    cb(type(data) == "table" and data or nil)
  end)
  if not queued then
    cb(nil)
  end
end

-- The status to show: the live one, or the last good one while the server
-- can't be reached (data.error says why).
function M.view(data)
  if data == nil then
    return nil
  end
  if data.servers ~= nil then
    return data
  end
  return data.last
end

function M.bytes(n)
  n = tonumber(n) or 0
  if n >= 1024 ^ 3 then
    return string.format("%.1f GB", n / 1024 ^ 3)
  end
  if n >= 1024 ^ 2 then
    return string.format("%d MB", math.floor(n / 1024 ^ 2))
  end
  return string.format("%d KB", math.max(1, math.floor(n / 1024)))
end

function M.duration(seconds)
  local s = math.floor(tonumber(seconds) or 0)
  local d, h, m = math.floor(s / 86400), math.floor(s % 86400 / 3600), math.floor(s % 3600 / 60)
  if d > 0 then
    return string.format("%dd %dh", d, h)
  elseif h > 0 then
    return string.format("%dh %02dm", h, m)
  end
  return string.format("%dm", m)
end

function M.ago(epoch)
  local s = os.time() - (tonumber(epoch) or 0)
  if s < 90 then
    return "just now"
  elseif s < 5400 then
    return tostring(math.floor(s / 60)) .. " min ago"
  elseif s < 172800 then
    return tostring(math.floor(s / 3600)) .. " h ago"
  end
  return tostring(math.floor(s / 86400)) .. " days ago"
end

-- An action still in progress (queued, stopping, starting, backing up, installing), or nil. Only
-- real actions count: a server that merely looks like it is booting can
-- still be stopped or restarted. The agent marks stale actions failed.
function M.busy(server)
  local a = server.action
  if a ~= nil and (a.phase == "queued" or a.phase == "stopping" or a.phase == "starting" or a.phase == "backing-up"
      or a.phase == "restoring" or a.phase == "installing") then
    return a
  end
  return nil
end

M.id = instance.id

return M
