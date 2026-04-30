-- CG3H in-game UI — ImGui mod manager.
--
-- Swap this file to change the look.  It reads mod_state (data) and the
-- context table (for paths + refresh/toggle/rebuild callbacks).  It never
-- mutates game state directly — every action goes through the context's
-- callbacks so this file stays decoupled from runtime.lua.

local M = {}

local ImGui = rom.ImGui
local window_open = false

-- v3.8: transient banner shown after a rebuild + hot-reload action.
-- { kind = "live"|"transition"|"restart"|"error", text = "..." }
local banner = nil

-- Status → (icon, color).  Kept tiny so adding new states later is a
-- one-line change.
local STATE_DECOR = {
    built    = { icon = "[OK]",    r = 0.35, g = 0.85, b = 0.35 },
    cached   = { icon = "[CACHE]", r = 0.55, g = 0.75, b = 1.00 },
    failed   = { icon = "[FAIL]",  r = 1.00, g = 0.40, b = 0.40 },
    disabled = { icon = "[OFF]",   r = 0.55, g = 0.55, b = 0.55 },
    unknown  = { icon = "[?]",     r = 0.70, g = 0.70, b = 0.70 },
}

local function _decor(state)
    return STATE_DECOR[state] or STATE_DECOR.unknown
end

local function _colored_state(state)
    local d = _decor(state)
    ImGui.TextColored(d.r, d.g, d.b, 1.0, d.icon .. " " .. state)
end

--- Invoke a context callback that returns a toggle outcome string, then
-- turn that outcome into a transient banner the window shows until the
-- next refresh.
local function _run_and_banner(cb, label)
    if not cb then return end
    local outcome = cb()
    if outcome == "live" then
        banner = { kind = "live",
                   text = label .. ": toggled." }
    elseif outcome == "transition" then
        banner = { kind = "transition",
                   text = label .. ": changes apply on next area transition." }
    elseif outcome == "restart" then
        banner = { kind = "transition",
                   text = label .. ": animation mods rebuild on next game launch." }
    elseif outcome == "error" or outcome == nil then
        banner = { kind = "error",
                   text = label .. ": toggle failed (see console)." }
    else
        banner = { kind = "error",
                   text = label .. ": unknown outcome (" .. tostring(outcome) .. ")" }
    end
end

-- ── Tab: Characters ────────────────────────────────────────────────────

local function _draw_characters_tab(state, ctx)
    ImGui.TextDisabled("Per-character build status. Click a header to expand.")
    ImGui.Separator()

    local chars = {}
    for char, _ in pairs(state.by_character) do
        table.insert(chars, char)
    end
    -- Also include characters that have ONLY disabled mods — they appear
    -- in build_status with state="disabled" but not in by_character.
    for char, rec in pairs(state.build_status) do
        if rec.state == "disabled" and not state.by_character[char] then
            table.insert(chars, char)
        end
    end
    table.sort(chars)

    if #chars == 0 then
        ImGui.TextDisabled("No CG3H mods installed.")
        return
    end

    for _, char in ipairs(chars) do
        local mods = state.by_character[char] or {}
        -- Gather mods regardless of enabled state by cross-referencing
        -- M.mods (which always holds every installed mod for this char).
        local all_mods = {}
        for _, m in ipairs(state.mods) do
            if m.character == char then
                table.insert(all_mods, m)
            end
        end

        local rec = state.build_status[char] or { state = "unknown" }
        local label = string.format("%s  %s  (%d mod%s)",
            _decor(rec.state).icon, char, #all_mods, #all_mods == 1 and "" or "s")
        if ImGui.CollapsingHeader(label) then
            ImGui.Indent()
            _colored_state(rec.state)

            if rec.gpk_path then
                ImGui.TextDisabled("GPK: " .. rec.gpk_path)
            end
            if rec.duration_ms and rec.duration_ms > 0 then
                ImGui.TextDisabled(string.format("Last build: %d ms", rec.duration_ms))
            end
            if rec.error then
                ImGui.TextColored(1.0, 0.4, 0.4, 1.0, "Error: " .. rec.error)
            end

            -- v3.9: per-entry body picker + "apply to all" cascade.
            -- One dropdown per scene entry (Hub, Battle, …), each listing
            -- only mods that actually ship a variant for that entry.
            local char_variants = state.variants and state.variants[char]
            if char_variants and next(char_variants) and ctx.on_set_variant_entry then
                ImGui.Spacing()
                ImGui.Text("Body:")
                ImGui.Indent()

                -- Deterministic entry ordering for a stable UI.
                local entry_names = {}
                for entry, _ in pairs(char_variants) do
                    table.insert(entry_names, entry)
                end
                table.sort(entry_names)

                -- Helper: human label for a swap-target id.  Targets are
                -- stock entry names (v3.9 ships scene-entry swapping); strip
                -- the `{Char}` prefix and `_Mesh` suffix so labels read
                -- "Hub" / "Battle" instead of "HecateHub_Mesh".
                local function _mod_label(mod_id)
                    for _, mod in ipairs(all_mods) do
                        if mod.id == mod_id then
                            return mod.name ~= "" and mod.name or mod.id
                        end
                    end
                    local short = mod_id:match("^" .. char .. "(.-)_Mesh$")
                    if short and short ~= "" then return short end
                    return mod_id
                end

                -- Per-entry dropdown.  Shows only mods with a variant for
                -- THIS entry — modders shipping partial coverage don't
                -- pollute dropdowns for scenes they didn't target.
                for _, entry_name in ipairs(entry_names) do
                    local entry_data = char_variants[entry_name]
                    local options = {}
                    for mod_id, _ in pairs(entry_data.variants) do
                        table.insert(options, mod_id)
                    end
                    table.sort(options, function(a, b)
                        if a == "stock" then return true end
                        if b == "stock" then return false end
                        return a < b
                    end)

                    local active = state.get_active_variant(char, entry_name)
                    if not active or active == "" then active = "stock" end
                    local preview = (active == "stock") and "Stock" or _mod_label(active)

                    -- Derive a short scene label from the entry name.
                    -- "HecateHub_Mesh" → "Hub", "HecateBattle_Mesh" → "Battle".
                    -- Falls back to the full entry name if no pattern match.
                    local scene = entry_name:match("^" .. char .. "(.-)_Mesh$")
                    if not scene or scene == "" then scene = entry_name end
                    ImGui.Text(scene .. ":")
                    ImGui.SameLine()

                    if ImGui.BeginCombo("##body_" .. char .. "_" .. entry_name, preview) then
                        for i, opt in ipairs(options) do
                            local selected = (opt == active)
                            local lbl = (opt == "stock") and "Stock" or _mod_label(opt)
                            if ImGui.Selectable(lbl .. "##body_" .. char .. "_"
                                    .. entry_name .. "_" .. i, selected) then
                                if opt ~= active then
                                    _run_and_banner(function()
                                        return ctx.on_set_variant_entry(char, entry_name, opt)
                                    end, char)
                                end
                            end
                        end
                        ImGui.EndCombo()
                    end
                end

                -- "Apply to all scenes" — only lists mods that cover every
                -- entry of this character.  Cascades the pick to every
                -- per-entry dropdown in one click.
                local all_coverers = state.mods_covering_all_entries(char)
                if #all_coverers > 0 and ctx.on_set_variant_all then
                    ImGui.Spacing()
                    ImGui.TextDisabled("Apply to all scenes:")
                    ImGui.SameLine()
                    if ImGui.BeginCombo("##body_all_" .. char, "pick one…") then
                        for i, mod_id in ipairs(all_coverers) do
                            local lbl = (mod_id == "stock") and "Stock"
                                        or _mod_label(mod_id)
                            if ImGui.Selectable(lbl .. "##body_all_" .. char .. "_" .. i, false) then
                                _run_and_banner(function()
                                    return ctx.on_set_variant_all(char, mod_id)
                                end, char)
                            end
                        end
                        ImGui.EndCombo()
                    end
                end
                ImGui.Unindent()
            end

            -- Accessories: mesh_add mods.  Each checkbox toggles that
            -- accessory on/off.  Toggle rebuilds the merged GPK (the
            -- draw-gate only hides whole mesh entries, not individual
            -- meshes within one — mesh-level visibility is a v4.x goal).
            local accessory_mods = {}
            for _, m in ipairs(all_mods) do
                if m.has_mesh_add then
                    table.insert(accessory_mods, m)
                end
            end
            if #accessory_mods > 0 then
                ImGui.Spacing()
                ImGui.Text("Accessories:")
                ImGui.Indent()
                for _, mod in ipairs(accessory_mods) do
                    local enabled = state.is_enabled(mod.id)
                    local new_val, clicked = ImGui.Checkbox("##acc_" .. mod.id, enabled)
                    if clicked and new_val ~= enabled then
                        _run_and_banner(function()
                            return ctx.on_toggle_mod and ctx.on_toggle_mod(mod.id, new_val)
                        end, char)
                    end
                    ImGui.SameLine()
                    local lbl = mod.name ~= "" and mod.name or mod.id
                    if enabled then
                        ImGui.Text(lbl)
                    else
                        local d = _decor("disabled")
                        ImGui.TextColored(d.r, d.g, d.b, 1.0, lbl .. " [off]")
                    end
                end
                ImGui.Unindent()
            end

            ImGui.Spacing()
            ImGui.Text("Mods:")
            for _, mod in ipairs(all_mods) do
                local enabled = state.is_enabled(mod.id)
                local new_val, clicked = ImGui.Checkbox("##en_" .. mod.id, enabled)
                if clicked and new_val ~= enabled then
                    _run_and_banner(function()
                        return ctx.on_toggle_mod and ctx.on_toggle_mod(mod.id, new_val)
                    end, char)
                end
                ImGui.SameLine()
                local label_text = string.format("%s  %s  by %s",
                    mod.name ~= "" and mod.name or mod.id,
                    mod.version ~= "" and ("v" .. mod.version) or "",
                    mod.author ~= "" and mod.author or "unknown")
                if enabled then
                    ImGui.Text(label_text)
                else
                    local d = _decor("disabled")
                    ImGui.TextColored(d.r, d.g, d.b, 1.0, label_text .. " [disabled]")
                end
            end
            ImGui.Unindent()
            ImGui.Spacing()
        end
    end
end

-- ── Tab: Mods ──────────────────────────────────────────────────────────

local function _draw_mods_tab(state)
    ImGui.TextDisabled("Flat list of all installed CG3H mods.")
    ImGui.Separator()

    if #state.mods == 0 then
        ImGui.TextDisabled("No CG3H mods installed.")
        return
    end

    if ImGui.BeginTable("cg3h_mods_table", 6) then
        ImGui.TableSetupColumn("On")
        ImGui.TableSetupColumn("Name")
        ImGui.TableSetupColumn("Author")
        ImGui.TableSetupColumn("Version")
        ImGui.TableSetupColumn("Character")
        ImGui.TableSetupColumn("Status")
        ImGui.TableHeadersRow()

        for _, mod in ipairs(state.mods) do
            local enabled = state.is_enabled(mod.id)
            local d = enabled and nil or _decor("disabled")

            ImGui.TableNextRow()
            ImGui.TableNextColumn()
            if enabled then
                ImGui.TextColored(0.35, 0.85, 0.35, 1.0, "on")
            else
                ImGui.TextColored(d.r, d.g, d.b, 1.0, "off")
            end

            local function _cell(text)
                ImGui.TableNextColumn()
                if d then
                    ImGui.TextColored(d.r, d.g, d.b, 1.0, text)
                else
                    ImGui.Text(text)
                end
            end
            _cell(mod.name ~= "" and mod.name or mod.id)
            _cell(mod.author ~= "" and mod.author or "-")
            _cell(mod.version ~= "" and mod.version or "-")
            _cell(mod.character)

            ImGui.TableNextColumn()
            local rec = state.build_status[mod.character] or { state = "unknown" }
            _colored_state(rec.state)
        end
        ImGui.EndTable()
    end
end

-- ── Tab: Animations ────────────────────────────────────────────────────
-- One CollapsingHeader per character, listing every modded animation
-- (new + patched) shipped by an enabled mod for that character.  Each
-- row has a Play button that targets the character it belongs to —
-- player for Melinoe / YoungMel, the live in-scene unit otherwise.

local _PLAYER_CHARACTERS = { Melinoe = true, YoungMel = true }

-- Find a live ObjectId to play `character`'s animation on.  Returns
-- (target_id, label) on success or (nil, reason) when the character
-- isn't currently in the scene.  The target IS the character —
-- there's no global picker.
local function _resolve_target_for(character)
    local g = rom.game
    if not g then return nil, "rom.game unavailable" end
    if _PLAYER_CHARACTERS[character] then
        if g.CurrentRun and g.CurrentRun.Hero then
            return g.CurrentRun.Hero.ObjectId, character .. " (player)"
        end
        return nil, "player not loaded yet"
    end
    if not (g.GetClosestUnitOfType and g.CurrentRun
            and g.CurrentRun.Hero) then
        return nil, "GetClosestUnitOfType unavailable"
    end
    -- Try the character name itself first, then NPC_<Char>_01 as a
    -- fallback for hub-style spawn names.  Both are derived from the
    -- mod's declared `character`, not from a hardcoded character list.
    for _, dest in ipairs({ character, "NPC_" .. character .. "_01" }) do
        local id = g.GetClosestUnitOfType({
            Id = g.CurrentRun.Hero.ObjectId,
            DestinationName = dest,
        })
        if id then return id, dest end
    end
    return nil, character .. " not in current scene"
end

local function _play_anim(ctx, character, anim_name)
    local target, label = _resolve_target_for(character)
    if not target then
        banner = { kind = "error",
                   text = "Play '" .. anim_name .. "': " .. (label or "no target") }
        return
    end
    local ok = ctx.on_play_animation
        and ctx.on_play_animation(target, anim_name)
        or false
    if ok then
        banner = { kind = "live",
                   text = "Played '" .. anim_name .. "' on " .. label }
    else
        banner = { kind = "error",
                   text = "Failed to play '" .. anim_name
                          .. "' on " .. label .. " (see log)" }
    end
end

local function _draw_animations_tab(state, ctx)
    -- Group enabled mods that ship animation work by character.
    local by_char = {}
    for _, mod in ipairs(state.mods) do
        if state.is_enabled(mod.id) then
            local n_new = mod.new_animations and #mod.new_animations or 0
            local n_patched = mod.patched_animations
                              and #mod.patched_animations or 0
            if n_new > 0 or n_patched > 0 then
                local list = by_char[mod.character]
                if not list then
                    list = {}
                    by_char[mod.character] = list
                end
                list[#list + 1] = mod
            end
        end
    end

    local chars = {}
    for c, _ in pairs(by_char) do chars[#chars + 1] = c end
    table.sort(chars)

    if #chars == 0 then
        ImGui.TextDisabled("No enabled mod ships custom or patched "
            .. "animations.  Build one with the Blender addon, "
            .. "enable it, and Refresh.")
        return
    end

    ImGui.TextDisabled("Click Play to trigger an animation on its "
        .. "character.  Player rows target Melinoe; NPC rows need "
        .. "the unit to be present in the current scene.")
    ImGui.Separator()

    for _, char in ipairs(chars) do
        local mods = by_char[char]
        local total_new, total_patched = 0, 0
        for _, mod in ipairs(mods) do
            total_new = total_new + (#(mod.new_animations or {}))
            total_patched = total_patched + (#(mod.patched_animations or {}))
        end
        local header = char
        if total_new > 0 then
            header = header .. "  +" .. total_new .. " new"
        end
        if total_patched > 0 then
            header = header .. "  ~" .. total_patched .. " patched"
        end

        if ImGui.CollapsingHeader(header) then
            ImGui.Indent()
            for _, mod in ipairs(mods) do
                local mod_label = "  — " .. (mod.name ~= "" and mod.name or mod.id)
                for i, alias in ipairs(mod.new_animations or {}) do
                    local btn = "Play##new_" .. mod.id .. "_" .. i
                    if ImGui.SmallButton(btn) then
                        _play_anim(ctx, char, alias.logical_name)
                    end
                    ImGui.SameLine()
                    local label = alias.logical_name
                    if alias.loop then label = label .. "  (loop)" end
                    ImGui.Text(label .. mod_label)
                end
                for i, anim in ipairs(mod.patched_animations or {}) do
                    local btn = "Play##patched_" .. mod.id .. "_" .. i
                    if ImGui.SmallButton(btn) then
                        _play_anim(ctx, char, anim)
                    end
                    ImGui.SameLine()
                    ImGui.Text(anim .. mod_label)
                end
            end
            ImGui.Unindent()
        end
    end
end

-- ── Tab: Summary ───────────────────────────────────────────────────────

local function _draw_summary_tab(state)
    ImGui.Text(string.format("Installed mods: %d", #state.mods))
    local char_count = 0
    for _, _ in pairs(state.by_character) do char_count = char_count + 1 end
    ImGui.Text(string.format("Characters targeted: %d", char_count))
    ImGui.Separator()

    if state.builder_version ~= "" then
        ImGui.Text("Builder version: " .. state.builder_version)
    end
    if state.built_at ~= "" then
        ImGui.Text("Last build: " .. state.built_at)
    end
    ImGui.Spacing()
    ImGui.TextColored(0.35, 0.85, 0.35, 1.0,
        string.format("Built:  %d", state.summary.built))
    ImGui.TextColored(0.55, 0.75, 1.00, 1.0,
        string.format("Cached: %d", state.summary.cached))
    ImGui.TextColored(1.00, 0.40, 0.40, 1.0,
        string.format("Failed: %d", state.summary.failed))
end

-- ── Banner helper ──────────────────────────────────────────────────────

local BANNER_COLOR = {
    live       = { 0.35, 0.85, 0.35 },
    transition = { 0.95, 0.80, 0.30 },
    error      = { 1.00, 0.40, 0.40 },
}

local function _draw_banner()
    if not banner then return end
    local c = BANNER_COLOR[banner.kind] or BANNER_COLOR.error
    ImGui.TextColored(c[1], c[2], c[3], 1.0, banner.text)
    ImGui.SameLine()
    if ImGui.SmallButton("Dismiss##banner") then
        banner = nil
    end
    ImGui.Separator()
end

-- ── Main window ────────────────────────────────────────────────────────

local function _draw_window(state, ctx)
    local new_open, should_draw = ImGui.Begin("CG3H Mod Manager", window_open)
    window_open = new_open
    if should_draw then
        if ImGui.Button("Refresh") then
            if ctx.on_refresh then ctx.on_refresh() end
            banner = nil
        end
        ImGui.SameLine()
        ImGui.TextDisabled(string.format("(%d mods)", #state.mods))
        ImGui.SameLine()
        if ImGui.Button("Pool stats") then
            if rom.data.draw_dump_pool_stats then
                local n = rom.data.draw_dump_pool_stats()
                banner = { kind = "live",
                           text = "Dumped " .. n .. " pool(s) to log" }
            end
        end

        ImGui.Separator()
        _draw_banner()

        if ImGui.BeginTabBar("cg3h_tabs") then
            if ImGui.BeginTabItem("Characters") then
                _draw_characters_tab(state, ctx)
                ImGui.EndTabItem()
            end
            if ImGui.BeginTabItem("Mods") then
                _draw_mods_tab(state)
                ImGui.EndTabItem()
            end
            if ImGui.BeginTabItem("Animations") then
                _draw_animations_tab(state, ctx)
                ImGui.EndTabItem()
            end
            if ImGui.BeginTabItem("Summary") then
                _draw_summary_tab(state)
                ImGui.EndTabItem()
            end
            ImGui.EndTabBar()
        end
    end
    ImGui.End()
end

-- ── Public API ─────────────────────────────────────────────────────────
--
-- Why these are split helpers instead of a single M.init that registers
-- with rom.gui.* itself: H2M's sol2 binding for ImGui calls captures the
-- calling Lua function's _ENV at invocation time.  Functions defined in
-- THIS file (ui.lua) carry ui.lua's chunk env as their _ENV — even if
-- ui.lua was loaded with `loadfile(path, "t", main_env)` — meaning sol2
-- attributes ImGui calls made from those functions to the wrong plugin
-- context.  That silently corrupts H2M's per-plugin ImGui state and
-- breaks rendering for OTHER plugins (we reproduced this end-to-end
-- with zerp-MelSkin).
--
-- The fix: main.lua wraps these helpers in closures defined in main.lua's
-- own chunk, then passes those closures to rom.gui.add_to_menu_bar /
-- rom.gui.add_imgui.  The closure carries main.lua's _ENV, so sol2 sees
-- our plugin context correctly and the ImGui call (made from inside the
-- closure when it calls ui.render or ui.render_menu_bar) is attributed
-- to CG3HBuilder.  Don't move the rom.gui.* calls back into this file.

--- Render the top-menu-bar entry.  Toggles the manager window open.
function M.render_menu_bar()
    if ImGui.MenuItem("Mod Manager") then
        window_open = not window_open
    end
end

--- Render the manager window if currently open.  Caller is expected to
-- run this each ImGui frame.
-- @param state   mod_state
-- @param ctx     { on_refresh, on_toggle_mod, on_set_variant_entry,
--                  on_set_variant_all, on_first_frame }
function M.render(state, ctx)
    if window_open then
        _draw_window(state, ctx)
    end
end

--- Whether the manager window is currently open.
function M.is_open()
    return window_open
end

return M
