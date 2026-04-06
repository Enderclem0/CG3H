-- CG3H Builder Plugin — Centralized runtime for CG3H mods
-- Scans plugins_data/ for installed CG3H mods, builds GPKs, loads textures

local LOG_PREFIX = "[CG3H]"

-- Find plugins_data base directory (parent of our own folder)
local base_path = _PLUGIN.plugins_data_mod_folder_path
local plugins_data_dir = base_path:gsub("[/\\][^/\\]+[/\\]?$", "")

-- Scan for CG3H mods
local mods = {}
local mod_count = 0

for _, entry in ipairs(rom.path.get_directories(plugins_data_dir) or {}) do
    -- Skip nested build output and our own folder
    local dir_name = entry:match("([^/\\]+)$") or ""
    if dir_name == "CG3HBuilder" or entry:find("build") then
        goto continue_scan
    end
    local mod_json_path = rom.path.combine(entry, "mod.json")
    if rom.path.exists(mod_json_path) then
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
                        path = entry,
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
-- Check if any mesh mods need building
local needs_build = false
for character, char_mods in pairs(by_character) do
    for _, mod in ipairs(char_mods) do
        if mod.has_glb then
            local gpk_path = rom.path.combine(_PLUGIN.plugins_data_mod_folder_path, character .. ".gpk")
            if not rom.path.exists(gpk_path) then
                needs_build = true
                break
            end
        end
    end
    if needs_build then break end
end

if needs_build then
    local builder_path = rom.path.combine(_PLUGIN.plugins_data_mod_folder_path, "cg3h_builder.exe")
    if rom.path.exists(builder_path) then
        rom.log.info(LOG_PREFIX .. " Building GPKs (first launch)...")
        os.execute(builder_path .. " --scan-all " .. plugins_data_dir .. " --game-dir ..")
        rom.log.info(LOG_PREFIX .. " Build complete — restart required for meshes to load")

        -- Show restart notification via ImGui
        rom.gui.add_always_draw_imgui(function()
            if ImGui.Begin("CG3H Builder") then
                ImGui.Text("CG3H mesh mods were built for the first time.")
                ImGui.Text("Please RESTART the game for meshes to appear.")
                ImGui.Text("")
                ImGui.Text("This message will not appear again.")
            end
            ImGui.End()
        end)
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

rom.log.info(LOG_PREFIX .. " CG3H Builder ready (" .. mod_count .. " mod(s) loaded)")
