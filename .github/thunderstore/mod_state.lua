-- CG3H mod_state — pure data layer for the runtime and the in-game UI.
--
-- This module does four things and nothing else:
--   1. Scan plugins_data/ for installed CG3H mods and parse their mod.json
--   2. Group mods by target character
--   3. Read the builder's cg3h_status.json (per-character build state)
--   4. Read/write cg3h_mod_state.json (per-mod enable/disable state)
--
-- It does NOT run the builder, touch game state, or draw anything.  The
-- UI reads from the exported table; runtime.lua mutates it once at startup.
-- Swap the UI freely; mod_state stays put.

local M = {}

M.mods = {}            -- array of { id, name, version, author, character, path, has_textures, has_glb, glb_name, mesh_entries }
M.by_character = {}    -- { [character] = { mod, ... } }
M.build_status = {}    -- { [character] = { state, gpk_path, error, duration_ms, mod_details } }
M.mod_state = {}       -- { [mod_id] = { enabled = bool } }   — user-writable
M.char_mesh_entries = {}  -- { [character] = { "HecateBattle_Mesh", "HecateHub_Mesh", ... } }
                          -- Union of mesh_entries from all installed mods for this character.
                          -- Used by runtime.lua hot_reload_character to drive per-entry reloads,
                          -- because sgg::Granny3D::mModelData is keyed by MESH ENTRY name, not
                          -- by character name.  Multi-entry characters (Hecate Battle/Hub,
                          -- Melinoe Hat/Body/Overlook) have multiple entries each.
M.builder_version = "" -- from cg3h_status.json
M.built_at = ""        -- ISO timestamp from cg3h_status.json
M.game_dir = ""        -- from cg3h_status.json (forward-slashed)
M.summary = { built = 0, cached = 0, failed = 0 }
M.last_scan_ts = 0

-- ── Helpers ────────────────────────────────────────────────────────────

-- r2modman nests plugins_data/{mod_id}/{mod_id}/mod.json; manual installs
-- put mod.json directly under plugins_data/{mod_id}/.  Find whichever.
local function find_mod_json(dir)
    local direct = rom.path.combine(dir, "mod.json")
    if rom.path.exists(direct) then
        return direct, dir
    end
    for _, sub in ipairs(rom.path.get_directories(dir) or {}) do
        local nested = rom.path.combine(sub, "mod.json")
        if rom.path.exists(nested) then
            return nested, sub
        end
    end
    return nil, nil
end

local function _field(content, key)
    return content:match('"' .. key .. '"%s*:%s*"([^"]*)"')
end

-- Parse a JSON string array under `"key": [ "a", "b", ... ]`.
-- Returns a list of the string values, empty list if the key is missing
-- or the array is malformed.  Used for target.mesh_entries since we
-- don't ship a JSON library in the plugin.
local function _string_array(content, key)
    local result = {}
    local array_block = content:match('"' .. key .. '"%s*:%s*%[([^%]]*)%]')
    if not array_block then
        return result
    end
    for value in array_block:gmatch('"([^"]*)"') do
        table.insert(result, value)
    end
    return result
end

-- ── Public API ─────────────────────────────────────────────────────────

--- Scan plugins_data/ for CG3H mods.  Populates M.mods and M.by_character.
-- Mutates state in place so there's one canonical source of truth.
function M.scan(plugins_data_dir)
    M.mods = {}
    M.by_character = {}
    M.char_mesh_entries = {}

    -- rom.path.get_directories appears to walk recursively on this H2M
    -- build, so we also get back the inner nested folder for each mod.
    -- Only accept entries whose immediate parent equals plugins_data_dir
    -- — that limits us to the top-level mod folders.
    local parent_norm = plugins_data_dir:gsub("\\", "/"):gsub("/+$", "")
    -- Also dedupe by mod_data_dir in case two walks produce the same
    -- underlying mod.json.
    local seen_data_dir = {}

    for _, entry in ipairs(rom.path.get_directories(plugins_data_dir) or {}) do
        -- Normalise and check that `entry` is a *direct* child of plugins_data_dir.
        local entry_norm = entry:gsub("\\", "/"):gsub("/+$", "")
        local entry_parent = entry_norm:gsub("/[^/]+$", "")
        local is_direct_child = (entry_parent == parent_norm)

        local dir_name = entry_norm:match("([^/]+)$") or ""
        if is_direct_child
            and not (dir_name:find("CG3HBuilder") or dir_name == "build") then
            local mod_json_path, mod_data_dir = find_mod_json(entry)
            if mod_json_path and not seen_data_dir[mod_data_dir] then
                seen_data_dir[mod_data_dir] = true
                local file = io.open(mod_json_path, "r")
                if file then
                    local content = file:read("*a")
                    file:close()
                    local format = _field(content, "format")
                    if format and format:find("cg3h%-mod") then
                        local mod = {
                            id           = dir_name,
                            name         = _field(content, "name") or "",
                            version      = _field(content, "version") or "",
                            author       = _field(content, "author") or "",
                            character    = _field(content, "character") or "",
                            path         = mod_data_dir,
                            has_textures = content:find('"textures"') ~= nil,
                            has_glb      = content:find('"glb"') ~= nil,
                            glb_name     = _field(content, "glb"),
                            mesh_entries = _string_array(content, "mesh_entries"),
                        }
                        if mod.character ~= "" and mod.id then
                            table.insert(M.mods, mod)
                            if not M.by_character[mod.character] then
                                M.by_character[mod.character] = {}
                            end
                            table.insert(M.by_character[mod.character], mod)

                            -- Accumulate the union of mesh_entries for
                            -- this character across all installed mods.
                            if not M.char_mesh_entries[mod.character] then
                                M.char_mesh_entries[mod.character] = {}
                            end
                            local seen = {}
                            for _, existing in ipairs(M.char_mesh_entries[mod.character]) do
                                seen[existing] = true
                            end
                            for _, entry in ipairs(mod.mesh_entries) do
                                if not seen[entry] then
                                    seen[entry] = true
                                    table.insert(M.char_mesh_entries[mod.character], entry)
                                end
                            end
                        end
                    end
                end
            end
        end
    end

    M.last_scan_ts = os.time()
end

--- Read cg3h_status.json (written by the Python builder) into M.build_status.
-- Missing file is fine — leaves build_status empty.  Parser is intentionally
-- minimal (string patterns only) so we don't ship a JSON lib in the plugin.
function M.load_status(builder_dir)
    M.build_status = {}
    M.builder_version = ""
    M.built_at = ""
    M.summary = { built = 0, cached = 0, failed = 0 }

    local path = rom.path.combine(builder_dir, "cg3h_status.json")
    if not rom.path.exists(path) then
        return
    end
    local f = io.open(path, "r")
    if not f then
        return
    end
    local content = f:read("*a")
    f:close()

    M.builder_version = content:match('"builder_version"%s*:%s*"([^"]*)"') or ""
    M.built_at = content:match('"built_at"%s*:%s*"([^"]*)"') or ""
    M.game_dir = content:match('"game_dir"%s*:%s*"([^"]*)"') or ""
    M.summary.built = tonumber(content:match('"built"%s*:%s*(%d+)')) or 0
    M.summary.cached = tonumber(content:match('"cached"%s*:%s*(%d+)')) or 0
    M.summary.failed = tonumber(content:match('"failed"%s*:%s*(%d+)')) or 0

    -- Walk each "Character": { ... } block inside "characters".  We
    -- intentionally do not try to parse nested arrays here — the fields we
    -- display (state, gpk_path, error, duration_ms) are all scalars, and
    -- the UI falls back to M.mods for mod lists.
    local chars_block = content:match('"characters"%s*:%s*(%b{})')
    if not chars_block then
        return
    end
    for char, body in chars_block:gmatch('"([^"]+)"%s*:%s*(%b{})') do
        local state = body:match('"state"%s*:%s*"([^"]*)"') or "unknown"
        local gpk_path = body:match('"gpk_path"%s*:%s*"([^"]*)"')
        local err = body:match('"error"%s*:%s*"([^"]*)"')
        local duration = tonumber(body:match('"duration_ms"%s*:%s*(%d+)'))
        M.build_status[char] = {
            state = state,
            gpk_path = gpk_path,
            error = err,
            duration_ms = duration,
        }
    end
end

-- ── Mod enable/disable state (v3.8) ───────────────────────────────────
-- Stored in cg3h_mod_state.json next to the cache keys.  Separate from
-- cg3h_status.json because that file is builder output; this one is
-- user input (written by the UI on checkbox toggle).

--- Read cg3h_mod_state.json into M.mod_state.  Missing → empty.
function M.load_mod_state(builder_dir)
    M.mod_state = {}
    local path = rom.path.combine(builder_dir, "cg3h_mod_state.json")
    if not rom.path.exists(path) then
        return
    end
    local f = io.open(path, "r")
    if not f then
        return
    end
    local content = f:read("*a")
    f:close()

    -- Minimal parser: we only care about the "enabled" bool per mod id.
    -- Structure is { "mods": { "mod_id": { "enabled": true/false }, ... } }.
    local mods_block = content:match('"mods"%s*:%s*(%b{})')
    if not mods_block then
        return
    end
    for mod_id, body in mods_block:gmatch('"([^"]+)"%s*:%s*(%b{})') do
        local enabled_str = body:match('"enabled"%s*:%s*(%a+)')
        local enabled = true
        if enabled_str == "false" then
            enabled = false
        end
        M.mod_state[mod_id] = { enabled = enabled }
    end
end

--- Write M.mod_state back to cg3h_mod_state.json.  Best-effort — logs a
-- warning on failure but does not error.  Hand-serialised because we
-- don't ship a JSON encoder in the plugin.
function M.save_mod_state(builder_dir)
    local path = rom.path.combine(builder_dir, "cg3h_mod_state.json")
    local lines = {}
    table.insert(lines, '{')
    table.insert(lines, '  "version": 1,')
    table.insert(lines, '  "mods": {')

    -- Sorted keys for stable file content.
    local ids = {}
    for mod_id, _ in pairs(M.mod_state) do
        table.insert(ids, mod_id)
    end
    table.sort(ids)

    for i, mod_id in ipairs(ids) do
        local entry = M.mod_state[mod_id]
        local enabled_str = entry.enabled and "true" or "false"
        local comma = (i < #ids) and "," or ""
        table.insert(lines, string.format(
            '    "%s": { "enabled": %s }%s', mod_id, enabled_str, comma))
    end

    table.insert(lines, '  }')
    table.insert(lines, '}')

    local f = io.open(path, "w")
    if not f then
        rom.log.info("[CG3H] WARNING: could not write " .. path)
        return false
    end
    f:write(table.concat(lines, "\n"))
    f:close()
    return true
end

--- Check whether a mod is enabled.  Missing entry defaults to TRUE so
-- freshly-installed mods are active without needing a state file write.
function M.is_enabled(mod_id)
    local entry = M.mod_state[mod_id]
    if not entry then
        return true
    end
    return entry.enabled ~= false
end

--- Set a mod's enabled state and persist.  Returns the character the
-- mod targets (or nil if the mod is unknown) so the caller can fire a
-- per-character rebuild without re-scanning.
function M.set_enabled(mod_id, enabled, builder_dir)
    M.mod_state[mod_id] = { enabled = enabled }
    M.save_mod_state(builder_dir)
    for _, mod in ipairs(M.mods) do
        if mod.id == mod_id then
            return mod.character
        end
    end
    return nil
end

-- ── Refresh + convenience ─────────────────────────────────────────────

--- One-shot: scan and load status + mod state.  Bound to Refresh.
function M.refresh(plugins_data_dir, builder_dir)
    M.scan(plugins_data_dir)
    M.load_status(builder_dir)
    M.load_mod_state(builder_dir)
end

--- Convenience: how many mods are installed?
function M.count()
    return #M.mods
end

--- Convenience: build state for a character, or "unknown".
function M.state_for(character)
    local rec = M.build_status[character]
    return rec and rec.state or "unknown"
end

return M
