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

-- ── v3.11: animation_add SJSON alias injection ─────────────────────────
-- For every alias declared by an animation_add mod, append an Animation
-- entry to the character's per-character SJSON.  H2M's
-- rom.data.on_sjson_read_as_string lets us rewrite the file contents
-- before the engine parses them; the hook fires inside FileStreamRead
-- and supports up to 8x the original size.

--- Build the SJSON text for one alias.  Mirrors stock entries:
--     {
--         Name = "..."
--         InheritFrom = "..."
--         GrannyAnimation = "..."
--         Loop = true
--         ChainTo = "..."
--     }
-- v3.15: emit a full SJSON Animation entry for `alias`.  See
-- mod_state.lua's alias parser for the field list / default rules:
--   * `nil` flags / numeric fields are NOT emitted.
--   * `false` flags ARE emitted explicitly (some fields like
--     Enable3DShadow have polarising stock values).
--   * Numeric fields are emitted verbatim; floats round-trip via tostring.
--   * `blend_in_frames > 0` produces a Blends array with a single
--     entry targeting BlendTransitionFromAll.
local function build_alias_sjson(alias)
    local lines = { "{" }
    -- Always-present basics
    table.insert(lines, '    Name = "' .. alias.logical_name .. '"')
    if alias.inherit_from and alias.inherit_from ~= "" then
        table.insert(lines, '    InheritFrom = "' .. alias.inherit_from .. '"')
    end
    table.insert(lines, '    GrannyAnimation = "' .. alias.granny_name .. '"')

    -- Loop is binary in stock data (presence = true); only emit when on.
    if alias.loop then
        table.insert(lines, '    Loop = true')
    end

    -- ChainTo string field; "" / nil = absent.
    if alias.chain_to and alias.chain_to ~= "" then
        table.insert(lines, '    ChainTo = "' .. alias.chain_to .. '"')
    end

    -- Playback / transition
    if alias.speed and alias.speed ~= 1.0 then
        table.insert(lines, '    GrannyAnimationSpeed = ' .. tostring(alias.speed))
    end
    if alias.blend_in_frames and alias.blend_in_frames > 0 then
        table.insert(lines, '    Blends =')
        table.insert(lines, '    [')
        table.insert(lines, '        {')
        table.insert(lines, '            BlendTransitionFrom = "BlendTransitionFromAll"')
        table.insert(lines, '            Duration = ' .. tostring(alias.blend_in_frames))
        table.insert(lines, '        }')
        table.insert(lines, '    ]')
    end

    -- Gameplay flags — stock convention: presence = true, absence = false.
    -- Emit only when explicitly true; nil / false stays absent.
    local function _flag(field, sjson_name)
        if alias[field] then
            table.insert(lines, '    ' .. sjson_name .. ' = true')
        end
    end
    _flag('cancel_on_owner_move', 'CancelOnOwnerMove')
    _flag('hold_last_frame', 'HoldLastFrame')
    _flag('allow_restart', 'AllowRestart')
    _flag('owner_invulnerable', 'OwnerInvulnerable')
    _flag('owner_immobile', 'OwnerImmobile')
    _flag('owner_has_no_collision', 'OwnerHasNoCollision')
    _flag('owner_untargetable', 'OwnerUntargetable')
    _flag('disable_owner_manual_interact', 'DisableOwnerManualInteract')

    -- Enable3DShadow is the only polarising flag — both true and false
    -- show up in stock data (~63%/37%) and the engine's default differs
    -- per character.  Emit explicitly when set either way; nil = inherit.
    if alias.enable_3d_shadow ~= nil then
        table.insert(lines, '    Enable3DShadow = ' ..
            (alias.enable_3d_shadow and 'true' or 'false'))
    end

    -- Numeric overrides; nil = inherit.
    if alias.scale ~= nil then
        table.insert(lines, '    Scale = ' .. tostring(alias.scale))
    end
    if alias.native_move_speed ~= nil then
        table.insert(lines, '    NativeMoveSpeed = ' .. tostring(alias.native_move_speed))
    end

    table.insert(lines, "}")
    return table.concat(lines, "\n")
end

--- Inject a list of synthesized Animation entries into a `{ Animations = [
-- ... ] }` SJSON file.  Strategy: the file ends `] }` (close-of-array,
-- close-of-table); we find the last `]` and splice our entries in just
-- before it.  Robust against trailing whitespace.
local function inject_animation_entries(content, entries_sjson)
    local last_bracket = nil
    local pos = 1
    while true do
        local i = string.find(content, "]", pos, true)
        if not i then break end
        last_bracket = i
        pos = i + 1
    end
    if not last_bracket then
        return content  -- malformed; leave alone
    end
    return string.sub(content, 1, last_bracket - 1)
        .. "\n\n" .. entries_sjson .. "\n\n"
        .. string.sub(content, last_bracket)
end

--- Read state.build_status[*].alias_animations and register one
-- on_sjson_read_as_string callback per (character, sjson_basename)
-- pair that injects all matching aliases.  Idempotent: multiple
-- aliases targeting the same SJSON share a single callback.
--- Register a SINGLE global SJSON callback that injects alias entries
-- on demand.  Must be called EARLY in plugin init — before
-- runtime.apply runs the builder — so the callback exists when the
-- engine first reads animation SJSON at startup.  The callback
-- captures `state` by reference, so when the builder later populates
-- state.build_status, subsequent SJSON reads see the data.
--
-- Idempotent: safe to call multiple times; only registers once per
-- plugin lifetime via the M.__alias_callback_registered flag.
function M.register_animation_aliases(state)
    if M.__alias_callback_registered then
        return  -- already wired up
    end
    if not (rom.data and rom.data.on_sjson_read_as_string) then
        rom.log.warning(LOG_PREFIX
            .. " rom.data.on_sjson_read_as_string missing — "
            .. "animation_add aliases cannot be registered")
        return
    end

    rom.data.on_sjson_read_as_string(function(file_path, content)
        -- Collect aliases targeting this file from the LIVE state
        -- (closure captures state by reference; build_status is
        -- populated by load_status after the builder finishes).
        --
        -- IMPORTANT: H2M's hook treats ANY string return as
        -- "modification happened" and rewrites the engine's read
        -- buffer with our string (data.cpp:354-365).  That round-trip
        -- through Lua perturbs other mods' SJSON-driven init even
        -- when the content is byte-identical.  Return nil unless we
        -- actually have aliases to inject — that lets H2M's
        -- any_modif_happened flag stay false and skips the rewrite.
        if not state or not state.build_status then
            return  -- nil → no modification
        end
        local matching = {}
        for _, rec in pairs(state.build_status) do
            for _, alias in ipairs(rec.alias_animations or {}) do
                if alias.sjson and alias.logical_name and alias.granny_name
                    and string.find(file_path, alias.sjson, 1, true) then
                    table.insert(matching, alias)
                end
            end
        end
        if #matching == 0 then
            return  -- nil → no modification
        end

        local parts = {}
        for _, alias in ipairs(matching) do
            table.insert(parts, build_alias_sjson(alias))
        end
        local injected = table.concat(parts, "\n\n")
        local new_content = inject_animation_entries(content, injected)
        if new_content == content then
            return  -- nothing actually changed
        end
        rom.log.info(LOG_PREFIX .. " injected "
            .. tostring(#matching) .. " alias(es) into "
            .. tostring(file_path)
            .. " (size " .. #content .. " -> " .. #new_content .. ")")
        return new_content
    end)  -- no path filter; we filter inside the callback by basename

    M.__alias_callback_registered = true
    rom.log.info(LOG_PREFIX .. " global SJSON-alias callback registered "
        .. "(injection deferred to FileStreamRead time)")
end

--- Trigger a re-read of the engine's game data so the alias
-- callback registered earlier gets a chance to fire AFTER the
-- builder has produced status.json.  No-op if the H2M binding isn't
-- available (older H2M).  Safe to call from main.lua right after
-- runtime.apply + mod_state.load_status.
function M.reload_game_data_for_aliases(state)
    -- Count aliases for the log message.
    local total = 0
    for _, rec in pairs(state.build_status or {}) do
        total = total + (rec.alias_animations and #rec.alias_animations or 0)
    end
    if total == 0 then
        return 0  -- nothing to reload for
    end
    if rom.data and rom.data.reload_game_data then
        rom.data.reload_game_data()
        rom.log.info(LOG_PREFIX .. " reload_game_data() — engine should "
            .. "re-read SJSON; callback will inject " .. total .. " alias(es)")
    else
        rom.log.warning(LOG_PREFIX .. " rom.data.reload_game_data missing — "
            .. "aliases registered but won't be applied until next game launch")
    end
    return total
end

--- Public helper — modders call this to play an animation by alias name.
-- Exposed under rom.mods["Enderclem-CG3HBuilder"].play_animation.
function M.play_animation(target_id, anim_name)
    if not (target_id and anim_name) then return false end
    local set_anim = rom.game and rom.game.SetAnimation
    if not set_anim then return false end
    local ok, err = pcall(set_anim, {
        Name = anim_name,
        DestinationId = target_id,
    })
    if not ok then
        rom.log.warning(LOG_PREFIX .. " play_animation('"
            .. tostring(anim_name) .. "') raised: " .. tostring(err))
        return false
    end
    return true
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
    -- v3.12 B: register every enabled mod's textures unconditionally.
    -- Skin selection no longer gates registration — every skin's
    -- unique-named textures land in mLoadedTexture2DHash so
    -- SetThingProperty(GrannyTexture=<unique_name>) can swap between
    -- them at runtime without re-loading anything.  Stock revert =
    -- SetThingProperty(GrannyTexture="").
    local registered = {}
    for _, mod in ipairs(state.mods) do
        if mod.has_textures then
            M.load_textures(mod)
            registered[mod.id] = true
        end
    end

    M.run_builder(state, ctx.builder_path, ctx.plugins_data_dir)
    M.register_gpks(state, ctx.builder_data_dir)

    -- Second pass: re-scan to catch any mod whose mod.json got rewritten
    -- by the builder (folder-mirror texture_replace mods are first
    -- detected at build time).  Only register those NOT registered in
    -- the first pass to avoid duplicate biome-override callbacks.
    state.scan(ctx.plugins_data_dir)
    for _, mod in ipairs(state.mods) do
        if not registered[mod.id] and mod.has_textures then
            M.load_textures(mod)
            registered[mod.id] = true
        end
    end
end

-- v3.12 B: live skin swap via SetThingProperty.
--
-- The MelSkin pattern: each skin's atlas ships under a UNIQUE name
-- (built into the .pkg as `<mod_id>_<basename>`), all skin pkgs are
-- LoadPackaged at startup so their entries live in mLoadedTexture2DHash,
-- and SetThingProperty(GrannyTexture=<unique_name>) on the live unit
-- tells the engine which atlas to use for rendering.  Empty string
-- reverts the engine to the model's default (stock) texture.
--
-- Player target = CurrentRun.Hero.ObjectId.  NPC target = whatever
-- GetClosestUnitOfType returns for the character name (or its
-- NPC_<char>_01 fallback).  We auto-re-apply on scene transitions
-- via rom.on_import.post("RoomLogic.lua") so a freshly-spawned NPC
-- gets its skin without the user re-clicking the picker.

local _PLAYER_CHARS = { Melinoe = true, YoungMel = true }

--- Find a live ObjectId to target for `character`.  Returns nil if
-- the character isn't in the current scene.  Probes:
--   1. Hero (player characters only).
--   2. GetClosestUnitOfType with the character name as DestinationName.
--   3. Same with `NPC_<character>_01` fallback for hub NPCs.
local function _live_target_for(character)
    local g = rom.game
    if not g then return nil end
    if _PLAYER_CHARS[character] then
        if g.CurrentRun and g.CurrentRun.Hero then
            return g.CurrentRun.Hero.ObjectId
        end
        return nil
    end
    if not (g.GetClosestUnitOfType and g.CurrentRun
            and g.CurrentRun.Hero) then
        return nil
    end
    for _, dest in ipairs({ character, "NPC_" .. character .. "_01" }) do
        local id = g.GetClosestUnitOfType({
            Id = g.CurrentRun.Hero.ObjectId,
            DestinationName = dest,
        })
        if id then return id end
    end
    return nil
end

--- Apply the persisted skin selection (if any) to `character`'s live
-- unit.  Returns:
--   "live"         — applied successfully.
--   "transition"   — character not in scene; will retry on scene change.
--   "error"        — H2M API missing or SetThingProperty raised.
function M.apply_skin_for_character(state, character)
    if not (rom.game and rom.game.SetThingProperty) then return "error" end
    local target_id = _live_target_for(character)
    if not target_id then return "transition" end

    local active = state.active_skins[character]
    local granny_texture = ""
    if active and active ~= "" and active ~= "stock" then
        local rec = state.build_status[character]
        local skin = rec and rec.skins and rec.skins[active]
        granny_texture = (skin and skin.granny_texture) or ""
    end
    local ok, err = pcall(rom.game.SetThingProperty, {
        Property = "GrannyTexture",
        Value = granny_texture,
        DestinationId = target_id,
    })
    if not ok then
        rom.log.warning(LOG_PREFIX .. " apply_skin_for_character('"
            .. tostring(character) .. "') raised: " .. tostring(err))
        return "error"
    end
    return "live"
end

--- Re-apply every persisted skin for every character.  Called on
-- first frame and on scene transitions.  Skins for characters not
-- currently in the scene are silently no-op (will retry next call).
function M.apply_all_skins(state)
    if not state.active_skins then return end
    for character, _ in pairs(state.active_skins) do
        M.apply_skin_for_character(state, character)
    end
    -- Also re-apply explicit "stock" entries so a recent revert sticks
    -- after a scene change (the iteration above only sees mod-id
    -- entries; cleared selections don't appear in active_skins).
    -- Player chars always benefit from a no-op stock apply.
    for char, _ in pairs(_PLAYER_CHARS) do
        if not state.active_skins[char] then
            M.apply_skin_for_character(state, char)
        end
    end
end

--- Hook the scene-load lifecycle so freshly-spawned units pick up
-- their persisted skin without user intervention, AND queue a
-- variant re-apply for the next render frame to refresh accessory
-- bone bindings.  Idempotent; main.lua calls this once at init.
function M.install_skin_scene_hook(state)
    if not (rom.on_import and rom.on_import.post) then return end
    rom.on_import.post(function(script_name)
        if script_name == "RoomLogic.lua"
                or script_name == "RoomManager.lua" then
            M.apply_all_skins(state)
            -- v3.13: queue the variant-swap cascade for the next
            -- render frame.  The cascade (draw_populate_entry_textures
            -- + draw_swap_to_variant) is what actually fixes the
            -- bind-pose lag on freshly-loaded mesh_add accessories
            -- (proven by in-game logs: opening the GUI fires it via
            -- the UI's first-frame callback and snaps accessories
            -- into place).  But calling it directly from on_import.post
            -- crashed the engine in ProcessAtlasMap — script-import
            -- callbacks fire on a loader thread, and the draw-state
            -- mutations need to happen on the render thread.  The
            -- always-draw imgui tick drains the flag safely.
            _variant_reapply_pending = true
        end
    end)
end

-- ── v3.13: deferred variant re-apply for bind-pose lag ──────────────
--
-- See install_skin_scene_hook above for the why.  The flag is set
-- by the script-import callback and drained by the always-draw
-- imgui tick.  Per-frame cost when the flag is false is a single
-- bool check, so the tick is cheap to leave installed always.

_variant_reapply_pending = false

function M.tick_pending_variant_reapply(state)
    if not _variant_reapply_pending then return end
    _variant_reapply_pending = false
    M.apply_active_variants(state)
end

function M.install_variant_reapply_tick(state)
    if not (rom.gui and rom.gui.add_always_draw_imgui) then return end
    rom.gui.add_always_draw_imgui(function()
        M.tick_pending_variant_reapply(state)
    end)
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
    if not target_mod then
        rom.log.info(LOG_PREFIX .. " [draw-gate] mod not found: " .. mod_id)
        return nil
    end

    -- animation_patch / animation_add mods don't ship meshes — there's
    -- nothing for the draw-gate to flip.  Their content is baked into
    -- the merged GPK + SJSON aliases at build time, so the toggle DOES
    -- take effect, just on next game launch (CG3HBuilder reruns the
    -- builder and respects mod_state).  Surface that as a "restart"
    -- outcome so the UI shows the right message instead of treating it
    -- as a failure.
    if #target_mod.mesh_entries == 0 then
        rom.log.info(LOG_PREFIX .. " [draw-gate] no mesh entries for "
            .. mod_id .. " (animation-only mod) — rebuild on next launch")
        return "restart"
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
