-- What differs between copies of this plugin: its id (the bar opens the panel
-- by it) and the settings' defaults, used while Noctalia hasn't loaded the
-- settings yet. Keep the defaults the same as in plugin.toml.
return {
  id = "soulmanyes/minecraft-server-panel",
  host = "mcpanel",
  folder = "minecraft-server-panel",
  ports = "25565-25575",
  title = "Minecraft servers",
}
