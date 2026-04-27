"""v3.11 task 86 follow-up — same-skeleton roundtrip on Melinoe.

The YoungMel test PROVED the engine resolves non-stock animation
entry names by hash.  This follow-up confirms the playback path
works end-to-end (visible result: Melinoe re-snaps to her idle pose,
no disappearance) by sourcing from Melinoe's own GPK so the cloned
animation references Melinoe's actual bones.

Strategy: patch the GPK CG3HBuilder already built.

Why
===
CG3HBuilder owns the `Melinoe.gpk` registration slot via
`rom.data.add_granny_file`, so a competing standalone plugin trying
to register its own Melinoe.gpk loses (or worse, knocks out the
user's mesh mods).  Editing CG3HBuilder's own merged GPK on disk
is safe because:

- We don't touch any of CG3HBuilder's inputs (no mods.json or
  source GLB changes), so the cache_key still matches on next
  launch.  CG3HBuilder will log "cached" and skip the rebuild,
  preserving our injection.
- Adding a NEW entry under a unique name doesn't conflict with
  any existing mod content.

What the script does
====================
1. Closes nothing — IT'S YOUR JOB to fully exit the game first
   (the .gpk is held open while the game is running).
2. Reads CG3HBuilder's plugins_data/Enderclem-CG3HBuilder/Melinoe.gpk
3. Picks Melinoe_NoWeapon_Base_Idle_C_00 as the source.
4. Adds the same bytes under Melinoe_Enderclem_RoundtripTest_C_00.
5. Packs back to the same path.
6. Refreshes the standalone smoke-test plugin to ONLY hook F5
   (no add_granny_file needed — the entry rides inside CG3HBuilder's
   already-registered GPK).

Run from the repo root, with the game CLOSED:
    python tests/scripts/test_animation_roundtrip_melinoe.py

After the script finishes, launch the game on the yazy profile,
get into the hub or any combat scene where Melinoe is on screen,
and press F5.  Expected result:
- Melinoe re-snaps to her standing-idle pose for one frame
- The standalone log shows "SetAnimation returned OK"
- No mesh disappearance
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'tools'))

from gpk_pack import extract_gpk, pack_gpk


# Use a visibly-distinct animation (one-shot salute) so we can SEE
# whether the call actually played.  Idle-on-idle is invisible.
#
# SJSON definition (Hero_Melinoe_Animation_Personality.sjson):
#   Name            = "MelinoeSalute"            <-- logical alias
#   GrannyAnimation = "Melinoe_NoWeapon_Base_Salute_00"  <-- GR2 entry key
SOURCE_ENTRY = "Melinoe_NoWeapon_Base_Salute_00"
NEW_ENTRY = "Melinoe_Enderclem_RoundtripTest_00"
STOCK_LOGICAL = "MelinoeSalute"
# v3.11 task #100 — the proof of the full loop.  We register this name as
# an SJSON alias at plugin init via rom.data.on_sjson_read_as_string;
# the alias points at our custom GR2 entry NEW_ENTRY.  If the game plays
# a salute when SetAnimation({Name = NEW_LOGICAL}) is called, the entire
# animation_add architecture works end-to-end (custom GR2 + custom alias).
NEW_LOGICAL = "EnderclemTestSalute"
# Which SJSON file we inject the alias entry into.  Doesn't have to be
# topical — the engine flattens all Animations from all files into one
# table keyed by Name.  Personality is fine and small.
INJECT_SJSON_BASENAME = "Hero_Melinoe_Animation_Personality.sjson"
PLUGIN_NAME = "Enderclem-AnimationAddSmokeTest"


def main():
    yazy_pd = os.path.expandvars(
        r"%APPDATA%\r2modmanPlus-local\HadesII\profiles\yazy"
        r"\ReturnOfModding\plugins_data"
    )
    builder_dir = os.path.join(yazy_pd, "Enderclem-CG3HBuilder")
    merged_gpk = os.path.join(builder_dir, "Melinoe.gpk")
    if not os.path.isfile(merged_gpk):
        print(f"FAIL: CG3HBuilder hasn't built Melinoe.gpk yet at {merged_gpk}")
        print("      Launch the game once with Melinoe mods enabled, then re-run this.")
        return 1

    print(f"[1] Reading {merged_gpk}")
    try:
        entries = extract_gpk(merged_gpk)
    except OSError as e:
        print(f"FAIL: GPK is locked or unreadable: {e}")
        print("      Make sure the game is fully closed.")
        return 1
    n_before = len(entries)
    if SOURCE_ENTRY not in entries:
        print(f"FAIL: {SOURCE_ENTRY} not in CG3HBuilder's merged Melinoe.gpk")
        anim_keys = sorted(k for k in entries if not k.endswith("_Mesh"))
        print(f"  available animations (first 10): {anim_keys[:10]}")
        return 1
    src_bytes = entries[SOURCE_ENTRY]
    print(f"  loaded {n_before} entries; source clip = {len(src_bytes):,} bytes")

    if NEW_ENTRY in entries:
        print(f"  note: {NEW_ENTRY} already exists, will be overwritten")
    entries[NEW_ENTRY] = src_bytes
    print(f"[2] Added {NEW_ENTRY} (now {len(entries)} entries)")

    pack_gpk(entries, merged_gpk)
    sz = os.path.getsize(merged_gpk)
    print(f"[3] Packed back -> {merged_gpk} ({sz:,} bytes)")

    rb = extract_gpk(merged_gpk)
    if NEW_ENTRY not in rb or rb[NEW_ENTRY] != src_bytes:
        print("FAIL: pack/extract roundtrip mismatch")
        return 1
    print("[4] Roundtrip verified")

    # Refresh the standalone trigger plugin (drop add_granny_file —
    # CG3HBuilder already owns the registration).
    plugin_dir = os.path.expandvars(
        r"%APPDATA%\r2modmanPlus-local\HadesII\profiles\yazy"
        r"\ReturnOfModding\plugins\\" + PLUGIN_NAME)
    plugin_data = os.path.expandvars(
        r"%APPDATA%\r2modmanPlus-local\HadesII\profiles\yazy"
        r"\ReturnOfModding\plugins_data\\" + PLUGIN_NAME)
    if os.path.isdir(plugin_dir):
        shutil.rmtree(plugin_dir)
    if os.path.isdir(plugin_data):
        shutil.rmtree(plugin_data)
    os.makedirs(plugin_dir, exist_ok=True)
    os.makedirs(plugin_data, exist_ok=True)

    with open(os.path.join(plugin_dir, "manifest.json"), "w") as f:
        json.dump({
            "name": PLUGIN_NAME,
            "version_number": "0.0.3",
            "website_url": "https://github.com/Enderclem0/CG3H",
            "description": "v3.11 SJSON-alias proof",
            "dependencies": ["Hell2Modding-Hell2Modding-1.0.95"],
        }, f, indent=2)

    main_lua = """-- v3.11 task #100 — full-loop animation_add proof.
--
-- Two pieces:
--   1. We register an `on_sjson_read_as_string` callback for the
--      Personality SJSON.  When the engine reads it at startup, we
--      append a synthesized Animation entry that aliases NEW_LOGICAL
--      to our custom GR2 entry NEW_GRANNY.
--   2. An ImGui button calls SetAnimation({Name = NEW_LOGICAL}) on
--      the Hero.  If Melinoe salutes (the cloned animation is a
--      salute), the full animation_add pipeline is proven: GPK entry +
--      SJSON alias = playable custom animation.

local LOG = "[CG3H/anim_add_test]"
local ANIM_NEW = %s         -- custom GR2 entry name (the GPK key)
local ANIM_GRANNY = %s      -- a stock GR2 entry name (control)
local ANIM_LOGICAL = %s     -- a stock SJSON alias (control: should always work)
local NEW_LOGICAL = %s      -- the alias we synthesize at SJSON read time
local SJSON_BASENAME = %s   -- which SJSON file we patch

-- ====================================================================
-- v3.11 SJSON alias injection
-- ====================================================================
-- The engine resolves SetAnimation({Name=X}) by looking X up in a table
-- of Animation entries it parses out of Content/Game/Animations/Model/
-- *.sjson at startup.  Hell2Modding's `rom.data.on_sjson_read_as_string`
-- hook lets us rewrite the file's contents BEFORE the engine parses it
-- (Hell2Modding/src/.../data.cpp:286-369).  We append a new Animation
-- entry to the existing Animations array.
--
-- The injected SJSON entry — synthesized at callback time so we can
-- substitute the Lua-side names without escaping pain.
local function build_alias_sjson()
    return table.concat({
        "{",
        "    Name = \\"" .. NEW_LOGICAL .. "\\"",
        "    InheritFrom = \\"MelinoeBaseAnimation\\"",
        "    GrannyAnimation = \\"" .. ANIM_NEW .. "\\"",
        "    ChainTo = \\"MelinoeIdleWeaponless\\"",
        "}",
    }, "\\n")
end

-- Inject a single Animation entry into a `{ Animations = [ ... ] }` SJSON.
-- Strategy: the SJSON file ends with `] }` — close-of-array, close-of-
-- table.  We find the LAST `]` in the file (which closes the Animations
-- array) and splice our entry in just before it.
local function inject_animation_entry(content, entry_sjson)
    local last_bracket = nil
    local pos = 1
    while true do
        local i = string.find(content, "]", pos, true)
        if not i then break end
        last_bracket = i
        pos = i + 1
    end
    if not last_bracket then
        rom.log.warning(LOG .. " no `]` found in SJSON, refusing to inject")
        return content
    end
    return string.sub(content, 1, last_bracket - 1)
        .. "\\n\\n" .. entry_sjson .. "\\n\\n"
        .. string.sub(content, last_bracket)
end

if rom.data and rom.data.on_sjson_read_as_string then
    rom.data.on_sjson_read_as_string(function(file_path, content)
        local entry = build_alias_sjson()
        local new_content = inject_animation_entry(content, entry)
        if new_content ~= content then
            rom.log.info(LOG .. " injected " .. NEW_LOGICAL
                .. " into " .. tostring(file_path)
                .. " (size " .. #content .. " -> " .. #new_content .. ")")
        end
        return new_content
    end, SJSON_BASENAME)
    rom.log.info(LOG .. " registered SJSON patcher for " .. SJSON_BASENAME)
else
    rom.log.warning(LOG .. " rom.data.on_sjson_read_as_string missing — "
        .. "the alias won't be registered, NEW_LOGICAL will fail.")
end

local function describe_target(target_id, label)
    rom.log.info(LOG .. " target id=" .. tostring(target_id)
        .. " (" .. tostring(label) .. ")")
end

local function call_set_anim(target_id, name)
    if not (rom.game and rom.game.SetAnimation) then
        rom.log.warning(LOG .. " SetAnimation unavailable")
        return
    end
    if not target_id then
        rom.log.warning(LOG .. " target nil")
        return
    end
    rom.log.info(LOG .. " >>> SetAnimation({DestinationId="
        .. tostring(target_id) .. ", Name=" .. tostring(name) .. "})")
    local ok, err = pcall(rom.game.SetAnimation,
        { DestinationId = target_id, Name = name })
    if ok then
        rom.log.info(LOG .. " <<< OK")
    else
        rom.log.warning(LOG .. " <<< raised: " .. tostring(err))
    end
end

local function get_hero_id()
    local cr = rom.game and rom.game.CurrentRun
    if cr and cr.Hero and cr.Hero.ObjectId then return cr.Hero.ObjectId end
    return nil
end

-- Walk the game's enemy table (engine populates it for the active
-- room) and return the first non-Hero ObjectId we find.
local function get_first_enemy_id()
    local g = rom.game
    if not g then return nil, "no rom.game" end
    local sources = {
        "ActiveEnemies", "RoomEnemies", "CurrentRun.CurrentRoom.SpawnedEnemies",
        "MapState.ActiveEnemies",
    }
    -- naive recurse for the convenience of this test
    local function probe(path)
        local node = g
        for k in string.gmatch(path, "[^.]+") do
            if type(node) ~= "table" then return nil end
            node = node[k]
            if node == nil then return nil end
        end
        return node
    end
    for _, p in ipairs(sources) do
        local t = probe(p)
        if type(t) == "table" then
            for _, v in pairs(t) do
                if type(v) == "table" and v.ObjectId then
                    return v.ObjectId, p
                end
            end
        end
    end
    return nil, "none of the enemy sources had an ObjectId"
end

-- Spawn a fresh thing far away that we can experiment with safely
local function spawn_test_thing()
    local g = rom.game or {}
    if not g.CreateBlankObstacle then
        return nil, "CreateBlankObstacle missing"
    end
    local id = g.CreateBlankObstacle({ Group = "Standing", X = -2000, Y = -2000 })
    return id, "CreateBlankObstacle"
end

local ImGui = rom.ImGui
rom.gui.add_imgui(function()
    if not ImGui then return end
    if ImGui.Begin("Anim Add Smoke Test") then
        ImGui.TextWrapped("Compare SetAnimation behaviour across entity types.")
        ImGui.Separator()
        ImGui.TextDisabled("Full-loop proof (synthesized alias):")
        if ImGui.Button("Hero -> NEW_LOGICAL (custom alias -> custom GR2)") then
            -- This is the v3.11 architecture in one click.  If Melinoe
            -- salutes here, both pieces work: SJSON patcher registered
            -- the alias before the engine read the file, and the alias
            -- correctly resolved to our custom GR2 entry.
            call_set_anim(get_hero_id(), NEW_LOGICAL)
        end
        ImGui.Separator()
        ImGui.TextDisabled("Controls:")
        if ImGui.Button("Hero -> LOGICAL alias (SJSON Name)") then
            -- This is what the game itself uses everywhere.  If THIS
            -- works without disappear, the disappearance was caused by
            -- passing a GR2 entry name (not a logical alias) to
            -- SetAnimation.  That means custom animations need an
            -- SJSON-side alias entry, not just a GPK entry.
            call_set_anim(get_hero_id(), ANIM_LOGICAL)
        end
        if ImGui.Button("Hero -> GR2 entry name (control)") then
            call_set_anim(get_hero_id(), ANIM_GRANNY)
        end
        if ImGui.Button("Hero -> CUSTOM GR2 entry (control)") then
            call_set_anim(get_hero_id(), ANIM_NEW)
        end
        ImGui.Separator()
        ImGui.TextDisabled("Other targets:")
        if ImGui.Button("Hecate (NPC) -> Salute") then
            local g = rom.game
            local hero = g.CurrentRun and g.CurrentRun.Hero
            if g.GetClosestUnitOfType and hero then
                local id = g.GetClosestUnitOfType({
                    Id = hero.ObjectId,
                    DestinationName = "NPC_Hecate_01"
                })
                describe_target(id, "NPC_Hecate_01")
                call_set_anim(id, "HecateHub_Torch_Base_HubSalute_C_00")
            else
                rom.log.warning(LOG .. " GetClosestUnitOfType missing or hero nil")
            end
        end
        if ImGui.Button("First enemy/NPC -> LOGICAL alias") then
            local id, src = get_first_enemy_id()
            describe_target(id, src)
            call_set_anim(id, ANIM_LOGICAL)
        end
        if ImGui.Button("Spawn blank obstacle -> LOGICAL alias") then
            local id, src = spawn_test_thing()
            describe_target(id, src)
            call_set_anim(id, ANIM_LOGICAL)
        end
        ImGui.Separator()
        if ImGui.Button("Probe globals (dump to log)") then
            local g = rom.game or {}
            rom.log.info(LOG .. " probe: CurrentRun=" .. tostring(g.CurrentRun ~= nil)
                .. " ActiveEnemies=" .. tostring(g.ActiveEnemies ~= nil)
                .. " RoomEnemies=" .. tostring(g.RoomEnemies ~= nil)
                .. " SetAnimation=" .. tostring(g.SetAnimation ~= nil)
                .. " CreateBlankObstacle=" .. tostring(g.CreateBlankObstacle ~= nil))
            local hero = g.CurrentRun and g.CurrentRun.Hero
            if hero then
                rom.log.info(LOG .. " hero keys (sample):")
                local n = 0
                for k, v in pairs(hero) do
                    if n < 12 then
                        rom.log.info(LOG .. "   " .. tostring(k) .. " = " .. tostring(v):sub(1, 80))
                        n = n + 1
                    end
                end
            end
        end
        ImGui.Separator()
        ImGui.TextDisabled("Logical alias: " .. ANIM_LOGICAL)
        ImGui.TextDisabled("GR2 entry:     " .. ANIM_GRANNY)
        ImGui.TextDisabled("Custom GR2:    " .. ANIM_NEW)
    end
    ImGui.End()
end)

rom.log.info(LOG .. " plugin loaded — open H2M GUI (INSERT) for the test window")
""" % (json.dumps(NEW_ENTRY), json.dumps(SOURCE_ENTRY),
        json.dumps(STOCK_LOGICAL), json.dumps(NEW_LOGICAL),
        json.dumps(INJECT_SJSON_BASENAME))
    with open(os.path.join(plugin_dir, "main.lua"), "w") as f:
        f.write(main_lua)
    print(f"[5] Refreshed trigger plugin at {plugin_dir}")

    print()
    print("=" * 64)
    print("PASS: Melinoe.gpk patched + trigger plugin refreshed.")
    print()
    print(f"Source clip: {SOURCE_ENTRY} (Melinoe-side, bones match)")
    print(f"New entry:   {NEW_ENTRY}")
    print()
    print("Expected on next launch + F5 in the hub:")
    print("  - 'SetAnimation returned OK' in the log")
    print("  - Melinoe visibly re-snaps to her idle pose for one frame")
    print("    (no mesh disappearance — the cloned animation references")
    print("    Melinoe's actual bone names)")
    print()
    print("If next launch DOES rebuild Melinoe.gpk (cache miss because you")
    print("toggled mods between runs), our patch will be wiped — re-run")
    print("this script after the rebuild settles.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
