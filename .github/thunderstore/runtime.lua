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

    -- v3.7 renamed the PKG prefix from "CG3HBuilder-" to "Enderclem-CG3HBuilder-"
    -- so H2M's LoadPackages filename check can find a registered module
    -- GUID in the stem.  Loading a legacy-named PKG would HARD CRASH the
    -- game (H2M calls TerminateProcess on a bad filename), so we do a
    -- one-time on-disk rename when we see the legacy name.  After the
    -- migration the mod is indistinguishable from a v3.7 build.
    local pkg_name = PKG_PREFIX .. "-" .. mod.id
    local pkg_path = rom.path.combine(mod.path, pkg_name)
    local pkg_file = pkg_path .. ".pkg"

    if not rom.path.exists(pkg_file) then
        local legacy_file = rom.path.combine(mod.path, "CG3HBuilder-" .. mod.id .. ".pkg")
        if rom.path.exists(legacy_file) then
            local ok = os.rename(legacy_file, pkg_file)
            if ok then
                rom.log.info(LOG_PREFIX .. "   Migrated legacy PKG for " .. mod.name)
            else
                rom.log.info(LOG_PREFIX .. "   WARN: could not migrate legacy PKG for "
                    .. mod.name .. " — rebuild the mod with v3.7 cg3h_build.py")
                return
            end
        else
            return
        end
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
        local has_glb = false
        for _, mod in ipairs(char_mods) do
            if mod.has_glb then has_glb = true; break end
        end
        if has_glb then
            local gpk_file = character .. ".gpk"
            local gpk_path = rom.path.combine(builder_data_dir, gpk_file)
            if rom.path.exists(gpk_path) then
                rom.data.add_granny_file(gpk_file, gpk_path)
                rom.log.info(LOG_PREFIX .. " Registered GPK: " .. gpk_file)
            end
        end
    end
end

--- Convenience: run the full runtime pipeline in one call.
function M.apply(state, ctx)
    for _, mod in ipairs(state.mods) do
        M.load_textures(mod)
    end
    M.run_builder(state, ctx.builder_path, ctx.plugins_data_dir)
    M.register_gpks(state, ctx.builder_data_dir)
end

return M
