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

M.mods = {}            -- array of { id, name, version, author, character, path, has_textures, has_glb, glb_name, mesh_entries, has_mesh_add, has_mesh_replace }
M.by_character = {}    -- { [character] = { mod, ... } }
M.build_status = {}    -- { [character] = { state, gpk_path, error, duration_ms, mod_details } }
M.mod_state = {}       -- { [mod_id] = { enabled = bool } }   — user-writable
M.char_mesh_entries = {}  -- { [character] = { "HecateBattle_Mesh", "HecateHub_Mesh", ... } }
                          -- Union of mesh_entries from all installed mods for this character.
                          -- sgg::Granny3D::mModelData is keyed by MESH ENTRY name, not by
                          -- character name.  Multi-entry characters (Hecate Battle/Hub,
                          -- Melinoe Hat/Body/Overlook) have multiple entries each.
M.builder_version = "" -- from cg3h_status.json
M.built_at = ""        -- ISO timestamp from cg3h_status.json
M.game_dir = ""        -- from cg3h_status.json (forward-slashed)
M.summary = { built = 0, cached = 0, failed = 0 }
M.last_scan_ts = 0
M.variants = {}        -- v3.9: populated by runtime.register_variants.
                       -- Shape: { [character] = { [entry_name] = { stock, variants = { [mod_id] = variant_entry_name } } } }

M.active_variants = {} -- v3.9: per-entry active body selection.
                       -- Shape: { [character] = { [entry_name] = mod_id } }
                       -- Missing / "stock" → stock render for that entry.

M.active_skins = {}    -- v3.12: per-character active texture-replace mod.
                       -- Shape: { [character] = mod_id }
                       -- Missing / "stock" → no exclusive skin (every
                       -- enabled texture_replace mod's pkg gets registered,
                       -- last-wins per PKG path — v3.11 baseline behavior).

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

-- v3.11 — parse the target.new_animations array, shape:
--   [ { "logical_name": "...", "granny_name": "...",
--       "clone_from": "...", "source_glb_action": "...",
--       "loop": true, ... }, ... ]
-- Returns a list of { logical_name, granny_name, loop } dicts.  Used by
-- the Animations tab so each mod can be tested directly without going
-- through cg3h_status.json (handy for hand-authored mods that haven't
-- been built yet).
local function _parse_new_animations(content)
    local result = {}
    local target_block = content:match('"target"%s*:%s*(%b{})')
    if not target_block then return result end
    local arr = target_block:match('"new_animations"%s*:%s*(%b[])')
    if not arr then return result end
    for entry_body in arr:gmatch('(%b{})') do
        local logical = entry_body:match('"logical_name"%s*:%s*"([^"]*)"')
        local granny = entry_body:match('"granny_name"%s*:%s*"([^"]*)"')
        local loop_v = entry_body:match('"loop"%s*:%s*(%a+)')
        if logical and granny then
            table.insert(result, {
                logical_name = logical,
                granny_name = granny,
                loop = (loop_v == "true"),
            })
        end
    end
    return result
end

-- Parse the target.new_mesh_routing object, shape:
--     { "TorusHubMesh": ["HecateHub_Mesh"], "TorusBattleMesh": [...] }
-- Returns { [mesh_name] = { entry1, entry2, ... } }.  Each mesh_name maps
-- to the list of stock mesh entries the mod's added mesh gets merged
-- into at build time.  Runtime uses this for per-mesh visibility toggle.
local function _parse_new_mesh_routing(content)
    local result = {}
    -- Find target's balanced object, then within it the new_mesh_routing
    -- balanced object.  The balanced-match (%b{}) handles nested braces.
    local target_block = content:match('"target"%s*:%s*(%b{})')
    if not target_block then return result end
    local routing_block = target_block:match('"new_mesh_routing"%s*:%s*(%b{})')
    if not routing_block then return result end
    -- For each `"mesh_name": [ ... ]` pair inside routing_block.
    for mesh_name, entries_arr in routing_block:gmatch('"([^"]+)"%s*:%s*(%b[])') do
        local entries = {}
        for v in entries_arr:gmatch('"([^"]*)"') do
            table.insert(entries, v)
        end
        if #entries > 0 then
            result[mesh_name] = entries
        end
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
                        -- Scope the type lookup to the "type" JSON field
                        -- so a "mesh_add" substring elsewhere (e.g. in a
                        -- description) doesn't mis-flag the mod.
                        local type_block = content:match('"type"%s*:%s*(%b[])')
                                        or content:match('"type"%s*:%s*"[^"]*"')
                                        or ""
                        local mod = {
                            id               = dir_name,
                            name             = _field(content, "name") or "",
                            version          = _field(content, "version") or "",
                            author           = _field(content, "author") or "",
                            character        = _field(content, "character") or "",
                            path             = mod_data_dir,
                            has_textures     = content:find('"textures"') ~= nil,
                            has_glb          = content:find('"glb"') ~= nil,
                            glb_name         = _field(content, "glb"),
                            mesh_entries     = _string_array(content, "mesh_entries"),
                            has_mesh_add     = type_block:find('"mesh_add"') ~= nil,
                            has_mesh_replace = type_block:find('"mesh_replace"') ~= nil,
                            has_animation_patch = type_block:find('"animation_patch"') ~= nil,
                            has_animation_add   = type_block:find('"animation_add"') ~= nil,
                            -- v3.11 — patched stock animations + brand-new aliases
                            patched_animations = _string_array(content, "animations"),
                            new_animations   = _parse_new_animations(content),
                            new_mesh_routing = _parse_new_mesh_routing(content),
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

        -- v3.9: per-character "variants" block shape =
        --   { [stock_entry] = { [mod_id] = variant_entry_name } }
        -- Emitted by the builder when mesh_replace mods declare
        -- target.mesh_entries.
        local variants = {}
        local v_block = body:match('"variants"%s*:%s*(%b{})')
        if v_block then
            for entry, entry_body in v_block:gmatch('"([^"]+)"%s*:%s*(%b{})') do
                variants[entry] = {}
                for mod_id, vname in entry_body:gmatch('"([^"]+)"%s*:%s*"([^"]*)"') do
                    variants[entry][mod_id] = vname
                end
            end
        end

        -- v3.15: alias_animations.  Each element carries the full
        -- SJSON Animation field set.  Parser extracts everything the
        -- runtime SJSON injector emits — see runtime.lua's
        -- `build_alias_sjson` for the field list.  blends array is
        -- still parsed-as-list (unused by SJSON injector today, kept
        -- for round-trip; v3.15.x will wire source-specific blends).
        local aliases = {}
        local aa_block = body:match('"alias_animations"%s*:%s*(%b[])')
        if aa_block then
            for entry_body in aa_block:gmatch('(%b{})') do
                local function s(field)
                    return entry_body:match('"' .. field .. '"%s*:%s*"([^"]*)"')
                end
                local function b(field)
                    -- Tristate: nil (key absent), true, false.  Used for
                    -- "absent vs explicitly false" gameplay flags.
                    local v = entry_body:match('"' .. field .. '"%s*:%s*(%a+)')
                    if v == "true" then return true end
                    if v == "false" then return false end
                    return nil
                end
                local function n(field)
                    -- Numeric, nil if missing.  Accepts int or float.
                    local v = entry_body:match('"' .. field .. '"%s*:%s*(%-?[%d%.]+)')
                    if v then return tonumber(v) end
                    return nil
                end
                -- v3.15: source-specific Blends array.  Each element
                -- looks like {"from":"...", "duration":N} (snake_case
                -- to match the rest of the schema).  Parsed into a
                -- Lua list so build_alias_sjson can emit one
                -- `BlendTransitionFrom = X / Duration = Y` row per
                -- predecessor.  Empty array = no Blends emitted.
                local blends = {}
                local blends_arr = entry_body:match('"blends"%s*:%s*(%b[])')
                if blends_arr then
                    for elem in blends_arr:gmatch('(%b{})') do
                        local fr = elem:match('"from"%s*:%s*"([^"]*)"')
                        local du = elem:match('"duration"%s*:%s*(%-?[%d%.]+)')
                        if fr and du then
                            blends[#blends + 1] = {
                                from = fr,
                                duration = tonumber(du),
                            }
                        end
                    end
                end

                local logical = s("logical_name")
                local granny = s("granny_name")
                if logical and granny then
                    aliases[#aliases + 1] = {
                        mod_id = s("mod_id"),
                        logical_name = logical,
                        granny_name = granny,
                        sjson = s("sjson"),
                        -- v3.11 basics
                        loop = b("loop"),
                        inherit_from = s("inherit_from"),
                        chain_to = s("chain_to"),
                        -- v3.15: transition / playback
                        speed = n("speed"),
                        blend_in_frames = n("blend_in_frames"),
                        blends = blends,
                        -- v3.15: gameplay flags
                        cancel_on_owner_move = b("cancel_on_owner_move"),
                        hold_last_frame = b("hold_last_frame"),
                        allow_restart = b("allow_restart"),
                        owner_invulnerable = b("owner_invulnerable"),
                        owner_immobile = b("owner_immobile"),
                        owner_has_no_collision = b("owner_has_no_collision"),
                        owner_untargetable = b("owner_untargetable"),
                        disable_owner_manual_interact = b("disable_owner_manual_interact"),
                        -- v3.15: polarising / overrides
                        enable_3d_shadow = b("enable_3d_shadow"),
                        scale = n("scale"),
                        native_move_speed = n("native_move_speed"),
                    }
                end
            end
        end

        -- v3.12: per-character "skins" block shape =
        --   { [mod_id] = { name, version, pkg_entries: [...], preview } }
        -- Emitted by the builder for every texture_replace mod.
        local skins = {}
        local s_block = body:match('"skins"%s*:%s*(%b{})')
        if s_block then
            for mod_id, mod_body in s_block:gmatch('"([^"]+)"%s*:%s*(%b{})') do
                local entry = {
                    name = mod_body:match('"name"%s*:%s*"([^"]*)"') or mod_id,
                    version = mod_body:match('"version"%s*:%s*"([^"]*)"') or "",
                    preview = mod_body:match('"preview"%s*:%s*"([^"]*)"'),
                    -- v3.12 B: SetThingProperty target for live swap.
                    granny_texture = mod_body:match(
                        '"granny_texture"%s*:%s*"([^"]*)"'),
                    pkg_entries = {},
                }
                local pe_arr = mod_body:match('"pkg_entries"%s*:%s*(%b[])')
                if pe_arr then
                    for v in pe_arr:gmatch('"([^"]*)"') do
                        table.insert(entry.pkg_entries, v)
                    end
                end
                skins[mod_id] = entry
            end
        end

        M.build_status[char] = {
            state = state,
            gpk_path = gpk_path,
            error = err,
            duration_ms = duration,
            variants = variants,
            alias_animations = aliases,
            skins = skins,
        }
    end
end

-- ── Mod enable/disable state (v3.8) ───────────────────────────────────
-- Stored in cg3h_mod_state.json next to the cache keys.  Separate from
-- cg3h_status.json because that file is builder output; this one is
-- user input (written by the UI on checkbox toggle).

--- Read cg3h_mod_state.json into M.mod_state and M.active_variants.
-- Missing → empty.
function M.load_mod_state(builder_dir)
    M.mod_state = {}
    M.active_variants = {}
    M.active_skins = {}
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

    -- "mods": { "mod_id": { "enabled": true/false }, ... }
    local mods_block = content:match('"mods"%s*:%s*(%b{})')
    if mods_block then
        for mod_id, body in mods_block:gmatch('"([^"]+)"%s*:%s*(%b{})') do
            local enabled_str = body:match('"enabled"%s*:%s*(%a+)')
            local enabled = true
            if enabled_str == "false" then
                enabled = false
            end
            M.mod_state[mod_id] = { enabled = enabled }
        end
    end

    -- v3.9: "active_variants": { "Character": { "EntryName": "mod_id", ... }, ... }
    local av_block = content:match('"active_variants"%s*:%s*(%b{})')
    if av_block then
        for char, char_body in av_block:gmatch('"([^"]+)"%s*:%s*(%b{})') do
            M.active_variants[char] = {}
            for entry, mod_id in char_body:gmatch('"([^"]+)"%s*:%s*"([^"]*)"') do
                M.active_variants[char][entry] = mod_id
            end
        end
    end

    -- v3.12: "active_skins": { "Character": "mod_id", ... }
    local as_block = content:match('"active_skins"%s*:%s*(%b{})')
    if as_block then
        for char, mod_id in as_block:gmatch('"([^"]+)"%s*:%s*"([^"]*)"') do
            if mod_id and mod_id ~= "" then
                M.active_skins[char] = mod_id
            end
        end
    end
end

--- Write M.mod_state + M.active_variants back to cg3h_mod_state.json.
-- Best-effort — logs a warning on failure but does not error.
-- Hand-serialised because we don't ship a JSON encoder in the plugin.
function M.save_mod_state(builder_dir)
    local path = rom.path.combine(builder_dir, "cg3h_mod_state.json")
    local lines = {}
    table.insert(lines, '{')
    table.insert(lines, '  "version": 2,')

    -- mods block — stable sort by id.
    table.insert(lines, '  "mods": {')
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
    table.insert(lines, '  },')

    -- v3.9: active_variants block — per-entry body selection.
    table.insert(lines, '  "active_variants": {')
    local chars = {}
    for char, _ in pairs(M.active_variants) do
        table.insert(chars, char)
    end
    table.sort(chars)
    for ci, char in ipairs(chars) do
        local per_entry = M.active_variants[char] or {}
        local entries = {}
        for entry, mod_id in pairs(per_entry) do
            -- Only persist non-stock picks — stock is the default.
            if mod_id and mod_id ~= "stock" then
                table.insert(entries, { entry = entry, mod = mod_id })
            end
        end
        table.sort(entries, function(a, b) return a.entry < b.entry end)
        if #entries == 0 then goto next_char end
        table.insert(lines, string.format('    "%s": {', char))
        for i, e in ipairs(entries) do
            local comma = (i < #entries) and "," or ""
            table.insert(lines, string.format(
                '      "%s": "%s"%s', e.entry, e.mod, comma))
        end
        local char_comma = (ci < #chars) and "}," or "}"
        table.insert(lines, '    ' .. char_comma)
        ::next_char::
    end
    table.insert(lines, '  },')

    -- v3.12: active_skins block — per-character exclusive skin picker.
    -- Only persist non-empty entries (default = no exclusive skin).
    table.insert(lines, '  "active_skins": {')
    local skin_chars = {}
    for char, mod_id in pairs(M.active_skins) do
        if mod_id and mod_id ~= "" and mod_id ~= "stock" then
            table.insert(skin_chars, char)
        end
    end
    table.sort(skin_chars)
    for i, char in ipairs(skin_chars) do
        local comma = (i < #skin_chars) and "," or ""
        table.insert(lines, string.format(
            '    "%s": "%s"%s', char, M.active_skins[char], comma))
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

-- ── v3.9: per-entry active variant accessors ────────────────────────────

--- Read the active variant mod_id for a given entry.  Returns "stock" if
-- no mod is selected.  Never returns nil so callers can treat the result
-- as a dropdown selection directly.
function M.get_active_variant(character, entry_name)
    local per = M.active_variants[character]
    if not per then return "stock" end
    local id = per[entry_name]
    if not id or id == "" then return "stock" end
    return id
end

--- Set the active variant for a single entry.  Pass "stock" (or nil) to
-- clear the selection.  Persists to cg3h_mod_state.json.
function M.set_active_variant(character, entry_name, mod_id, builder_dir)
    if not M.active_variants[character] then
        M.active_variants[character] = {}
    end
    if mod_id == "stock" or mod_id == nil or mod_id == "" then
        M.active_variants[character][entry_name] = nil
    else
        M.active_variants[character][entry_name] = mod_id
    end
    M.save_mod_state(builder_dir)
end

-- ── v3.12: per-character active skin accessors ───────────────────────

--- Read the active skin mod_id for a character.  Returns "stock" when
-- no exclusive skin is selected (= every enabled texture_replace mod
-- gets registered, last-wins).  Never returns nil.
function M.get_active_skin(character)
    local id = M.active_skins[character]
    if not id or id == "" then return "stock" end
    return id
end

--- Set the active skin for a character.  Pass "stock" (or nil) to
-- clear the selection and revert to the v3.11 stack-everything
-- behavior.  Persists to cg3h_mod_state.json.  Takes effect on next
-- launch — texture overrides are registered at plugin init.
function M.set_active_skin(character, mod_id, builder_dir)
    if mod_id == "stock" or mod_id == nil or mod_id == "" then
        M.active_skins[character] = nil
    else
        M.active_skins[character] = mod_id
    end
    M.save_mod_state(builder_dir)
end

--- Subset of mods that provide a variant for EVERY entry in the
-- character's variants table.  Used for the "Apply to all scenes"
-- dropdown — only candidates that cover every scene should appear.
function M.mods_covering_all_entries(character)
    local char_vars = M.variants[character]
    if not char_vars then return {} end
    local entry_names = {}
    for name, _ in pairs(char_vars) do table.insert(entry_names, name) end
    if #entry_names == 0 then return {} end

    local candidate = {}  -- mod_id → count of entries it covers
    for _, entry_data in pairs(char_vars) do
        for mod_id, _ in pairs(entry_data.variants) do
            candidate[mod_id] = (candidate[mod_id] or 0) + 1
        end
    end
    local full = {}
    for mod_id, n in pairs(candidate) do
        if n == #entry_names then
            table.insert(full, mod_id)
        end
    end
    table.sort(full)
    return full
end

return M
