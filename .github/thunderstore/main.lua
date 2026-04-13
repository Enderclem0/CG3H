-- CG3H Builder Plugin — entry point.
--
-- This file is intentionally thin.  It only:
--   1. Discovers the plugin/builder/data paths
--   2. Wires up require() for our sibling modules
--   3. Populates mod_state, runs the runtime pipeline, registers the UI
--
-- All three layers are swappable:
--   - mod_state.lua   — pure data (scan/parse/group)
--   - runtime.lua     — game-facing side effects (textures + builder + GPKs)
--   - ui.lua          — ImGui presentation
--
-- Change the look of the in-game manager by editing ui.lua alone.

local LOG_PREFIX = "[CG3H]"

-- ── Path discovery ─────────────────────────────────────────────────────
-- _PLUGIN.plugins_data_mod_folder_path is unreliable under r2modman
-- folder renames, so derive the data folder from the plugin folder.
-- We support both layouts:
--   flat (v3.7+):       plugins/Enderclem-CG3HBuilder/main.lua
--   legacy (pre-v3.7):  plugins/Enderclem-CG3HBuilder/CG3HBuilder/main.lua
-- Anchoring on the "plugins_data" segment handles both without the
-- fragile "strip N segments" trick used earlier.

local plugin_folder = _PLUGIN.plugins_mod_folder_path
local data_folder = plugin_folder:gsub("[/\\]plugins[/\\]", "/plugins_data/")

-- plugins_data root = everything up to and including the "plugins_data"
-- segment.  Use a direct string.find because Lua's "[/\\]" in a pattern
-- string was producing inconsistent results with mixed separators.
local plugins_data_dir
local idx = data_folder:find("plugins_data", 1, true)  -- plain find, not pattern
if idx then
    plugins_data_dir = data_folder:sub(1, idx + #"plugins_data" - 1)
else
    -- Fallback: one level up
    plugins_data_dir = data_folder:gsub("[/\\][^/\\]+[/\\]?$", "")
end

-- Find cg3h_builder.exe — could be in data_folder directly or one level deeper
local builder_data_dir = data_folder
if not rom.path.exists(rom.path.combine(data_folder, "cg3h_builder.exe")) then
    for _, sub in ipairs(rom.path.get_directories(data_folder) or {}) do
        if rom.path.exists(rom.path.combine(sub, "cg3h_builder.exe")) then
            builder_data_dir = sub
            break
        end
    end
end
local builder_path = rom.path.combine(builder_data_dir, "cg3h_builder.exe")

-- ── Module loading ─────────────────────────────────────────────────────
-- H2M's Lua plugin loader does NOT add the plugin folder to package.path,
-- so we add it ourselves before the first require.  Includes the
-- `?/init.lua` fallback so future `ui/` sub-modules load cleanly.

package.path = plugin_folder .. "/?.lua;"
            .. plugin_folder .. "/?/init.lua;"
            .. package.path

local mod_state = require("mod_state")
local runtime   = require("runtime")
local ui        = require("ui")

-- ── Pipeline ───────────────────────────────────────────────────────────

rom.log.info(LOG_PREFIX .. " plugins_data_dir = " .. plugins_data_dir)
rom.log.info(LOG_PREFIX .. " builder_data_dir = " .. builder_data_dir)

mod_state.scan(plugins_data_dir)
-- v3.8: read per-mod enable/disable state BEFORE the build so the builder
-- subprocess (which reads the same JSON) sees a consistent view.
mod_state.load_mod_state(builder_data_dir)
rom.log.info(LOG_PREFIX .. " Found " .. mod_state.count() .. " CG3H mod(s)")
for _, mod in ipairs(mod_state.mods) do
    local flag = mod_state.is_enabled(mod.id) and "" or " [disabled]"
    rom.log.info(LOG_PREFIX .. "   - " .. mod.id .. " (" .. mod.character .. ")" .. flag)
end

local runtime_ctx = {
    builder_path      = builder_path,
    builder_data_dir  = builder_data_dir,
    plugins_data_dir  = plugins_data_dir,
    game_dir          = "",  -- populated below from cg3h_status.json
}

runtime.apply(mod_state, runtime_ctx)

-- Status JSON is written by the builder during runtime.apply().  Read it
-- after the build so the UI sees fresh per-character results AND so
-- runtime_ctx.game_dir gets populated for stock-restore hot-reload.
mod_state.load_status(builder_data_dir)
runtime_ctx.game_dir = mod_state.game_dir

-- NOTE: apply_visibility is NOT called at startup because HashGuid::Lookup
-- returns 0 before the first scene loads.  The builder respects mod_state
-- at build time, so disabled mods are already excluded from the GPK.

if runtime.has_draw_gate() then
    rom.log.info(LOG_PREFIX .. " Draw-gate available — toggle is instant")
else
    rom.log.info(LOG_PREFIX .. " Draw-gate unavailable — toggle requires rebuild + restart")
end

ui.init(mod_state, {
    on_refresh = function()
        mod_state.refresh(plugins_data_dir, builder_data_dir)
    end,
    on_toggle_mod = function(mod_id, enabled)
        local character = mod_state.set_enabled(mod_id, enabled, builder_data_dir)
        if not character then
            return nil
        end

        -- Try the draw-gate for instant visual toggle.  Falls back to
        -- rebuild + restart on older H2M without the DoDraw3D hook.
        local outcome = runtime.toggle_mod_visibility(mod_id, enabled, mod_state)
        if outcome then
            return outcome
        end

        return runtime.trigger_rebuild_and_reload(character, runtime_ctx, mod_state)
    end,
    on_rebuild = function(character)
        return runtime.trigger_rebuild_and_reload(character, runtime_ctx, mod_state)
    end,
})

rom.log.info(LOG_PREFIX .. " CG3H Builder ready (" .. mod_state.count() .. " mod(s) loaded)")
