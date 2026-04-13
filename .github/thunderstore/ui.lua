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

-- v3.8: session reload counter.  Each successful hot-reload bumps this.
-- Over a threshold the UI shows a soft pool-pressure warning.
local session_reload_count = 0
local POOL_PRESSURE_THRESHOLD = 10

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

--- Invoke a context callback that returns a hot-reload outcome string,
-- then turn that outcome into a transient banner the window shows until
-- the next refresh.  Centralised so toggle + rebuild share behaviour.
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
                   text = label .. ": rebuilt — restart the game to see changes." }
    elseif outcome == "error" then
        banner = { kind = "error",
                   text = label .. ": hot-reload failed (see console)." }
    elseif outcome == nil then
        banner = { kind = "error",
                   text = label .. ": rebuild failed (see console)." }
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

            ImGui.SameLine()
            if ImGui.SmallButton("Rebuild##" .. char) then
                _run_and_banner(function()
                    return ctx.on_rebuild and ctx.on_rebuild(char)
                end, char)
            end

            if rec.gpk_path then
                ImGui.TextDisabled("GPK: " .. rec.gpk_path)
            end
            if rec.duration_ms and rec.duration_ms > 0 then
                ImGui.TextDisabled(string.format("Last build: %d ms", rec.duration_ms))
            end
            if rec.error then
                ImGui.TextColored(1.0, 0.4, 0.4, 1.0, "Error: " .. rec.error)
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
-- @param ctx     { on_refresh = fn, on_toggle_mod = fn(id, enabled), on_rebuild = fn(char) }
function M.init(state, ctx)
    ctx = ctx or {}

    rom.gui.add_to_menu_bar(function()
        if ImGui.MenuItem("Mod Manager") then
            window_open = not window_open
        end
    end)

    rom.gui.add_imgui(function()
        if window_open then
            _draw_window(state, ctx)
        end
    end)
end

return M
