-- CG3H Builder Plugin — Centralized runtime for CG3H mods
-- Scans plugins_data/ for installed CG3H mods, builds GPKs, loads textures

local LOG_PREFIX = "[CG3H]"

-- Find our plugin's data folder and the plugins_data root.
-- _PLUGIN.plugins_data_mod_folder_path may not exist when r2modman renames folders.
-- Derive from the plugin folder instead: plugins/X/... → plugins_data/X/...
local plugin_folder = _PLUGIN.plugins_mod_folder_path
local data_folder = plugin_folder:gsub("[/\\]plugins[/\\]", "/plugins_data/")
-- plugins_data root is two levels up for r2modman (plugins_data/Author-Mod/Inner/)
-- or one level up for flat layout (plugins_data/CG3HBuilder/)
local plugins_data_dir = data_folder:gsub("[/\\][^/\\]+[/\\][^/\\]+[/\\]?$", "")
if not rom.path.exists(plugins_data_dir) then
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

-- Helper: find mod.json in a directory (handles r2modman nested layout)
-- r2modman: plugins_data/AuthorName-ModName/AuthorName-ModName/mod.json
-- manual:   plugins_data/AuthorName-ModName/mod.json
local function find_mod_json(dir)
    local direct = rom.path.combine(dir, "mod.json")
    if rom.path.exists(direct) then return direct, dir end
    -- Check one level deeper (r2modman nesting)
    for _, sub in ipairs(rom.path.get_directories(dir) or {}) do
        local nested = rom.path.combine(sub, "mod.json")
        if rom.path.exists(nested) then return nested, sub end
    end
    return nil, nil
end

-- Scan for CG3H mods
local mods = {}
local mod_count = 0

for _, entry in ipairs(rom.path.get_directories(plugins_data_dir) or {}) do
    -- Skip our own folder and build output
    local dir_name = entry:match("([^/\\]+)$") or ""
    if dir_name:find("CG3HBuilder") or entry:find("build") then
        goto continue_scan
    end
    local mod_json_path, mod_data_dir = find_mod_json(entry)
    if mod_json_path then
        local file = io.open(mod_json_path, "r")
        if file then
            local content = file:read("*a")
            file:close()
            local format = content:match('"format"%s*:%s*"([^"]*)"')
            if format and format:find("cg3h%-mod") then
                local character = content:match('"character"%s*:%s*"([^"]*)"')
                local mod_name = content:match('"name"%s*:%s*"([^"]*)"')
                local mod_id = entry:match("([^/\\]+)$")
                local has_textures = content:find('"textures"') ~= nil
                local has_glb = content:find('"glb"') ~= nil
                local glb_name = content:match('"glb"%s*:%s*"([^"]*)"')

                if character and mod_id then
                    table.insert(mods, {
                        id = mod_id,
                        name = mod_name or mod_id,
                        character = character,
                        path = mod_data_dir,
                        has_textures = has_textures,
                        has_glb = has_glb,
                        glb_name = glb_name,
                    })
                    mod_count = mod_count + 1
                end
            end
        end
    end
    ::continue_scan::
end

rom.log.info(LOG_PREFIX .. " Found " .. mod_count .. " CG3H mod(s)")

-- Group mods by character
local by_character = {}
for _, mod in ipairs(mods) do
    if not by_character[mod.character] then
        by_character[mod.character] = {}
    end
    table.insert(by_character[mod.character], mod)
end

-- 1. Load all custom textures
for _, mod in ipairs(mods) do
    local pkg_name = "CG3HBuilder-" .. mod.id
    local pkg_path = rom.path.combine(mod.path, pkg_name)
    local pkg_file = pkg_path .. ".pkg"
    if mod.has_textures and rom.path.exists(pkg_file) then
        -- Method A: biome overrides (for NEW texture names)
        local pkg_hash = rom.data.get_hash_guid_from_string(pkg_name)
        for _, biome in ipairs({mod.character, "BiomeHub", "BiomeF", "BiomeIHouse"}) do
            local biome_hash = rom.data.get_hash_guid_from_string(biome)
            rom.data.load_package_overrides_set(biome_hash, {pkg_hash, biome_hash})
        end
        -- Method B: LoadPackages (for REPLACING existing texture names)
        local _loaded = false
        rom.on_import.post(function(script_name)
            if _loaded then return end
            if script_name == "Main.lua" then
                _loaded = true
                rom.game.LoadPackages{Name = pkg_path}
            end
        end)
        rom.log.info(LOG_PREFIX .. "   Textures: " .. pkg_name .. ".pkg (" .. mod.name .. ")")
    end
end

-- 2. Build GPKs (merged per character, output to CG3HBuilder folder)
-- Always run the builder — it handles cache invalidation internally
-- (rebuilds only when mods change, skips when cache key matches)
local needs_build = false
for character, char_mods in pairs(by_character) do
    for _, mod in ipairs(char_mods) do
        if mod.has_glb then
            needs_build = true
            break
        end
    end
    if needs_build then break end
end

-- Characters that need hot-reload after building
local built_characters = {}

if needs_build then
    local builder_path = rom.path.combine(builder_data_dir, "cg3h_builder.exe")
    if rom.path.exists(builder_path) then
        rom.log.info(LOG_PREFIX .. " Building GPKs (first launch)...")
        local cmd = 'cmd /c ""' .. builder_path .. '" --scan-all "' .. plugins_data_dir .. '""'
        os.execute(cmd)
        rom.log.info(LOG_PREFIX .. " Build complete")

        -- Register built GPKs with H2M file redirect and queue hot-reload
        for character, char_mods in pairs(by_character) do
            local has_glb = false
            for _, mod in ipairs(char_mods) do
                if mod.has_glb then has_glb = true; break end
            end
            if has_glb then
                local gpk_file = character .. ".gpk"
                local gpk_path = rom.path.combine(builder_data_dir, gpk_file)
                if rom.path.exists(gpk_path) then
                    rom.data.add_granny_file(gpk_file, gpk_path)
                    table.insert(built_characters, character)
                    rom.log.info(LOG_PREFIX .. " Registered GPK: " .. gpk_file)
                end
            end
        end
    else
        rom.log.info(LOG_PREFIX .. " WARNING: cg3h_builder.exe not found")
    end
else
    -- Log cached status
    for character, char_mods in pairs(by_character) do
        local has_glb = false
        for _, mod in ipairs(char_mods) do
            if mod.has_glb then has_glb = true; break end
        end
        if has_glb then
            rom.log.info(LOG_PREFIX .. " GPK cached: " .. character)
        end
    end
end

-- No hot-reload needed: add_granny_file registers BEFORE the game's
-- LoadAllModelAndAnimationData runs, so the initial load picks up our GPKs.

rom.log.info(LOG_PREFIX .. " CG3H Builder ready (" .. mod_count .. " mod(s) loaded)")
