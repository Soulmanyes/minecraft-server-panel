--!nonstrict
-- Server panel: host CPU/RAM/disk, then one card per game server with its
-- players and Start / Stop / Restart. Stop and Restart ask for a second click.

local shared = require("./shared.luau")

local FAST_S = 4 -- refresh while an action runs
local SLOW_S = 15
local CONFIRM_MS = 5000

local data = nil
local open = false
local loading = false
local confirm = nil -- { id, kind, at } waiting for the second click
local sending = {} -- server id -> true while an action call is on its way
local showBackups = {} -- server id -> true while its backup list is open
local showMods = {} -- server id -> true while its mod list is open
local modLists = {} -- server id -> list of mods, or false while loading
local modFilter = {} -- server id -> filter text
local MAX_MODS = 60
local COLUMNS = 2
-- Panel 960 wide: minus 2 x 16 padding, the gap between columns and the scrollbar.
local CARD_WIDTH = 436
local ramDraft = {} -- server id -> { min, max } in GB while its RAM editor is open
local GB = 1024 ^ 3
-- Adding a server and the cards of new servers span both columns.
local FULL_WIDTH = CARD_WIDTH * COLUMNS + 8
local adding = false -- the "Add a server" card is open
local cands = nil -- recent server zips in ~/Downloads, false while loading
local linkText = ""
local portDraft = {} -- new server id -> typed port
local convertView = {} -- new server id -> suggested run.sh, or false while loading
local DONE_SHOWN_S = 600
local FAILED_SHOWN_S = 3600

local STATE_COLOR = { running = "primary", starting = "tertiary", stopping = "tertiary", unresponsive = "error",
                      stopped = "on_surface_variant" }
local ORDER = { running = 1, unresponsive = 1, starting = 2, stopping = 2, stopped = 3 }

local function refresh(fresh)
  if loading then
    return
  end
  loading = true
  shared.run(fresh and { "status", "--fresh" } or { "status" }, function(d)
    loading = false
    data = d or data
    render()
  end)
end

local function running(job)
  return job ~= nil and job.phase ~= "done" and job.phase ~= "failed"
end

-- An upload from this PC or a download on the server that hasn't finished.
local function installing(view)
  return running(data and data.install) or running(view and view.fetch)
end

local function anyBusy(view)
  for _, list in ipairs({ view and view.servers or {}, view and view.setup or {} }) do
    for _, s in ipairs(list) do
      if shared.busy(s) or sending[s.id] then
        return true
      end
    end
  end
  return installing(view)
end

local function act(server, kind, backup)
  confirm = nil
  sending[server.id] = true
  render()
  shared.run({ "action", kind, server.id, backup }, function(result)
    sending[server.id] = nil
    if result == nil or not result.ok then
      noctalia.notifyError(shared.config().title, noctalia.tr("ui.action-failed", {
        action = noctalia.tr("ui.btn-" .. kind), server = tostring(server.name),
        error = tostring(result and result.error or "no answer"),
      }))
    end
    refresh(true)
  end)
end

local function noticesOn(id)
  for _, n in ipairs(data and data.notices or {}) do
    if n == id then
      return true
    end
  end
  return false
end

local function toggleNotices(server)
  local on = not noticesOn(server.id)
  shared.run({ "notices", server.id, on and "on" or "off" }, function(result)
    if result ~= nil and result.ok and data ~= nil then
      data.notices = result.notices
    end
    render()
  end)
end

local function toggleAuto(server)
  sending[server.id] = true
  render()
  shared.run({ "auto", server.id, server.auto_restart and "off" or "on" }, function(result)
    sending[server.id] = nil
    if result == nil or not result.ok then
      noctalia.notifyError(shared.config().title, tostring(result and result.error or "no answer"))
    end
    refresh(true)
  end)
end

local renaming = {} -- server id -> typed name while its rename box is open

local function saveName(server)
  local name = tostring(renaming[server.id] or "")
  sending[server.id] = true
  render()
  shared.run({ "rename", server.id, name }, function(result)
    sending[server.id] = nil
    if result ~= nil and result.ok then
      renaming[server.id] = nil
    else
      noctalia.notifyError(shared.config().title, noctalia.tr("ui.rename-failed", { server = tostring(server.name),
        error = tostring(result and result.error or "no answer") }))
    end
    refresh(true)
  end)
end

-- A card's title: the name with a rename pencil and the state, or the rename box.
local function titleRows(s, stateText, stateColor)
  local id = s.id
  if renaming[id] ~= nil then
    return { ui.row({ align = "center", gap = 6 }, {
      ui.input({ key = "rename-" .. id, value = tostring(renaming[id]), controlSize = "sm", flexGrow = 1,
        placeholder = noctalia.tr("ui.rename-placeholder"),
        onChange = function(text) renaming[id] = text; render() end }),
      ui.button({ text = noctalia.tr("ui.rename-save"), variant = "primary", controlSize = "sm",
        enabled = not sending[id], onClick = function() saveName(s) end }),
      ui.button({ text = noctalia.tr("ui.cancel"), variant = "ghost", controlSize = "sm",
        onClick = function() renaming[id] = nil; render() end }),
    }) }
  end
  local rows = { ui.row({ align = "center", gap = 4 }, {
    ui.label({ text = tostring(s.name), fontSize = 14, fontWeight = "bold", maxLines = 1 }),
    ui.button({ glyph = "pencil", variant = "ghost", controlSize = "sm", tooltip = noctalia.tr("ui.rename"),
      onClick = function() renaming[id] = tostring(s.name); render() end }),
    ui.spacer({ flexGrow = 1 }),
    ui.label({ text = stateText, fontSize = 11, fontWeight = "bold", color = stateColor }),
  }) }
  -- Still running in the session of its old name: it moves on the next restart.
  local session = tostring(s.session or "")
  local tmux = tostring(s.tmux or "")
  if s.state ~= "stopped" and session ~= "" and tmux ~= "" and tmux ~= session and tmux:sub(1, #session + 1) ~= session .. "-" then
    rows[#rows + 1] = ui.label({ text = noctalia.tr("ui.rename-session", { session = session }), fontSize = 11,
                                 color = "tertiary", maxLines = 2 })
  end
  return rows
end

local function meter(label, value, fraction)
  return ui.column({ gap = 3, flexGrow = 1 }, {
    ui.row({ align = "center" }, {
      ui.label({ text = label, fontSize = 11, color = "on_surface_variant", flexGrow = 1 }),
      ui.label({ text = value, fontSize = 11, fontWeight = "bold" }),
    }),
    ui.progress({ progress = math.max(0, math.min(1, fraction)), track = "on_surface/0.22", height = 5, radius = 2 }),
  })
end

local function hostCard(h)
  local cores = tonumber(h.cores) or 1
  local load = h.load or {}
  return ui.column({ gap = 8, padding = 12, radius = 10, fill = "surface_variant/0.85", border = "outline/0.50", borderWidth = 1 }, {
    ui.row({ gap = 12 }, {
      meter(noctalia.tr("ui.cpu"), string.format("%.0f%%", tonumber(h.cpu) or 0), (tonumber(h.cpu) or 0) / 100),
      meter(noctalia.tr("ui.ram"), shared.bytes(h.mem_used) .. " / " .. shared.bytes(h.mem_total),
        (tonumber(h.mem_used) or 0) / math.max(1, tonumber(h.mem_total) or 1)),
      meter(noctalia.tr("ui.disk"), shared.bytes(h.disk_used) .. " / " .. shared.bytes(h.disk_total),
        (tonumber(h.disk_used) or 0) / math.max(1, tonumber(h.disk_total) or 1)),
    }),
    ui.label({ text = noctalia.tr("ui.host-sub", {
      load = string.format("%.2f", tonumber(load[1]) or 0), cores = cores, uptime = shared.duration(h.uptime),
    }), fontSize = 11, color = "on_surface_variant" }),
  })
end

local function actionButtons(s, busy)
  local id = s.id
  if confirm ~= nil and confirm.id == id and confirm.backup == nil and noctalia.nowMs() - confirm.at < CONFIRM_MS then
    local kind = confirm.kind
    return {
      ui.button({ text = noctalia.tr("ui.confirm-" .. kind), variant = "destructive", controlSize = "sm",
                  onClick = function() act(s, kind) end }),
      ui.button({ text = noctalia.tr("ui.cancel"), variant = "ghost", controlSize = "sm",
                  onClick = function() confirm = nil; render() end }),
    }
  end
  local function ask(kind)
    return function()
      confirm = { id = id, kind = kind, at = noctalia.nowMs() }
      render()
    end
  end
  local on = noticesOn(id)
  local out = {
    ui.button({ glyph = on and "bell-ringing" or "bell-off", variant = "ghost", controlSize = "sm",
      tooltip = noctalia.tr(on and "ui.notices-on" or "ui.notices-off"), onClick = function() toggleNotices(s) end }),
  }
  out[#out + 1] = ui.button({ glyph = s.auto_restart and "refresh-alert" or "refresh-off", variant = "ghost",
    controlSize = "sm", enabled = not sending[id],
    tooltip = noctalia.tr(s.auto_restart and "ui.autorestart-on" or "ui.autorestart-off"), onClick = function() toggleAuto(s) end })
  if s.tmux ~= nil and s.tmux ~= "" then
    out[#out + 1] = ui.button({ glyph = "terminal-2", variant = "ghost", controlSize = "sm",
      tooltip = noctalia.tr("ui.console", { session = s.tmux }),
      onClick = function()
        noctalia.runInTerminal("ssh -t " .. shared.config().host .. " tmux attach -t '" .. tostring(s.tmux):gsub("'", "") .. "'")
      end })
  end
  local address = tostring((shared.view(data) or {}).address or "")
  if address ~= "" then
    local full = address .. ":" .. tostring(s.port)
    out[#out + 1] = ui.button({ glyph = "copy", variant = "ghost", controlSize = "sm",
      tooltip = noctalia.tr("ui.copy-address", { address = full }),
      onClick = function() noctalia.copyToClipboard(full, "text/plain") end })
  end
  local working = busy or sending[id]
  if s.state == "stopped" then
    out[#out + 1] = ui.button({ text = noctalia.tr("ui.btn-start"), glyph = "player-play", variant = "primary",
      controlSize = "sm", enabled = not working, onClick = function() act(s, "start") end })
  else
    out[#out + 1] = ui.button({ text = noctalia.tr("ui.btn-restart"), glyph = "rotate-clockwise", variant = "outline",
      controlSize = "sm", enabled = not working, onClick = ask("restart") })
    out[#out + 1] = ui.button({ text = noctalia.tr("ui.btn-stop"), glyph = "player-stop", variant = "outline",
      controlSize = "sm", enabled = not working, onClick = ask("stop") })
  end
  return out
end

local function backupRows(s, busy)
  local own = s.backups or {}
  local text
  if #own > 0 then
    local total = 0
    for _, b in ipairs(own) do
      total = total + (tonumber(b.size) or 0)
    end
    text = noctalia.tr("ui.backups-line", { count = #own, ago = shared.ago(own[1].time), size = shared.bytes(total) })
  else
    text = noctalia.tr("ui.backups-none")
  end
  local working = busy or sending[s.id]
  local rows = {
    ui.row({ align = "center", gap = 6 }, {
      ui.glyph({ name = "archive", size = 14, color = "on_surface_variant" }),
      ui.label({ text = text, fontSize = 11, color = "on_surface_variant", flexGrow = 1, maxLines = 2 }),
      #own > 0 and ui.button({ glyph = showBackups[s.id] and "chevron-up" or "chevron-down", variant = "ghost",
        controlSize = "sm", tooltip = noctalia.tr("ui.backups-show"),
        onClick = function() showBackups[s.id] = not showBackups[s.id]; render() end }) or ui.spacer({ width = 1 }),
      ui.button({ text = noctalia.tr("ui.btn-backup"), glyph = "archive", variant = "outline", controlSize = "sm",
        enabled = not working,
        onClick = function() act(s, "backup") end }),
    }),
  }
  if showBackups[s.id] then
    for _, b in ipairs(own) do
      local name = b.name
      local cells = {
        ui.label({ text = os.date("%Y-%m-%d %H:%M", tonumber(b.time) or 0), fontSize = 11, flexGrow = 1 }),
        ui.label({ text = shared.bytes(b.size), fontSize = 11, color = "on_surface_variant" }),
      }
      if confirm ~= nil and confirm.id == s.id and confirm.backup == name and noctalia.nowMs() - confirm.at < CONFIRM_MS then
        cells[#cells + 1] = ui.button({ text = noctalia.tr(s.state == "stopped" and "ui.confirm-restore-stopped"
          or "ui.confirm-restore"), variant = "destructive", controlSize = "sm",
          onClick = function() act(s, "restore", name) end })
        cells[#cells + 1] = ui.button({ text = noctalia.tr("ui.cancel"), variant = "ghost", controlSize = "sm",
          onClick = function() confirm = nil; render() end })
      else
        cells[#cells + 1] = ui.button({ text = noctalia.tr("ui.btn-restore"), glyph = "history", variant = "ghost",
          controlSize = "sm", enabled = not working, tooltip = noctalia.tr("ui.restore-tip"),
          onClick = function()
            confirm = { id = s.id, kind = "restore", backup = name, at = noctalia.nowMs() }
            render()
          end })
      end
      rows[#rows + 1] = ui.row({ align = "center", gap = 8, paddingH = 20 }, cells)
    end
  end
  local mod = s.mod_backups
  if mod ~= nil then
    rows[#rows + 1] = ui.label({ text = noctalia.tr("ui.mod-backups", {
      count = mod.count, ago = shared.ago(mod.newest), size = shared.bytes(mod.size),
    }), fontSize = 11, color = "on_surface_variant", maxLines = 2 })
  end
  return rows
end

local function loadMods(id)
  modLists[id] = false
  shared.run({ "mods", id }, function(result)
    modLists[id] = result ~= nil and result.ok and result.mods or {}
    render()
  end)
end

local function packRows(s)
  local p = s.pack
  if p == nil or (tonumber(p.mods) or 0) == 0 then
    return {}
  end
  local bits = {}
  if p.version ~= nil and p.version ~= "" then
    bits[#bits + 1] = noctalia.tr("ui.pack-version", { version = p.version })
  end
  if p.loader ~= nil and p.loader ~= "" then
    bits[#bits + 1] = p.loader
  end
  bits[#bits + 1] = noctalia.tr("ui.mod-count", { mods = tostring(p.mods) })
  local text = table.concat(bits, " · ")
  local id = s.id
  local rows = {
    ui.row({ align = "center", gap = 6 }, {
      ui.glyph({ name = "package", size = 14, color = "on_surface_variant" }),
      ui.label({ text = text, fontSize = 11, color = "on_surface_variant", flexGrow = 1, maxLines = 2 }),
      ui.button({ glyph = showMods[id] and "chevron-up" or "chevron-down", variant = "ghost", controlSize = "sm",
        tooltip = noctalia.tr("ui.mods-show"), onClick = function()
          showMods[id] = not showMods[id]
          render()
        end }),
    }),
  }
  if not showMods[id] then
    return rows
  end
  if modLists[id] == nil then
    loadMods(id)
  end
  local list = modLists[id]
  if not list then
    rows[#rows + 1] = ui.label({ text = noctalia.tr("ui.mods-loading"), fontSize = 11, color = "on_surface_variant" })
    return rows
  end
  rows[#rows + 1] = ui.input({ key = "mods-filter-" .. id, value = modFilter[id] or "", controlSize = "sm",
    placeholder = noctalia.tr("ui.mods-filter"),
    onChange = function(text) modFilter[id] = text; render() end })
  local needle = tostring(modFilter[id] or ""):lower()
  local shown, matched = 0, 0
  for _, m in ipairs(list) do
    local name = (m.name ~= nil and m.name ~= "") and m.name or m.file
    if needle == "" or (tostring(name) .. " " .. tostring(m.file)):lower():find(needle, 1, true) then
      matched = matched + 1
      if shown < MAX_MODS then
        shown = shown + 1
        rows[#rows + 1] = ui.row({ key = "mod-" .. id .. "-" .. tostring(m.file), align = "center", gap = 8, paddingH = 20,
          tooltip = tostring(m.file) }, {
          ui.label({ text = tostring(name), fontSize = 11, flexGrow = 1, maxLines = 1 }),
          ui.label({ text = tostring(m.version or ""), fontSize = 11, color = "on_surface_variant", maxLines = 1 }),
        })
      end
    end
  end
  if matched == 0 then
    rows[#rows + 1] = ui.label({ text = noctalia.tr("ui.mods-none"), fontSize = 11, color = "on_surface_variant" })
  elseif matched > shown then
    rows[#rows + 1] = ui.label({ text = noctalia.tr("ui.mods-more", { count = tostring(matched - shown) }),
                                 fontSize = 11, color = "on_surface_variant" })
  end
  return rows
end

local function gbText(v)
  return v == math.floor(v) and string.format("%d", v) or string.format("%.1f", v)
end

local function saveRam(server)
  local d = ramDraft[server.id]
  sending[server.id] = true
  render()
  local minMb, maxMb = math.floor(d.min * 1024 + 0.5), math.floor(d.max * 1024 + 0.5)
  shared.run({ "ram", server.id, string.format("%d", minMb), string.format("%d", maxMb) }, function(result)
    sending[server.id] = nil
    if result ~= nil and result.ok then
      ramDraft[server.id] = nil
      noctalia.notify(shared.config().title, noctalia.tr("ui.ram-saved", { server = tostring(server.name),
        min = gbText(d.min), max = gbText(d.max) }))
    else
      noctalia.notifyError(shared.config().title, noctalia.tr("ui.ram-failed", { server = tostring(server.name),
        error = tostring(result and result.error or "no answer") }))
    end
    refresh(true)
  end)
end

-- A typeable number in its own box, with - / + for 1 GB steps.
local function stepper(key, label, value, onType, onMinus, onPlus)
  return ui.row({ align = "center", gap = 4 }, {
    ui.label({ text = label, fontSize = 11, color = "on_surface_variant" }),
    ui.button({ glyph = "minus", variant = "ghost", controlSize = "sm", onClick = onMinus }),
    ui.input({ key = key, value = value, controlSize = "sm", width = 56, onChange = onType }),
    ui.label({ text = "GB", fontSize = 11, color = "on_surface_variant" }),
    ui.button({ glyph = "plus", variant = "ghost", controlSize = "sm", onClick = onPlus }),
  })
end

local function ramRows(s, busy)
  local r = s.ram
  if r == nil then
    return {}
  end
  local id = s.id
  local function gb(bytes)
    local v = (tonumber(bytes) or 0) / GB
    return v == math.floor(v) and string.format("%d", v) or string.format("%.1f", v)
  end
  local text = (tonumber(r.max) or 0) > 0 and noctalia.tr("ui.ram-line", { min = gb(r.min > 0 and r.min or r.max), max = gb(r.max) })
    or noctalia.tr("ui.ram-default")
  local d = ramDraft[id]
  local rows = {
    ui.row({ align = "center", gap = 6 }, {
      ui.glyph({ name = "cpu", size = 14, color = "on_surface_variant" }),
      ui.label({ text = text, fontSize = 11, color = "on_surface_variant", flexGrow = 1 }),
      r.pending and ui.label({ text = noctalia.tr("ui.ram-pending"), fontSize = 11, color = "tertiary" })
        or ui.spacer({ width = 1 }),
      ui.button({ glyph = d and "x" or "pencil", variant = "ghost", controlSize = "sm", tooltip = noctalia.tr("ui.ram-edit"),
        onClick = function()
          if ramDraft[id] then
            ramDraft[id] = nil
          else
            local max = math.max(1, math.floor((tonumber(r.max) or 0) / GB + 0.5))
            local min = math.max(1, math.floor((tonumber(r.min) or 0) / GB + 0.5))
            if (tonumber(r.max) or 0) == 0 then
              min, max = 4, 8
            elseif (tonumber(r.min) or 0) == 0 then
              min = max
            end
            ramDraft[id] = { min = math.min(min, max), max = max }
          end
          render()
        end }),
    }),
  }
  if d ~= nil then
    local view = shared.view(data)
    local total = math.floor(((view and view.host and tonumber(view.host.mem_total)) or 0) / GB)
    local top = math.max(1, total - 2)
    -- Typed text stays as typed (d.minText) until it is a number again; the
    -- buttons step the number and rewrite the text.
    local function typed(field)
      return function(text)
        d[field .. "Text"] = text
        local v = tonumber((tostring(text):gsub(",", ".")))
        d[field] = v ~= nil and math.floor(v * 10 + 0.5) / 10 or nil
        render()
      end
    end
    local function step(field, delta)
      return function()
        local v = (d[field] or 0) + delta
        d[field] = math.max(1, math.min(top, math.floor(v + 0.5)))
        d[field .. "Text"] = nil
        render()
      end
    end
    local valid = d.min ~= nil and d.max ~= nil and d.min >= 0.5 and d.min <= d.max and d.max <= top
    rows[#rows + 1] = ui.row({ align = "center", gap = 10, paddingH = 20 }, {
      stepper("ram-min-" .. id, noctalia.tr("ui.ram-min"), d.minText or gbText(d.min or 0), typed("min"),
        step("min", -1), step("min", 1)),
      stepper("ram-max-" .. id, noctalia.tr("ui.ram-max"), d.maxText or gbText(d.max or 0), typed("max"),
        step("max", -1), step("max", 1)),
    })
    rows[#rows + 1] = ui.row({ align = "center", gap = 8, paddingH = 20 }, {
      ui.label({ text = valid and noctalia.tr("ui.ram-hint", { file = tostring(r.file) })
        or noctalia.tr("ui.ram-invalid", { top = tostring(top) }), fontSize = 10,
        color = valid and "on_surface_variant" or "error", flexGrow = 1, maxLines = 2 }),
      ui.button({ text = noctalia.tr("ui.ram-save"), variant = "primary", controlSize = "sm",
        enabled = valid and not (busy or sending[id]), onClick = function() saveRam(s) end }),
    })
  end
  return rows
end

local function serverCard(s)
  local busy = shared.busy(s)
  local state = tostring(s.state)
  local stateText = noctalia.tr("ui.state-" .. state)
  if busy ~= nil and busy.action == "restart" then
    stateText = noctalia.tr("ui.restarting")
  elseif busy ~= nil and busy.action == "backup" then
    stateText = noctalia.tr("ui.backing-up")
  elseif busy ~= nil and busy.action == "restore" then
    stateText = noctalia.tr("ui.restoring")
  end

  local bits = { noctalia.tr("ui.port", { port = tostring(s.port) }) }
  if s.version ~= nil and s.version ~= "" then
    bits[#bits + 1] = tostring(s.version)
  end
  if s.uptime ~= nil then
    bits[#bits + 1] = noctalia.tr("ui.up", { time = shared.duration(s.uptime) })
  end
  if s.rss ~= nil then
    bits[#bits + 1] = shared.bytes(s.rss)
  end
  if s.cpu ~= nil then
    bits[#bits + 1] = string.format("%.0f%% CPU", tonumber(s.cpu) or 0)
  end

  local rows = titleRows(s, stateText, STATE_COLOR[state] or "on_surface")
  rows[#rows + 1] = ui.label({ text = table.concat(bits, " · "), fontSize = 11, color = "on_surface_variant", maxLines = 2 })
  if state == "running" then
    local players = noctalia.tr("ui.players", { online = tostring(s.online or 0), max = tostring(s.max or 0) })
    if s.names ~= nil and #s.names > 0 then
      players = players .. ": " .. table.concat(s.names, ", ")
    end
    rows[#rows + 1] = ui.row({ align = "center", gap = 6 }, {
      ui.glyph({ name = "users", size = 14, color = (tonumber(s.online) or 0) > 0 and "primary" or "on_surface_variant" }),
      ui.label({ text = players, fontSize = 12, maxLines = 3, flexGrow = 1 }),
    })
  end
  local auto = s.auto
  if auto ~= nil and (tonumber(auto.gave_up) or 0) > 0 then
    rows[#rows + 1] = ui.label({ text = noctalia.tr("ui.auto-gave-up", { ago = shared.ago(auto.gave_up) }),
                                 fontSize = 11, color = "error", maxLines = 2 })
  elseif auto ~= nil and os.time() - (tonumber(auto.last) or 0) < 86400 then
    rows[#rows + 1] = ui.label({ text = noctalia.tr("ui.auto-last", { ago = shared.ago(auto.last),
      count = tostring(auto.restarts or 0) }), fontSize = 11, color = "tertiary", maxLines = 2 })
  end
  local lag = s.lag
  if lag ~= nil and os.time() - (tonumber(lag.last) or 0) < 3600 then
    rows[#rows + 1] = ui.row({ align = "center", gap = 6 }, {
      ui.glyph({ name = "alert-triangle", size = 14, color = "tertiary" }),
      ui.label({ text = noctalia.tr("ui.lag-line", { count = tostring(lag.recent or 0),
        behind = string.format("%.1f", (tonumber(lag.behind_ms) or 0) / 1000), ago = shared.ago(lag.last) }),
        fontSize = 11, color = "tertiary", maxLines = 2, flexGrow = 1 }),
    })
  end
  local a = s.action
  if a ~= nil and a.phase == "failed" and os.time() - (tonumber(a.updated) or 0) < 3600 then
    rows[#rows + 1] = ui.label({ text = noctalia.tr("ui.last-failed", {
      action = noctalia.tr("ui.btn-" .. tostring(a.action)), error = tostring(a.error or "?"), ago = shared.ago(a.updated),
    }), fontSize = 11, color = "error", maxLines = 3 })
  end
  for _, r in ipairs(backupRows(s, busy ~= nil)) do
    rows[#rows + 1] = r
  end
  for _, r in ipairs(ramRows(s, busy ~= nil)) do
    rows[#rows + 1] = r
  end
  for _, r in ipairs(packRows(s)) do
    rows[#rows + 1] = r
  end
  local buttons = actionButtons(s, busy ~= nil)
  table.insert(buttons, 1, ui.spacer({ flexGrow = 1 }))
  -- Keeps the buttons at the bottom when the card is stretched to its row's height.
  rows[#rows + 1] = ui.spacer({ flexGrow = 1 })
  rows[#rows + 1] = ui.row({ align = "center", gap = 6 }, buttons)

  return ui.column({
    key = "server-" .. tostring(s.id),
    width = CARD_WIDTH,
    gap = 6,
    padding = 12,
    radius = 10,
    fill = state == "stopped" and "surface_variant/0.75" or "surface_variant",
    border = state == "running" and "primary/0.80" or "outline/0.55",
    borderWidth = 1,
  }, rows)
end

-- Copying the agent to the box, offered while it is missing there.
local deploying = false
local function deploy()
  deploying = true
  render()
  shared.run({ "deploy" }, function(result)
    deploying = false
    if result ~= nil and result.ok then
      noctalia.notify(shared.config().title, noctalia.tr("ui.deploy-done"))
    else
      noctalia.notifyError(shared.config().title, tostring(result and result.error or "no answer"))
    end
    refresh(true)
  end)
end

-- Adding a server -------------------------------------------------------------

local function loadCandidates()
  cands = false
  shared.run({ "candidates" }, function(result)
    cands = result ~= nil and result.ok and result.candidates or {}
    render()
  end)
end

local function startInstall(args)
  shared.run(args, function(result)
    if result == nil or not result.ok then
      noctalia.notifyError(shared.config().title, tostring(result and result.error or "no answer"))
    end
    refresh(true)
  end)
end

local function progressRow(glyph, text, color, fraction)
  local rows = {
    ui.row({ align = "center", gap = 6 }, {
      ui.glyph({ name = glyph, size = 14, color = color }),
      ui.label({ text = text, fontSize = 11, color = color, flexGrow = 1, maxLines = 3 }),
    }),
  }
  if fraction ~= nil then
    rows[#rows + 1] = ui.progress({ progress = math.max(0, math.min(1, fraction)), track = "on_surface/0.22", height = 5,
                                    radius = 2 })
  end
  return ui.column({ gap = 4 }, rows)
end

-- The upload from this PC and the download on the server, while they run and a
-- while after they finish.
local function installRows(view)
  local rows = {}
  local i = data and data.install
  if i ~= nil then
    local name = tostring(i.name or "")
    local age = os.time() - (tonumber(i.updated) or 0)
    if i.phase == "uploading" then
      rows[#rows + 1] = progressRow("upload", noctalia.tr("ui.install-uploading", { name = name,
        percent = tostring(i.percent or 0) }), "on_surface", (tonumber(i.percent) or 0) / 100)
    elseif i.phase == "done" and age < DONE_SHOWN_S then
      rows[#rows + 1] = progressRow("circle-check", noctalia.tr("ui.install-done", { name = name,
        folder = tostring(i.folder) }), "primary")
    elseif i.phase == "failed" and age < FAILED_SHOWN_S then
      rows[#rows + 1] = progressRow("alert-triangle", noctalia.tr("ui.install-failed", { name = name,
        error = tostring(i.error or "?") }), "error")
    elseif running(i) then
      rows[#rows + 1] = progressRow("loader", noctalia.tr("ui.install-" .. tostring(i.phase), { name = name }),
        "on_surface")
    end
  end
  local f = view and view.fetch
  if f ~= nil then
    local name = tostring(f.name or f.url or "")
    local age = os.time() - (tonumber(f.updated) or 0)
    if f.phase == "downloading" then
      local total = tonumber(f.total) or 0
      local text = total > 0 and noctalia.tr("ui.fetch-downloading-of", { name = name, got = shared.bytes(f.got),
        total = shared.bytes(total) }) or noctalia.tr("ui.fetch-downloading", { name = name, got = shared.bytes(f.got) })
      rows[#rows + 1] = progressRow("download", text, "on_surface", total > 0 and (tonumber(f.got) or 0) / total or nil)
    elseif f.phase == "unpacking" then
      rows[#rows + 1] = progressRow("loader", noctalia.tr("ui.install-unpacking", { name = name }), "on_surface")
    elseif f.phase == "done" and age < DONE_SHOWN_S then
      rows[#rows + 1] = progressRow("circle-check", noctalia.tr("ui.install-done", { name = name,
        folder = tostring(f.folder) }), "primary")
    elseif f.phase == "failed" and age < FAILED_SHOWN_S then
      rows[#rows + 1] = progressRow("alert-triangle", noctalia.tr("ui.install-failed", { name = name,
        error = tostring(f.error or "?") }), "error")
    end
  end
  return rows
end

local function card(key, rows, border)
  return ui.column({ key = key, gap = 8, padding = 12, radius = 10, width = FULL_WIDTH, fill = "surface_variant",
                     border = border or "outline/0.55", borderWidth = 1 }, rows)
end

local function addCard(view)
  local busy = installing(view)
  local rows = {
    ui.row({ align = "center", gap = 8 }, {
      ui.glyph({ name = "package-import", size = 16, color = "primary" }),
      ui.label({ text = noctalia.tr("ui.add-title"), fontSize = 14, fontWeight = "bold", flexGrow = 1 }),
      ui.button({ glyph = "x", variant = "ghost", controlSize = "sm", onClick = function() adding = false; render() end }),
    }),
    ui.label({ text = noctalia.tr("ui.add-hint"), fontSize = 11, color = "on_surface_variant", maxLines = 2 }),
  }
  if busy then
    rows[#rows + 1] = ui.label({ text = noctalia.tr("ui.add-busy"), fontSize = 11, color = "tertiary" })
  end
  rows[#rows + 1] = ui.label({ text = noctalia.tr("ui.add-recent"), fontSize = 12, fontWeight = "bold" })
  if cands == false then
    rows[#rows + 1] = ui.label({ text = noctalia.tr("ui.loading"), fontSize = 11, color = "on_surface_variant" })
  elseif cands == nil or #cands == 0 then
    rows[#rows + 1] = ui.label({ text = noctalia.tr("ui.add-none"), fontSize = 11, color = "on_surface_variant" })
  else
    for _, c in ipairs(cands) do
      local path = c.path
      rows[#rows + 1] = ui.row({ key = "cand-" .. tostring(path), align = "center", gap = 8, paddingH = 20,
                                 tooltip = tostring(path) }, {
        ui.label({ text = tostring(c.name), fontSize = 11, flexGrow = 1, maxLines = 1 }),
        ui.label({ text = shared.bytes(c.size) .. " · " .. shared.ago(c.time), fontSize = 11, color = "on_surface_variant" }),
        ui.button({ text = noctalia.tr("ui.add-upload"), glyph = "upload", variant = "outline", controlSize = "sm",
          enabled = not busy, onClick = function() startInstall({ "install", path }) end }),
      })
    end
  end
  local boxZips = view and view.box_zips or {}
  if #boxZips > 0 then
    rows[#rows + 1] = ui.label({ text = noctalia.tr("ui.add-box"), fontSize = 12, fontWeight = "bold" })
    for _, z in ipairs(boxZips) do
      local name = z.name
      rows[#rows + 1] = ui.row({ key = "boxzip-" .. tostring(name), align = "center", gap = 8, paddingH = 20 }, {
        ui.label({ text = "~/" .. tostring(name), fontSize = 11, flexGrow = 1, maxLines = 1 }),
        ui.label({ text = shared.bytes(z.size) .. " · " .. shared.ago(z.time), fontSize = 11, color = "on_surface_variant" }),
        ui.button({ text = noctalia.tr("ui.add-unpack"), glyph = "package-import", variant = "outline", controlSize = "sm",
          enabled = not busy, tooltip = noctalia.tr("ui.add-unpack-tip"),
          onClick = function() startInstall({ "install", "box", name }) end }),
      })
    end
  end
  rows[#rows + 1] = ui.row({ align = "center", gap = 8 }, {
    ui.button({ text = noctalia.tr("ui.add-pick"), glyph = "folder-open", variant = "outline", controlSize = "sm",
      enabled = not busy, onClick = function() startInstall({ "install", "pick" }) end }),
  })
  local link = tostring(linkText)
  rows[#rows + 1] = ui.label({ text = noctalia.tr("ui.add-link"), fontSize = 12, fontWeight = "bold" })
  rows[#rows + 1] = ui.row({ align = "center", gap = 8 }, {
    ui.input({ key = "add-link", value = link, controlSize = "sm", flexGrow = 1, placeholder = "https://…/server.zip",
      onChange = function(text) linkText = text; render() end }),
    ui.button({ text = noctalia.tr("ui.add-fetch"), glyph = "download", variant = "outline", controlSize = "sm",
      enabled = not busy and link:match("^https?://") ~= nil,
      onClick = function()
        linkText = ""
        startInstall({ "install", "url", link })
      end }),
  })
  return card("add", rows, "primary/0.80")
end

-- A new server's setup ----------------------------------------------------------

local function setupStep(s, kind, arg)
  sending[s.id] = true
  confirm = nil
  render()
  shared.run({ "setup", kind, s.id, arg }, function(result)
    sending[s.id] = nil
    if result == nil or not result.ok then
      noctalia.notifyError(shared.config().title, noctalia.tr("ui.setup-failed", { step = kind,
        server = tostring(s.name), error = tostring(result and result.error or "no answer") }))
    elseif kind == "save-run" then
      convertView[s.id] = nil
    elseif kind == "port" then
      portDraft[s.id] = nil
    end
    refresh(true)
  end)
end

local function loadConvert(s)
  convertView[s.id] = false
  render()
  shared.run({ "setup", "convert", s.id }, function(result)
    if result == nil or not result.ok then
      convertView[s.id] = nil
      noctalia.notifyError(shared.config().title, noctalia.tr("ui.setup-failed", { step = "convert",
        server = tostring(s.name), error = tostring(result and result.error or "no answer") }))
    else
      convertView[s.id] = result
    end
    render()
  end)
end

-- The .bat a conversion reads: run.bat, else start.bat, else the first one (like the agent).
local function batFor(s)
  for _, want in ipairs({ "run.bat", "start.bat" }) do
    for _, b in ipairs(s.bats or {}) do
      if tostring(b):lower() == want then
        return b
      end
    end
  end
  return (s.bats or {})[1]
end

local function convertRows(s)
  local c = convertView[s.id]
  if c == false then
    return { ui.label({ text = noctalia.tr("ui.convert-loading", { bat = tostring(batFor(s)) }), fontSize = 11,
                        color = "on_surface_variant" }) }
  end
  local half = math.floor((FULL_WIDTH - 24 - 8) / 2)
  local function code(title, text)
    return ui.column({ gap = 4, width = half, padding = 8, radius = 6, fill = "surface/0.90", border = "outline/0.50",
                       borderWidth = 1 }, {
      ui.label({ text = title, fontSize = 11, fontWeight = "bold" }),
      ui.label({ text = (tostring(text):gsub("%s+$", "")), fontSize = 10, fontFamily = "monospace", maxLines = 60 }),
    })
  end
  local rows = {
    ui.label({ text = noctalia.tr("ui.convert-title", { bat = tostring(c.bat) }), fontSize = 12, fontWeight = "bold" }),
    ui.row({ gap = 8, align = "start" }, {
      code(noctalia.tr("ui.convert-bat", { bat = tostring(c.bat) }), c.bat_text),
      code(noctalia.tr("ui.convert-new"), c.run_sh),
    }),
  }
  local nojava = false
  for _, n in ipairs(c.flagged or {}) do
    if n == 0 then
      nojava = true
    end
  end
  if nojava then
    rows[#rows + 1] = ui.label({ text = noctalia.tr("ui.convert-nojava"), fontSize = 11, color = "error", maxLines = 2 })
  elseif #(c.flagged or {}) > 0 then
    rows[#rows + 1] = ui.label({ text = noctalia.tr("ui.convert-flagged"), fontSize = 11, color = "tertiary", maxLines = 2 })
  end
  rows[#rows + 1] = ui.row({ align = "center", gap = 8 }, {
    ui.spacer({ flexGrow = 1 }),
    ui.button({ text = noctalia.tr("ui.cancel"), variant = "ghost", controlSize = "sm",
      onClick = function() convertView[s.id] = nil; render() end }),
    ui.button({ text = noctalia.tr("ui.convert-save"), glyph = "device-floppy", variant = "primary", controlSize = "sm",
      enabled = not sending[s.id], onClick = function() setupStep(s, "save-run") end }),
  })
  return rows
end

-- Names of the other servers (set up or not) that use port.
local function portUsers(view, port, own)
  local names = {}
  for _, o in ipairs(view.servers or {}) do
    if tonumber(o.port) == port then
      names[#names + 1] = tostring(o.name)
    end
  end
  for _, o in ipairs(view.setup or {}) do
    if o.id ~= own and tonumber(o.port) == port then
      names[#names + 1] = tostring(o.name)
    end
  end
  return names
end

local function portRows(s, view, working)
  local c = shared.config()
  local first, last = c.first, c.last
  local current = tonumber(s.port) or 0
  local draft = portDraft[s.id]
  if draft == nil then
    draft = current > 0 and tostring(current) or nil
    if draft == nil then
      for p = first, last do
        if #portUsers(view, p, s.id) == 0 then
          draft = tostring(p)
          break
        end
      end
    end
    draft = draft or tostring(first)
  end
  local n = tonumber(draft)
  local valid = n ~= nil and n >= first and n <= last and n == math.floor(n)
  local hint
  if not valid then
    hint = noctalia.tr("ui.setup-port-range", { first = tostring(first), last = tostring(last) })
  else
    local users = portUsers(view, n, s.id)
    hint = #users > 0 and noctalia.tr("ui.setup-port-used", { servers = table.concat(users, ", ") })
      or noctalia.tr("ui.setup-port-free")
  end
  local function step(delta)
    return function()
      local v = math.max(first, math.min(last, (tonumber(draft) or first) + delta))
      portDraft[s.id] = tostring(v)
      render()
    end
  end
  return {
    ui.row({ align = "center", gap = 6 }, {
      ui.glyph({ name = current > 0 and "circle-check" or "plug", size = 14,
                 color = current > 0 and "primary" or "on_surface_variant" }),
      ui.label({ text = current > 0 and noctalia.tr("ui.setup-port-current", { port = tostring(current) })
        or noctalia.tr("ui.setup-port-none"), fontSize = 11, color = "on_surface_variant", width = 140 }),
      ui.button({ glyph = "minus", variant = "ghost", controlSize = "sm", onClick = step(-1) }),
      ui.input({ key = "port-" .. s.id, value = draft, controlSize = "sm", width = 72,
        onChange = function(text) portDraft[s.id] = text; render() end }),
      ui.button({ glyph = "plus", variant = "ghost", controlSize = "sm", onClick = step(1) }),
      ui.button({ text = noctalia.tr("ui.setup-port-set"), variant = "outline", controlSize = "sm",
        enabled = valid and not working and n ~= current, onClick = function() setupStep(s, "port", tostring(n)) end }),
      ui.label({ text = hint, fontSize = 11, color = valid and "on_surface_variant" or "error", flexGrow = 1, maxLines = 2 }),
    }),
  }
end

local function setupCard(s, view)
  local busy = shared.busy(s)
  local working = busy ~= nil or sending[s.id] == true
  local state = tostring(s.state)
  local rows = titleRows(s, noctalia.tr("ui.state-" .. state), "tertiary")
  rows[#rows + 1] = ui.label({ text = noctalia.tr("ui.setup-from", { ago = shared.ago(s.installed), source = tostring(s.source) })
    .. " · ~/" .. tostring(s.folder), fontSize = 11, color = "on_surface_variant", maxLines = 2 })
  local a = s.action
  if a ~= nil and a.phase == "failed" and os.time() - (tonumber(a.updated) or 0) < FAILED_SHOWN_S then
    rows[#rows + 1] = ui.label({ text = noctalia.tr("ui.last-failed", {
      action = noctalia.tr("ui.btn-" .. tostring(a.action)), error = tostring(a.error or "?"), ago = shared.ago(a.updated),
    }), fontSize = 11, color = "error", maxLines = 3 })
  end
  -- Start script
  if s.script ~= nil and s.script ~= "" then
    rows[#rows + 1] = ui.row({ align = "center", gap = 6 }, {
      ui.glyph({ name = "circle-check", size = 14, color = "primary" }),
      ui.label({ text = noctalia.tr("ui.setup-script-ok", { script = tostring(s.script) }), fontSize = 11,
                 color = "on_surface_variant", flexGrow = 1 }),
    })
  else
    rows[#rows + 1] = ui.row({ align = "center", gap = 6 }, {
      ui.glyph({ name = "file-alert", size = 14, color = "tertiary" }),
      ui.label({ text = noctalia.tr("ui.setup-script-missing"), fontSize = 11, color = "tertiary", flexGrow = 1 }),
    })
    local choices = {}
    local bat = batFor(s)
    if bat ~= nil then
      choices[#choices + 1] = ui.button({ text = noctalia.tr("ui.setup-make-run", { bat = tostring(bat) }),
        glyph = "file-code", variant = "outline", controlSize = "sm", enabled = not working and convertView[s.id] == nil,
        onClick = function() loadConvert(s) end })
    end
    for _, jar in ipairs(s.installers or {}) do
      choices[#choices + 1] = ui.button({ text = noctalia.tr("ui.setup-installer", { jar = tostring(jar) }),
        glyph = "package", variant = "outline", controlSize = "sm", enabled = not working,
        tooltip = noctalia.tr("ui.setup-installer-tip"), onClick = function() act(s, "installer", jar) end })
    end
    for _, jar in ipairs(s.jars or {}) do
      choices[#choices + 1] = ui.button({ text = noctalia.tr("ui.setup-make-run-jar", { jar = tostring(jar) }),
        glyph = "file-plus", variant = "outline", controlSize = "sm", enabled = not working,
        tooltip = noctalia.tr("ui.setup-make-run-jar-tip"), onClick = function() setupStep(s, "make-run", jar) end })
    end
    for _, script in ipairs(s.starters or {}) do
      choices[#choices + 1] = ui.button({ text = noctalia.tr("ui.setup-use-script", { script = tostring(script) }),
        glyph = "terminal", variant = "outline", controlSize = "sm", enabled = not working,
        tooltip = noctalia.tr("ui.setup-use-script-tip"), onClick = function() setupStep(s, "script", script) end })
    end
    if #choices > 0 then
      rows[#rows + 1] = ui.row({ align = "center", gap = 6, paddingH = 20 }, choices)
    end
  end
  if convertView[s.id] ~= nil then
    for _, r in ipairs(convertRows(s)) do
      rows[#rows + 1] = r
    end
  end
  -- Java: which install the start script runs, with the one the Minecraft version needs marked.
  if s.script ~= nil and s.script ~= "" and #(s.javas or {}) > 0 then
    local needed = tonumber(s.java_needed) or 0
    local cells = {
      ui.glyph({ name = "coffee", size = 14, color = "on_surface_variant" }),
      ui.label({ text = noctalia.tr("ui.setup-java"), fontSize = 11, color = "on_surface_variant" }),
    }
    local options = { { label = noctalia.tr("ui.setup-java-default"), path = "java" } }
    for _, j in ipairs(s.javas) do
      options[#options + 1] = { label = tostring(j.major) .. (tonumber(j.major) == needed and " ✓" or ""), path = j.path }
    end
    for _, o in ipairs(options) do
      local current = tostring(s.java) == o.path
      cells[#cells + 1] = ui.button({ text = o.label, variant = current and "primary" or "outline", controlSize = "sm",
        enabled = not working and not current, tooltip = o.path,
        onClick = function() setupStep(s, "java", o.path) end })
    end
    local hint = needed > 0 and noctalia.tr("ui.setup-java-needed", { mc = tostring(s.mc), java = tostring(needed) })
      or noctalia.tr("ui.setup-java-current", { java = tostring(s.java) })
    cells[#cells + 1] = ui.label({ text = hint, fontSize = 11, color = "on_surface_variant", flexGrow = 1, maxLines = 2 })
    rows[#rows + 1] = ui.row({ align = "center", gap = 6 }, cells)
  end
  -- Port, RAM, EULA
  for _, r in ipairs(portRows(s, view, working)) do
    rows[#rows + 1] = r
  end
  for _, r in ipairs(ramRows(s, working)) do
    rows[#rows + 1] = r
  end
  rows[#rows + 1] = ui.row({ align = "center", gap = 8 }, {
    ui.toggle({ checked = s.eula == true, onChange = function(value)
      if (value == true or tostring(value) == "true") and not s.eula and not working then
        setupStep(s, "eula")
      else
        render()
      end
    end }),
    ui.label({ text = noctalia.tr("ui.setup-eula"), fontSize = 11, flexGrow = 1 }),
  })
  -- Buttons
  local buttons = {}
  if s.tmux ~= nil and s.tmux ~= "" then
    buttons[#buttons + 1] = ui.button({ glyph = "terminal-2", variant = "ghost", controlSize = "sm",
      tooltip = noctalia.tr("ui.console", { session = s.tmux }),
      onClick = function()
        noctalia.runInTerminal("ssh -t " .. shared.config().host .. " tmux attach -t '" .. tostring(s.tmux):gsub("'", "") .. "'")
      end })
  end
  if confirm ~= nil and confirm.id == s.id and confirm.kind == "remove" and noctalia.nowMs() - confirm.at < CONFIRM_MS then
    buttons[#buttons + 1] = ui.button({ text = noctalia.tr("ui.setup-remove-confirm", { folder = tostring(s.folder) }),
      variant = "destructive", controlSize = "sm", onClick = function() setupStep(s, "remove") end })
    buttons[#buttons + 1] = ui.button({ text = noctalia.tr("ui.cancel"), variant = "ghost", controlSize = "sm",
      onClick = function() confirm = nil; render() end })
  else
    buttons[#buttons + 1] = ui.button({ text = noctalia.tr("ui.setup-remove"), glyph = "trash", variant = "ghost",
      controlSize = "sm", enabled = not working and state ~= "installing",
      onClick = function() confirm = { id = s.id, kind = "remove", at = noctalia.nowMs() }; render() end })
  end
  buttons[#buttons + 1] = ui.spacer({ flexGrow = 1 })
  local ready = s.script ~= nil and s.script ~= "" and (tonumber(s.port) or 0) > 0 and s.eula == true
  buttons[#buttons + 1] = ui.button({ text = noctalia.tr("ui.setup-finish"), glyph = "check", variant = "primary",
    controlSize = "sm", enabled = ready and not working and state ~= "installing", tooltip = noctalia.tr("ui.setup-finish-tip"),
    onClick = function() setupStep(s, "finish") end })
  rows[#rows + 1] = ui.row({ align = "center", gap = 6 }, buttons)
  return card("setup-" .. tostring(s.id), rows, "tertiary/0.80")
end

function render()
  if not open then
    return
  end
  local header = ui.row({ align = "center", gap = 8 }, {
    ui.glyph({ name = "server", size = 20, color = "primary" }),
    ui.label({ text = shared.config().title, fontSize = 18, fontWeight = "bold", flexGrow = 1 }),
    ui.button({ glyph = adding and "x" or "plus", variant = adding and "ghost" or "outline", controlSize = "sm",
                tooltip = noctalia.tr("ui.add-server"), onClick = function()
                  adding = not adding
                  if adding then
                    loadCandidates()
                  end
                  render()
                end }),
    ui.button({ glyph = "refresh", variant = "ghost", controlSize = "sm", enabled = not loading,
                tooltip = noctalia.tr("ui.refresh"), onClick = function() refresh(true) end }),
  })
  local view = shared.view(data)
  local body = { header }
  if data ~= nil and data.error ~= nil then
    body[#body + 1] = ui.row({ align = "center", gap = 6 }, {
      ui.glyph({ name = "alert-triangle", size = 16, color = "error" }),
      ui.label({ text = shared.config().host == "" and noctalia.tr("ui.no-host")
        or noctalia.tr("ui.unreachable", { error = tostring(data.error) }), fontSize = 12,
                 color = "error", maxLines = 3, flexGrow = 1 }),
      tostring(data.error):find("agent missing", 1, true) and ui.button({ text = noctalia.tr("ui.deploy"),
        glyph = "upload", variant = "primary", controlSize = "sm", enabled = not deploying,
        tooltip = noctalia.tr("ui.deploy-tip"), onClick = deploy }) or ui.spacer({ width = 1 }),
    })
  end
  if view == nil then
    body[#body + 1] = ui.label({ text = noctalia.tr(data == nil and "ui.loading" or "ui.no-data"),
                                 color = "on_surface_variant" })
    panel.render(ui.column({ gap = 12, padding = 16, flexGrow = 1 }, body))
    return
  end

  body[#body + 1] = hostCard(view.host or {})
  local list = {}
  for _, s in ipairs(view.servers or {}) do
    list[#list + 1] = s
  end
  table.sort(list, function(a, b)
    local oa, ob = ORDER[a.state] or 9, ORDER[b.state] or 9
    if oa ~= ob then
      return oa < ob
    end
    return tostring(a.name):lower() < tostring(b.name):lower()
  end)
  -- Cards go into rows of COLUMNS, left to right, so running servers fill the top row.
  -- Each row stretches its cards to the tallest one, so the grid stays aligned.
  local gridRows = {}
  for i, s in ipairs(list) do
    local r = math.floor((i - 1) / COLUMNS) + 1
    gridRows[r] = gridRows[r] or {}
    table.insert(gridRows[r], serverCard(s))
  end
  -- Adding a server and new servers' setup go above the server cards, full width.
  local content = {}
  if adding then
    content[#content + 1] = addCard(view)
  end
  local progress = installRows(view)
  if #progress > 0 then
    content[#content + 1] = card("install-progress", progress)
  end
  for _, s in ipairs(view.setup or {}) do
    content[#content + 1] = setupCard(s, view)
  end
  if #list == 0 then
    content[#content + 1] = ui.label({ text = noctalia.tr("ui.no-servers"), color = "on_surface_variant" })
  else
    for r, cards in ipairs(gridRows) do
      content[#content + 1] = ui.row({ key = "grid-" .. tostring(r), gap = 8, align = "stretch" }, cards)
    end
  end
  body[#body + 1] = ui.scroll({ flexGrow = 1 }, { ui.column({ gap = 8 }, content) })
  local stamp = data.error ~= nil and noctalia.tr("ui.last-seen", { ago = shared.ago(view.time) })
    or noctalia.tr("ui.updated", { ago = shared.ago(view.time) })
  body[#body + 1] = ui.label({ text = stamp, fontSize = 11, color = "on_surface_variant" })

  panel.render(ui.column({ gap = 12, padding = 16, flexGrow = 1, align = "stretch" }, body))
end

function onOpen()
  open = true
  confirm = nil
  render()
  refresh(false)
  panel.setWantsSecondTicks(true)
end

local ticks = 0
function update()
  ticks = ticks + 1
  if confirm ~= nil and noctalia.nowMs() - confirm.at >= CONFIRM_MS then
    confirm = nil
    render()
  end
  local busy = anyBusy(shared.view(data))
  if ticks % (busy and FAST_S or SLOW_S) == 0 then
    refresh(busy)
  elseif ticks % 30 == 0 then
    render() -- keep "updated … ago" and uptimes moving
  end
end

function onClose()
  open = false
  confirm = nil
  panel.setWantsSecondTicks(false)
end

function onKey(chord, pressed)
  if pressed and tostring(chord or ""):lower() == "escape" then
    panel.close()
  end
end
