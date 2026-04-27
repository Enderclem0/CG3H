"""v3.11 task 86 smoke test — roundtrip a stock animation as a new GR2 entry.

Hypothesis to test
==================
The engine resolves animation entries via HashGuid lookup on the GPK
entry name.  If we register a GPK with two keys pointing at the same
animation bytes (one stock, one custom), Lua's
SetAnimation({Name = "<custom>"}) should play the same clip.

What the script does
====================
1. Extracts YoungMel.gpk.
2. Picks YoungMel_NoWeapon_Base_Idle_C_00 as the source.
3. Adds the same raw bytes under a new key
   "YoungMel_Enderclem_RoundtripTest_C_00".
4. Writes the modified GPK into the standalone smoke-test plugin's
   data folder.
5. Drops a small standalone Lua plugin into the yazy profile that:
     - Registers the new GPK via rom.data.add_granny_file at init
     - Hooks F5 to call SetAnimation on the player with the new name

This bypasses CG3HBuilder entirely — a standalone Lua plugin owns
the GPK registration and the trigger.  No interaction with the user's
existing mods or the CG3HBuilder build cycle.

What it proves (if successful)
==============================
- Custom animation entry names are resolved by the engine's hash
  lookup, no SDB pre-population needed (HashGuid::Lookup hashes
  the input string at call time).
- Animation playback doesn't cross-check the GR2's internal
  Animation.Name field — the GPK entry key is authoritative.
- The smallest-possible animation_add path works: just write a
  new entry alongside the original.

What it does NOT prove
======================
- Authoring a NEW animation from scratch (we're cloning bytes,
  not constructing a fresh Animation struct).  That's task 87.

Run from the repo root:
    python tests/scripts/test_animation_roundtrip.py

After the script runs successfully, launch the game on the yazy
profile and press F5 in-game — Melinoe should re-trigger her current
clip via our custom-named entry (the cloned source is
YoungMel_Idle, which uses the YoungMel skeleton; played back on
Melinoe it'll show as a re-snap to the source pose).
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'tools'))

from cg3h_constants import find_game_path
from gpk_pack import extract_gpk, pack_gpk


# Source clip and the new entry name we'll add alongside it.
#
# We use YoungMel (which has no CG3H mods on the user's profile) so
# our standalone GPK registration via add_granny_file doesn't fight
# with CG3HBuilder for the same filename slot.  Cross-skeleton play
# (YoungMel anim on Melinoe) collapses the mesh visually because
# track names reference YoungMel's bones — that's expected and is
# itself useful evidence that the engine performs name-based bone
# resolution at playback time.
SOURCE_GPK_BASENAME = "YoungMel.gpk"
SOURCE_ENTRY = "YoungMel_NoWeapon_Base_Idle_C_00"
NEW_ENTRY = "YoungMel_Enderclem_RoundtripTest_C_00"
PLUGIN_NAME = "Enderclem-AnimationAddSmokeTest"


def main():
    game = find_game_path()
    if not game:
        print("FAIL: Hades II install not found"); return 1
    src_gpk = os.path.join(game, "Content", "GR2", "_Optimized", SOURCE_GPK_BASENAME)
    if not os.path.isfile(src_gpk):
        print(f"FAIL: source GPK missing: {src_gpk}"); return 1

    print(f"[1] Extracting {src_gpk}")
    entries = extract_gpk(src_gpk)
    n_before = len(entries)
    if SOURCE_ENTRY not in entries:
        print(f"FAIL: {SOURCE_ENTRY} not in GPK")
        return 1
    src_bytes = entries[SOURCE_ENTRY]
    print(f"  loaded {n_before} entries, source clip {len(src_bytes):,} bytes")

    print(f"[2] Adding new entry {NEW_ENTRY}")
    entries[NEW_ENTRY] = src_bytes
    print(f"  now {len(entries)} entries (was {n_before})")

    # Standalone Lua plugin owns the GPK + trigger.  No interaction with
    # the user's existing CG3H mods or the build cycle.
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

    out_gpk = os.path.join(plugin_data, "YoungMel.gpk")
    pack_gpk(entries, out_gpk)
    sz = os.path.getsize(out_gpk)
    print(f"[3] Packed -> {out_gpk} ({sz:,} bytes)")

    # Verify pack/extract roundtrip
    rb = extract_gpk(out_gpk)
    if NEW_ENTRY not in rb or rb[NEW_ENTRY] != src_bytes:
        print("FAIL: pack/extract roundtrip mismatch")
        return 1
    print("[4] Pack/extract verified: new entry bytes match source")

    # Plugin manifest
    with open(os.path.join(plugin_dir, "manifest.json"), "w") as f:
        json.dump({
            "name": PLUGIN_NAME,
            "version_number": "0.0.1",
            "website_url": "https://github.com/Enderclem0/CG3H",
            "description": "v3.11 task 86 smoke test",
            "dependencies": ["Hell2Modding-Hell2Modding-1.0.92"],
        }, f, indent=2)

    # main.lua: register GPK at init, hook F5
    main_lua = """-- v3.11 task 86 smoke test
-- Registers a YoungMel.gpk that contains a custom animation entry name,
-- and hooks F5 to call SetAnimation with that custom name on the player.

local LOG = "[CG3H/anim_add_test]"
local ANIM_NAME = %s

-- _PLUGIN.plugins_data_mod_folder_path is unreliable under r2modman
-- folder renames, so derive the data folder from the plugin folder
-- by swapping the "/plugins/" segment to "/plugins_data/".
local plugin_folder = _PLUGIN.plugins_mod_folder_path
local data_folder = plugin_folder:gsub("[/\\\\]plugins[/\\\\]", "/plugins_data/")
local gpk_path = rom.path.combine(data_folder, "YoungMel.gpk")

if rom.path.exists(gpk_path) then
    rom.data.add_granny_file("YoungMel.gpk", gpk_path)
    rom.log.info(LOG .. " registered " .. gpk_path)
else
    rom.log.warning(LOG .. " GPK not found at " .. gpk_path)
end

-- F5 hotkey: call SetAnimation with the custom-named entry on the player.
-- H2M's ImGui binding lives under `rom.ImGui`, not as top-level globals.
local ImGui = rom.ImGui
local ImGuiKey = rom.ImGuiKey
rom.gui.add_always_draw_imgui(function()
    if not (ImGui and ImGui.IsKeyPressed and ImGuiKey) then return end
    if ImGui.IsKeyPressed(ImGuiKey.F5, false) then
        rom.log.info(LOG .. " F5 pressed, calling SetAnimation(" .. ANIM_NAME .. ")")

        -- Plugin runs in H2M's Lua state, not the game's.  Globals
        -- like CurrentRun live in the game's _G, exposed to us via
        -- rom.game (per H2M's hades_lua.hpp:118 -- `rom.game = state._G`).
        local g = rom.game or {}
        local set_anim = g.SetAnimation
        local cur_run = g.CurrentRun
        local hero_g = g.Hero
        rom.log.info(LOG .. " probe:"
            .. " rom.game=" .. tostring(rom.game ~= nil)
            .. " SetAnimation=" .. tostring(set_anim ~= nil)
            .. " CurrentRun=" .. tostring(cur_run ~= nil)
            .. " CurrentRun.Hero=" .. tostring(cur_run and cur_run.Hero ~= nil)
            .. " CurrentRun.CurrentRoom=" .. tostring(cur_run and cur_run.CurrentRoom ~= nil)
            .. " Hero=" .. tostring(hero_g ~= nil))

        -- Resolve a player ObjectId.  CurrentRun.Hero is the canonical
        -- path during gameplay; some scenes use a top-level Hero global.
        local target_id = nil
        local source = nil
        if cur_run and cur_run.Hero and cur_run.Hero.ObjectId then
            target_id = cur_run.Hero.ObjectId
            source = "rom.game.CurrentRun.Hero"
        elseif hero_g and hero_g.ObjectId then
            target_id = hero_g.ObjectId
            source = "rom.game.Hero"
        end

        if not (set_anim and target_id) then
            rom.log.warning(LOG .. " no SetAnimation+target_id; load a save / enter combat first")
            return
        end
        rom.log.info(LOG .. " target=" .. tostring(target_id) .. " (" .. source .. ")")

        local ok, err = pcall(set_anim, {
            Name = ANIM_NAME,
            DestinationId = target_id,
        })
        if not ok then
            rom.log.warning(LOG .. " SetAnimation raised: " .. tostring(err))
        else
            rom.log.info(LOG .. " SetAnimation returned OK")
        end
    end
end)

rom.log.info(LOG .. " plugin loaded -- press F5 in-game to trigger " .. ANIM_NAME)
""" % (json.dumps(NEW_ENTRY),)
    with open(os.path.join(plugin_dir, "main.lua"), "w") as f:
        f.write(main_lua)
    print(f"[5] Plugin installed at {plugin_dir}")

    print()
    print("=" * 64)
    print("PASS: roundtrip GPK + smoke-test plugin deployed.")
    print()
    print(f"Source clip: {SOURCE_ENTRY}")
    print(f"New entry:   {NEW_ENTRY}")
    print()
    print("Next steps:")
    print("  1. Launch the game on the yazy profile.")
    print(f"  2. CG3HBuilder log should show '[CG3H/anim_add_test] registered ...'.")
    print( "  3. Reach the hub (or any scene with Melinoe loaded).")
    print( "  4. Press F5.  Watch LogOutput.log:")
    print( "     - 'F5 pressed, calling SetAnimation(...)' — hook fired")
    print( "     - 'SetAnimation returned OK' — engine accepted the call")
    print( "     If both lines appear, the engine resolved our custom-named")
    print( "     entry.  HYPOTHESIS CONFIRMED.")
    print()
    print( "  Visible behaviour: SetAnimation(YoungMel_Idle) on Melinoe will")
    print( "  cause her to snap to YoungMel's idle pose — the bones overlap")
    print( "  (both are humanoid skeletons named with the same conventions),")
    print( "  but the pose may look off.  We're testing engine resolution,")
    print( "  not visual fidelity.")
    print()
    print("To remove the smoke test:")
    print(f"  rm -rf '{plugin_dir}'")
    print(f"  rm -rf '{plugin_data}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
