--!nonstrict
-- Server panel bar widget: server glyph plus players online. Primary while
-- someone is playing, error when the server can't be reached.

local shared = require("./shared.luau")

local POLL_MS = 30000

local data = nil

local function render()
  local view = shared.view(data)
  if view == nil then
    barWidget.setGlyph("server-off")
    barWidget.setGlyphColor(data ~= nil and "error" or "on_surface_variant")
    barWidget.setText("")
    barWidget.setTooltip(shared.config().host == "" and noctalia.tr("ui.no-host")
      or data ~= nil and noctalia.tr("ui.unreachable", { error = tostring(data.error) }) or noctalia.tr("ui.no-data"))
    return
  end

  local players, running = 0, 0
  local lines = {}
  for _, s in ipairs(view.servers or {}) do
    if s.state ~= "stopped" then
      running = running + 1
      players = players + (tonumber(s.online) or 0)
      local line = noctalia.tr("ui.state-" .. tostring(s.state)) .. ": " .. tostring(s.name)
      if s.state == "running" then
        line = line .. " · " .. tostring(s.online or 0) .. "/" .. tostring(s.max or 0)
        if s.names ~= nil and #s.names > 0 then
          line = line .. " (" .. table.concat(s.names, ", ") .. ")"
        end
      end
      lines[#lines + 1] = line
    end
  end
  if running == 0 then
    lines[#lines + 1] = noctalia.tr("ui.none-running")
  end
  local h = view.host or {}
  lines[#lines + 1] = noctalia.tr("ui.host-line", {
    cpu = string.format("%.0f", tonumber(h.cpu) or 0),
    ram = shared.bytes(h.mem_used) .. " / " .. shared.bytes(h.mem_total),
  })
  if data.error ~= nil then
    lines[#lines + 1] = noctalia.tr("ui.unreachable", { error = tostring(data.error) })
  end

  barWidget.setGlyph(data.error ~= nil and "server-off" or "server")
  barWidget.setGlyphColor(data.error ~= nil and "error" or (players > 0 and "primary")
    or (running > 0 and "on_surface" or "on_surface_variant"))
  barWidget.setText(players > 0 and tostring(players) or "")
  barWidget.setTooltip(table.concat(lines, "\n"))
end

function update()
  noctalia.setUpdateInterval(POLL_MS)
  shared.run({ "status" }, function(d)
    data = d or data
    render()
  end)
end

function onClick()
  noctalia.togglePanel(shared.id .. ":panel")
end
