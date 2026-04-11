-- CG3H mod_state — pure data layer for the runtime and the in-game UI.
--
-- This module does three things and nothing else:
--   1. Scan plugins_data/ for installed CG3H mods and parse their mod.json
--   2. Group mods by target character
--   3. Read the builder's cg3h_status.json (per-character build state)
--
-- It does NOT run the builder, touch game state, or draw anything.  The
-- UI reads from the exported table; runtime.lua mutates it once at startup.
-- Swap the UI freely; mod_state stays put.

local M = {}

M.mods = {}            -- array of { id, name, version, author, character, path, has_textures, has_glb, glb_name }
M.by_character = {}    -- { [character] = { mod, ... } }
M.build_status = {}    -- { [character] = { state, gpk_path, error, duration_ms, mod_details } }
M.builder_version = "" -- from cg3h_status.json
M.built_at = ""        -- ISO timestamp from cg3h_status.json
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

-- ── Public API ─────────────────────────────────────────────────────────

--- Scan plugins_data/ for CG3H mods.  Populates M.mods and M.by_character.
-- Mutates state in place so there's one canonical source of truth.
function M.scan(plugins_data_dir)
    M.mods = {}
    M.by_character = {}

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
                            id         = dir_name,
                            name       = _field(content, "name") or "",
                            version    = _field(content, "version") or "",
                            author     = _field(content, "author") or "",
                            character  = _field(content, "character") or "",
                            path       = mod_data_dir,
                            has_textures = content:find('"textures"') ~= nil,
                            has_glb    = content:find('"glb"') ~= nil,
                            glb_name   = _field(content, "glb"),
                        }
                        if mod.character ~= "" and mod.id then
                            table.insert(M.mods, mod)
                            if not M.by_character[mod.character] then
                                M.by_character[mod.character] = {}
                            end
                            table.insert(M.by_character[mod.character], mod)
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

--- One-shot: scan and load status.  Bound to the UI's Refresh button.
function M.refresh(plugins_data_dir, builder_dir)
    M.scan(plugins_data_dir)
    M.load_status(builder_dir)
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
