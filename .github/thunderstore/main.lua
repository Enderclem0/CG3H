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
-- folder renames, so derive the data folder from the plugin folder
-- by swapping the "/plugins/" segment to "/plugins_data/".  Works
-- regardless of whatever nesting r2modman happens to apply on ZIP
-- import (see project_r2modman_layout.md).

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
-- Use dofile() with absolute paths instead of require() + package.path.
-- Two reasons:
--   1. require() caches into the GLOBAL package.loaded table; if any other
--      plugin happens to require a module name we used (mod_state, runtime,
--      ui), they'd silently get OUR module.
--   2. Mutating package.path persists across the entire Lua state and
--      affects every plugin that uses package.path-aware loaders (require,
--      package.searchers).  We can't predict the downstream effect.
-- dofile() takes a literal path, doesn't cache, doesn't touch package.path
-- — completely scoped to our plugin.  Modules end with `return M` so the
-- behavior matches the previous require() return value.

local function _load_module(name)
    return dofile(rom.path.combine(plugin_folder, name .. ".lua"))
end

local mod_state = _load_module("mod_state")
local runtime   = _load_module("runtime")
local ui        = _load_module("ui")

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

-- v3.11: register the SJSON-alias callback BEFORE running the
-- builder.  The callback closes over `mod_state.build_status`, which
-- the builder will populate later.  Registering early ensures the
-- callback exists when the engine first reads animation SJSON files
-- (which happens early — before runtime.apply finishes the synchronous
-- builder subprocess).  After the builder + load_status complete,
-- reload_game_data() forces the engine to re-read SJSON so the
-- callback fires with the populated state.
runtime.register_animation_aliases(mod_state)

runtime.apply(mod_state, runtime_ctx)

-- Status JSON is written by the builder during runtime.apply().  Read it
-- after the build so the UI sees fresh per-character results AND so
-- runtime_ctx.game_dir gets populated for stock-restore hot-reload.
mod_state.load_status(builder_data_dir)
runtime_ctx.game_dir = mod_state.game_dir

-- v3.9: populate mod_state.variants from the builder's status output.
-- Must run AFTER load_status so build_status[char].variants is available.
runtime.register_variants(mod_state, runtime_ctx)

-- v3.11: now that build_status is populated, ask the engine to re-read
-- its game data — our callback will inject the alias entries on the
-- second read.  No-op if no aliases are registered.
local n_aliases = runtime.reload_game_data_for_aliases(mod_state)
if n_aliases > 0 then
    rom.log.info(LOG_PREFIX .. " " .. n_aliases
        .. " custom animation alias(es) queued")
end

-- v3.11: expose the play_animation helper on rom.game so other plugins
-- can call it without knowing CG3H's internals.  rom.game is the game's
-- _G, shared across all H2M plugins.  Modders use:
--   local cg3h = rom.game.CG3H_API
--   cg3h.play_animation(rom.game.CurrentRun.Hero.ObjectId, "MyAnim")
if rom.game then
    rom.game.CG3H_API = rom.game.CG3H_API or {}
    rom.game.CG3H_API.play_animation = runtime.play_animation
    -- v3.12 B: skin selection persists immediately and applies live
    -- via SetThingProperty when the target unit is in the scene.
    -- Out-of-scene targets get auto-applied on the next room load
    -- (via runtime.install_skin_scene_hook below).
    rom.game.CG3H_API.set_active_skin = function(character, mod_id)
        mod_state.set_active_skin(character, mod_id, builder_data_dir)
        return runtime.apply_skin_for_character(mod_state, character)
    end
    rom.game.CG3H_API.clear_skin = function(character)
        mod_state.set_active_skin(character, nil, builder_data_dir)
        return runtime.apply_skin_for_character(mod_state, character)
    end
    rom.game.CG3H_API.version = "3.12.0"
end

-- v3.12 B: hook scene loads so freshly-spawned NPCs (and the hero
-- after a hub→biome transition) pick up their persisted skin without
-- the user re-clicking the picker.
runtime.install_skin_scene_hook(mod_state)

-- v3.13: drain queued bind-pose nudges on each rendered frame.  The
-- scene hook above queues one nudge per character with a mesh_add
-- accessory; this tick fires SetAnimation as soon as the target unit
-- spawns (typically a handful of frames after script import).
runtime.install_accessory_nudge_tick(mod_state)

-- NOTE: apply_visibility is NOT called at startup because HashGuid::Lookup
-- returns 0 before the first scene loads.  The builder respects mod_state
-- at build time, so disabled mods are already excluded from the GPK.

if not runtime.has_draw_gate() then
    rom.log.info(LOG_PREFIX
        .. " ERROR: draw-gate binding missing — upgrade Hell2Modding to 1.0.92 or newer")
end

local variant_count = 0
for _, entries in pairs(mod_state.variants or {}) do
    for _, entry_data in pairs(entries) do
        for _ in pairs(entry_data.variants or {}) do
            variant_count = variant_count + 1
        end
    end
end
if variant_count > 0 then
    rom.log.info(LOG_PREFIX .. " " .. variant_count .. " variant(s) registered for outfit switching")
end

-- IMPORTANT: build a context table once, then construct the rom.gui.*
-- callback closures HERE in main.lua's chunk.  When H2M's sol2 binding
-- registers a closure, the closure carries its function-environment
-- with it; if that env is ui.lua's chunk env (because the closure was
-- created INSIDE ui.lua's M.init), sol2 misroutes the callback to a
-- different plugin context — which silently corrupts ImGui rendering
-- for OTHER plugins (verified end-to-end: registering CG3H's add_imgui
-- from inside ui.init reproducibly breaks zerp-MelSkin's menu).
-- Constructing the closures here keeps their _ENV bound to main.lua,
-- which IS the plugin's m_env.  ui.lua exposes only helper functions.
local ui_ctx = {
    on_refresh = function()
        mod_state.refresh(plugins_data_dir, builder_data_dir)
    end,
    on_toggle_mod = function(mod_id, enabled)
        local character = mod_state.set_enabled(mod_id, enabled, builder_data_dir)
        if not character then
            return nil
        end
        return runtime.toggle_mod_visibility(mod_id, enabled, mod_state)
    end,
    on_set_variant_entry = function(character, entry_name, mod_id)
        mod_state.set_active_variant(character, entry_name, mod_id, builder_data_dir)
        return runtime.swap_entry(character, entry_name, mod_id, mod_state)
    end,
    on_set_variant_all = function(character, mod_id)
        local char_variants = mod_state.variants[character] or {}
        for entry_name, _ in pairs(char_variants) do
            mod_state.set_active_variant(character, entry_name, mod_id, builder_data_dir)
        end
        return runtime.swap_character_all(character, mod_id, mod_state)
    end,
    on_set_active_skin = function(character, mod_id)
        -- v3.12 B: persist first so apply_skin_for_character reads the
        -- new selection, then call SetThingProperty for the live swap.
        -- Returns "live" when applied, "transition" when the target
        -- isn't in the current scene (skin will apply on next room
        -- load via the post-import hook).
        mod_state.set_active_skin(character, mod_id, builder_data_dir)
        return runtime.apply_skin_for_character(mod_state, character)
    end,
    on_play_animation = function(target_id, anim_name)
        return runtime.play_animation(target_id, anim_name)
    end,
    on_first_frame = function()
        if rom.data.draw_sanity_check_gmd then
            rom.data.draw_sanity_check_gmd("HecateHub_Mesh")
        end
        runtime.apply_active_variants(mod_state)
        -- v3.12 B: replay every persisted skin on its live target.
        -- Characters not in the current scene get re-applied
        -- automatically on the next room load via the on_import hook
        -- installed below.
        runtime.apply_all_skins(mod_state)
    end,
}

-- One-shot first-frame guard.  Local to main.lua's chunk so the
-- closure built below has main.lua's env.
local _ui_did_first_frame = false

rom.gui.add_to_menu_bar(function()
    ui.render_menu_bar()
end)
rom.gui.add_imgui(function()
    if not _ui_did_first_frame then
        _ui_did_first_frame = true
        if ui_ctx.on_first_frame then ui_ctx.on_first_frame() end
    end
    ui.render(mod_state, ui_ctx)
end)

-- v3.9: per-mod outfit picker.  Each mesh_replace mod with declared
-- target.mesh_entries gets a slim variant entry in the merged GPK; plus
-- a per-entry "Stock" variant with true-stock bytes.  The picker swaps
-- between them; default at startup is "Stock" (applied in first frame).

rom.log.info(LOG_PREFIX .. " CG3H Builder ready (" .. mod_state.count() .. " mod(s) loaded)")
