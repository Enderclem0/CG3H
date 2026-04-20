-- CG3H runtime — all game-facing side effects live here.
--
-- This module touches the game:
--   1. Loads per-mod texture PKGs via biome override + LoadPackages
--   2. Invokes cg3h_builder.exe --scan-all to build merged GPKs
--   3. Registers built GPKs with H2M via rom.data.add_granny_file
--
-- It reads from mod_state (so the UI tab shows the same mod list as what
-- gets loaded) but does not mutate UI-owned state beyond that.  Keep all
-- game calls here so the UI stays pure.

local M = {}

local LOG_PREFIX = "[CG3H]"

-- Must match tools/cg3h_constants.py::CG3H_BUILDER_FOLDER.  PKG files
-- shipped by CG3H mods are named "{PKG_PREFIX}-{mod_id}.pkg" so H2M's
-- LoadPackages filename-GUID check finds our registered builder folder
-- name inside the stem.
local PKG_PREFIX = "Enderclem-CG3HBuilder"

-- Every known biome — custom textures are registered as overrides on all
-- of them so a modded character's look persists across every area.
local ALL_BIOMES = {
    "BiomeB", "BiomeC", "BiomeChaos", "BiomeF", "BiomeG", "BiomeH",
    "BiomeHub", "BiomeI", "BiomeIHouse", "BiomeMap", "BiomeN",
    "BiomeO", "BiomeOBoss", "BiomeP", "BiomeQ", "BiomeQEnding",
}

--- Register a mod's texture PKG as a biome override for every biome +
-- for the mod's target character, AND queue a LoadPackages call for the
-- in-scene replacement path.  See project_mod_type_analysis.md for why
-- both methods are needed.
function M.load_textures(mod)
    if not mod.has_textures then
        return
    end

    -- PKG filename must start with "Enderclem-CG3HBuilder-" so H2M's
    -- LoadPackages filename check can find a registered module GUID in
    -- the stem — bad filenames hard-crash the game (TerminateProcess).
    local pkg_name = PKG_PREFIX .. "-" .. mod.id
    local pkg_path = rom.path.combine(mod.path, pkg_name)
    local pkg_file = pkg_path .. ".pkg"

    if not rom.path.exists(pkg_file) then
        return
    end

    -- Method A: biome overrides (new texture names)
    local pkg_hash = rom.data.get_hash_guid_from_string(pkg_name)
    local biomes = { mod.character }
    for _, biome in ipairs(ALL_BIOMES) do
        table.insert(biomes, biome)
    end
    for _, biome in ipairs(biomes) do
        local biome_hash = rom.data.get_hash_guid_from_string(biome)
        rom.data.load_package_overrides_set(biome_hash, { pkg_hash, biome_hash })
    end

    -- Method B: LoadPackages (replaces existing texture names)
    local loaded = false
    rom.on_import.post(function(script_name)
        if loaded then return end
        if script_name == "Main.lua" then
            loaded = true
            rom.game.LoadPackages{ Name = pkg_path }
        end
    end)

    rom.log.info(LOG_PREFIX .. "   Textures: " .. pkg_name .. ".pkg (" .. mod.name .. ")")
end

--- Run the builder exe to produce merged GPKs for every character with
-- mesh mods.  Caller provides mod_state so we can short-circuit when no
-- GLB mods are installed.
function M.run_builder(state, builder_path, plugins_data_dir)
    -- Skip if nothing needs a GPK
    local needs_build = false
    for _, mods in pairs(state.by_character) do
        for _, mod in ipairs(mods) do
            if mod.has_glb then
                needs_build = true
                break
            end
        end
        if needs_build then break end
    end
    if not needs_build then
        for char, mods in pairs(state.by_character) do
            for _, mod in ipairs(mods) do
                if mod.has_glb then
                    rom.log.info(LOG_PREFIX .. " GPK cached: " .. char)
                    break
                end
            end
        end
        return
    end

    if not rom.path.exists(builder_path) then
        rom.log.info(LOG_PREFIX .. " WARNING: cg3h_builder.exe not found")
        return
    end

    rom.log.info(LOG_PREFIX .. " Building GPKs...")
    local cmd = 'cmd /c ""' .. builder_path .. '" --scan-all "' .. plugins_data_dir .. '""'
    os.execute(cmd)
    rom.log.info(LOG_PREFIX .. " Build complete")
end

--- Register every built GPK with H2M's file-redirect table.  Must run
-- BEFORE the game's LoadAllModelAndAnimationData — which is true at plugin
-- init time, so no hot-reload is required.
function M.register_gpks(state, builder_data_dir)
    for character, char_mods in pairs(state.by_character) do
        local has_enabled_glb = false
        for _, mod in ipairs(char_mods) do
            if mod.has_glb and state.is_enabled(mod.id) then
                has_enabled_glb = true
                break
            end
        end
        if has_enabled_glb then
            local gpk_file = character .. ".gpk"
            local gpk_path = rom.path.combine(builder_data_dir, gpk_file)
            if rom.path.exists(gpk_path) then
                rom.data.add_granny_file(gpk_file, gpk_path)
                rom.log.info(LOG_PREFIX .. " Registered GPK: " .. gpk_file)
            end
        end
    end
end

-- ── v3.9: variant registration ─────────────────────────────────────────

--- Build state.variants from the builder's per-character variants map
-- (cg3h_status.json → M.build_status[char].variants).
--
-- v3.9 Option A': each mesh_replace mod that declares target.mesh_entries
-- gets its own slim variant entry emitted by the builder.  The merged
-- stock entry stays sized for MAX-of-all-mods so the drawable allocated
-- at scene-load can always accommodate a swap to any variant (which is
-- strictly <= stock in size).
--
-- Shape:
--   state.variants[char][stock_entry] = {
--     stock    = stock_entry,
--     variants = { [mod_id] = variant_entry_name },
--   }
--
-- Picking `mod_id` in the UI installs a hash remap
-- stock_entry -> variant_entry_name so DoDraw3D reads the variant's
-- GrannyMeshData (slim single-mod content) instead of stock's merged-all.
function M.register_variants(state, ctx)
    state.variants = {}
    for char, rec in pairs(state.build_status or {}) do
        local vmap = rec and rec.variants
        if vmap and next(vmap) then
            state.variants[char] = {}
            for stock_entry, mods_for_entry in pairs(vmap) do
                state.variants[char][stock_entry] = {
                    stock    = stock_entry,
                    variants = mods_for_entry,
                }
            end
        end
    end
end

--- Switch the active render entry for ONE source entry of a character.
-- `target_id == "stock"` / nil → clear remap, render the source itself.
-- Otherwise install a remap so the game renders `target_id`'s data when
-- it asks for `entry_name`.  Populates the target's texture handles
-- (mimicking PrepDraw) before installing the remap so DoDraw3D's
-- fallback path resolves them on the very first remapped frame.
function M.swap_entry(character, entry_name, target_id, state)
    if type(rom.data.draw_swap_to_variant) ~= "function"
        or type(rom.data.draw_restore_stock) ~= "function" then
        rom.log.info(LOG_PREFIX .. " [variant] H2M missing v3.9 API")
        return nil
    end

    local char_variants = state.variants and state.variants[character]
    if not char_variants then return nil end
    local entry_data = char_variants[entry_name]
    if not entry_data then return nil end

    if target_id == nil or target_id == "" or target_id == entry_name then
        rom.data.draw_restore_stock(entry_name)
        rom.log.info(LOG_PREFIX .. " [variant] " .. entry_name .. " -> merged-stock")
        return "live"
    end

    local target = entry_data.variants[target_id]
    if not target then
        rom.log.info(LOG_PREFIX .. " [variant] " .. entry_name
            .. " has no swap target " .. target_id)
        return nil
    end

    -- Populate target's GMD+0x44 (mirrors PrepDraw).  Safe because the
    -- target is by construction NOT the currently-drawn entry — picking
    -- yourself short-circuits to restore_stock above.
    if type(rom.data.draw_populate_entry_textures) == "function" then
        rom.data.draw_populate_entry_textures(target)
    end

    rom.data.draw_swap_to_variant(entry_name, target)
    rom.log.info(LOG_PREFIX .. " [variant] " .. entry_name .. " -> " .. target)
    return "live"
end

--- Apply ONE mod's body to every entry of a character that has a variant
-- for it.  Skips entries the mod doesn't cover (leaves them at their
-- current setting).  Used by the "Apply to all scenes" dropdown.
function M.swap_character_all(character, mod_id, state)
    local char_variants = state.variants and state.variants[character]
    if not char_variants then return nil end
    for entry_name, _ in pairs(char_variants) do
        M.swap_entry(character, entry_name, mod_id, state)
    end
    return "live"
end

--- On startup, apply the default view for every variant-bearing entry.
-- User-persisted picks take precedence; otherwise defaults to "stock"
-- (the re-serialized true-stock entry) so the game opens with the
-- unmodified character by default instead of merged-all.
-- Must be called after register_variants AND after LoadAllModelAndAnimationData.
-- The UI calls this on its first ImGui frame — by then the scene has loaded.
function M.apply_active_variants(state)
    if type(rom.data.draw_swap_to_variant) ~= "function" then return end
    local applied = 0
    for char, entries in pairs(state.variants or {}) do
        local user_picks = (state.active_variants or {})[char] or {}
        for entry_name, entry_data in pairs(entries) do
            local pick = user_picks[entry_name]
            if not pick or pick == "" then
                if entry_data.variants and entry_data.variants["stock"] then
                    pick = "stock"
                end
            end
            if pick and pick ~= "" then
                local outcome = M.swap_entry(char, entry_name, pick, state)
                if outcome == "live" then applied = applied + 1 end
            end
        end
    end
    if applied > 0 then
        rom.log.info(LOG_PREFIX .. " [variant] applied " .. applied
            .. " default/persisted selection(s)")
    end
end

--- Convenience: run the full runtime pipeline in one call.
-- register_variants is intentionally NOT called here — it depends on
-- state.build_status which is populated by mod_state.load_status AFTER
-- the builder writes cg3h_status.json.  main.lua calls it explicitly.
function M.apply(state, ctx)
    for _, mod in ipairs(state.mods) do
        M.load_textures(mod)
    end
    M.run_builder(state, ctx.builder_path, ctx.plugins_data_dir)
    M.register_gpks(state, ctx.builder_data_dir)
end

-- ── v3.8: draw-call visibility gate ────────────────────────────────────
-- Hooks sgg::DrawManager::DoDraw3D (+ shadow/thumbnail variants) via
-- rom.data.draw_set_visible to suppress draw calls per mesh entry.
-- Instant, no rebuild, no restart, no data mutation.

--- Check whether the H2M draw-gate API is available.  The fork DLL
-- exposes it via draw.cpp; if this returns false, the fork failed to
-- load (user installed upstream Hell2Modding instead of our fork, or
-- the DLL in Ship/ is stale).
function M.has_draw_gate()
    return type(rom.data.draw_set_visible) == "function"
end

--- Toggle visibility of a single mod's mesh entries.  Uses the draw-call
-- hook for instant visual feedback.  Returns "live" on success or nil if
-- the draw gate is unavailable.
function M.toggle_mod_visibility(mod_id, enabled, state)
    if not M.has_draw_gate() then
        return nil
    end

    local target_mod = nil
    for _, mod in ipairs(state.mods) do
        if mod.id == mod_id then
            target_mod = mod
            break
        end
    end
    if not target_mod or #target_mod.mesh_entries == 0 then
        rom.log.info(LOG_PREFIX .. " [draw-gate] no mesh entries for " .. mod_id)
        return nil
    end

    -- mesh_add mods merge their meshes INTO the stock entry alongside the
    -- body, so entry-level draw-gate would hide the body too.  Use the
    -- finer-grained per-mesh API to flip just the accessory's meshes.
    if target_mod.has_mesh_add then
        if type(rom.data.draw_set_mesh_visible) ~= "function" then
            rom.log.info(LOG_PREFIX
                .. " [mesh-gate] set_mesh_visible missing — rebuild required")
            return nil
        end
        -- For each (mesh_name, entry) the mod routes to, flip the mesh
        -- in the stock entry AND in every variant entry that mirrors it.
        -- The builder merges the accessory into every variant at build
        -- time, so when a body variant is active DoDraw3D reads the
        -- variant's GMD, not stock's — toggling only stock would leave
        -- the accessory visible under any picked variant.
        local char_variants = (state.variants or {})[target_mod.character] or {}
        local routing = target_mod.new_mesh_routing or {}
        local count = 0
        for mesh_name, entries in pairs(routing) do
            for _, entry in ipairs(entries) do
                -- Stock entry.
                if rom.data.draw_set_mesh_visible(entry, mesh_name, enabled) then
                    count = count + 1
                end
                -- All variants (stock + per-body-mod) that shadow this entry.
                local vdata = char_variants[entry]
                if vdata and vdata.variants then
                    for _, variant_entry in pairs(vdata.variants) do
                        if rom.data.draw_set_mesh_visible(variant_entry, mesh_name, enabled) then
                            count = count + 1
                        end
                    end
                end
            end
        end
        if count == 0 then
            rom.log.info(LOG_PREFIX
                .. " [mesh-gate] no meshes toggled for '" .. mod_id
                .. "' — rebuild required")
            return nil
        end
        rom.log.info(LOG_PREFIX .. " [mesh-gate] " .. mod_id
            .. " (" .. (enabled and "show" or "hide") .. ") "
            .. count .. " mesh(es)")
        return "live"
    end

    local character = target_mod.character

    if enabled then
        -- Re-enable: make all this mod's entries visible.
        for _, entry in ipairs(target_mod.mesh_entries) do
            rom.data.draw_set_visible(entry, true)
            rom.log.info(LOG_PREFIX .. " [draw-gate] show " .. entry)
        end
    else
        -- Disable: hide entries that no other ENABLED mod uses.
        local other_entries = {}
        for _, mod in ipairs(state.mods) do
            if mod.character == character
                and mod.id ~= mod_id
                and state.is_enabled(mod.id) then
                for _, entry in ipairs(mod.mesh_entries) do
                    other_entries[entry] = true
                end
            end
        end

        for _, entry in ipairs(target_mod.mesh_entries) do
            if not other_entries[entry] then
                rom.data.draw_set_visible(entry, false)
                rom.log.info(LOG_PREFIX .. " [draw-gate] hide " .. entry)
            end
        end
    end

    return "live"
end

--- Sync the draw-gate hidden set to match current mod_state.  Call once
-- at startup (after GPK registration) to handle the case where mods
-- were disabled between sessions but the GPK cache wasn't rebuilt.
function M.apply_visibility(state)
    if not M.has_draw_gate() then
        return
    end

    for character, entries in pairs(state.char_mesh_entries) do
        -- Collect entries that have at least one enabled mod.
        local enabled_entries = {}
        for _, mod in ipairs(state.mods) do
            if mod.character == character and state.is_enabled(mod.id) then
                for _, entry in ipairs(mod.mesh_entries) do
                    enabled_entries[entry] = true
                end
            end
        end

        for _, entry in ipairs(entries) do
            if not enabled_entries[entry] then
                rom.data.draw_set_visible(entry, false)
                rom.log.info(LOG_PREFIX .. " [draw-gate] startup hide " .. entry)
            end
        end
    end
end

-- v3.9 removed mid-session rebuild path.  LoadModelData isn't safe after
-- the initial load, so a runtime rebuild couldn't take effect until the
-- next game restart anyway — and the builder already runs on plugin init
-- every launch (see run_builder above).  Toggles are handled by the
-- draw-gate; adding/removing mods takes effect on the next restart.

return M
