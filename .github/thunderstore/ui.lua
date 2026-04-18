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
            if rom.data.dump_pool_stats then
                local n = rom.data.dump_pool_stats()
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

--- Register the ImGui callbacks with H2M.
-- @param state   the mod_state table (read-only from here on)
-- @param ctx     { on_refresh = fn, on_toggle_mod = fn(id, enabled), ... }
function M.init(state, ctx)
    ctx = ctx or {}

    rom.gui.add_to_menu_bar(function()
        if ImGui.MenuItem("Mod Manager") then
            window_open = not window_open
        end
    end)

    -- One-shot: on the first ImGui frame, apply the default/persisted
    -- variant selections.  Deferred to first frame because model entries
    -- only exist in mModelData after LoadAllModelAndAnimationData — which
    -- runs BEFORE any ImGui callback fires but AFTER plugin init.  See
    -- project_loading_timeline.md.
    local did_init_variants = false
    rom.gui.add_imgui(function()
        if not did_init_variants then
            did_init_variants = true
            if ctx.on_first_frame then ctx.on_first_frame() end
        end
        if window_open then
            _draw_window(state, ctx)
        end
    end)
end

return M
