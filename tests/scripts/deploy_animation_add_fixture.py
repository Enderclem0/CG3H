"""End-to-end fixture for v3.11 animation_add.

Validates the full pipeline in-game with one click:

  1. CG3HBuilder picks up the mod, classifies it as animation_add.
  2. convert() byte-clones the new GR2 entries into the merged
     Melinoe.gpk.
  3. status.json records the alias entries.
  4. main.lua registers SJSON aliases at game startup, BEFORE the
     engine reads animation data.
  5. Trigger plugin's ImGui buttons call SetAnimation with the
     synthesized logical names.

Scope: byte-clone only (no source_glb_action).  Cloning bytes proves
the full builder→status.json→main.lua→engine path; the encoder
itself was already validated by tests/scripts/test_animation_add_build.py
on YoungMel.  The fixture uses a stub GLB (no meshes, no animations)
so we don't have to invoke the exporter (which spawns multiprocessing
workers — re-importing this script in each worker would fork-bomb).

Run from the repo root, with the GAME CLOSED:
    python tests/scripts/deploy_animation_add_fixture.py
"""
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'tools'))

import pygltflib  # type: ignore
from cg3h_constants import find_game_path


CHARACTER = "Melinoe"
MOD_NAME = "MelinoeAnimAddFixture"
MOD_ID = f"Enderclem-{MOD_NAME}"
TRIGGER_PLUGIN = "Enderclem-AnimationAddSmokeTest"

# Two test entries, both targeted at the same character.  The names
# below are the GR2 GPK keys we ship; aliases (Lua-facing) live in
# the same dict under logical_name.
# Byte-clone-only entries.  Each clones the bytes of an existing
# stock animation under a new GR2 entry key, then registers the
# new logical alias via the SJSON injector.  Should play visually
# identically to the underlying source animation.
TEST_ENTRIES = [
    {
        "logical_name": "TestPureCloneSalute",
        "granny_name":  "Melinoe_FixtureTest_PureClone_00",
        "clone_from":   "Melinoe_NoWeapon_Base_Salute_00",
        "inherit_from": "MelinoeBaseAnimation",
        "chain_to":     "MelinoeIdleWeaponless",
    },
    {
        "logical_name": "TestPureCloneIdle",
        "granny_name":  "Melinoe_FixtureTest_IdleClone_00",
        "clone_from":   "Melinoe_NoWeapon_Base_Idle_00",
        "loop": True,
        "inherit_from": "MelinoeBaseAnimation",
    },
]


def _write_stub_glb(glb_path):
    """Write a minimal GLB so convert()'s parse_glb call succeeds.
    No meshes, no animations — convert()'s mesh loop is a no-op and
    the byte-clone path doesn't need any GLB content."""
    g = pygltflib.GLTF2()
    g.asset = pygltflib.Asset(version="2.0", generator="cg3h-fixture-stub")
    g.scenes = [pygltflib.Scene(nodes=[])]
    g.scene = 0
    g.save(glb_path)


def _yazy_plugins_data():
    return os.path.expandvars(
        r"%APPDATA%\r2modmanPlus-local\HadesII\profiles\yazy"
        r"\ReturnOfModding\plugins_data"
    )


def _yazy_plugins():
    return os.path.expandvars(
        r"%APPDATA%\r2modmanPlus-local\HadesII\profiles\yazy"
        r"\ReturnOfModding\plugins"
    )


def _build_trigger_plugin(plugins_dir, plugins_data_dir):
    """Drop a tiny ImGui plugin that lets us click each test alias."""
    plugin_dir = os.path.join(plugins_dir, TRIGGER_PLUGIN)
    plugin_data = os.path.join(plugins_data_dir, TRIGGER_PLUGIN)
    for d in (plugin_dir, plugin_data):
        if os.path.isdir(d):
            shutil.rmtree(d)
        os.makedirs(d, exist_ok=True)

    # Build a Lua list literal of {label, alias} pairs from TEST_ENTRIES
    lua_pairs = ",\n".join(
        f'    {{ {json.dumps(e["logical_name"])}, '
        f'{json.dumps(e["logical_name"])} }}'
        for e in TEST_ENTRIES
    )

    main_lua = f"""-- v3.11 animation_add fixture trigger.
-- Open the H2M GUI (INSERT) to find the test window.

local LOG = "[CG3H/anim_add_fixture]"
local ALIASES = {{
{lua_pairs}
}}

local function get_hero_id()
    local cr = rom.game and rom.game.CurrentRun
    if cr and cr.Hero and cr.Hero.ObjectId then return cr.Hero.ObjectId end
    return nil
end

local function play(alias)
    local set_anim = rom.game and rom.game.SetAnimation
    local id = get_hero_id()
    if not (set_anim and id) then
        rom.log.warning(LOG .. " no SetAnimation+Hero (load a save)")
        return
    end
    rom.log.info(LOG .. " >>> SetAnimation Name=" .. tostring(alias))
    local ok, err = pcall(set_anim, {{ Name = alias, DestinationId = id }})
    if ok then
        rom.log.info(LOG .. " <<< OK")
    else
        rom.log.warning(LOG .. " <<< raised: " .. tostring(err))
    end
end

local ImGui = rom.ImGui
rom.gui.add_imgui(function()
    if not ImGui then return end
    if ImGui.Begin("Anim Add Fixture") then
        ImGui.TextWrapped(
            "Three animation_add aliases, all built end-to-end through "
            .. "the v3.11 pipeline.  All three should play without "
            .. "Melinoe disappearing.")
        ImGui.Separator()
        for _, pair in ipairs(ALIASES) do
            local label, alias = pair[1], pair[2]
            if ImGui.Button("Hero -> " .. label) then
                play(alias)
            end
        end
        ImGui.Separator()
        if ImGui.Button("Hero -> MelinoeSalute (control)") then
            play("MelinoeSalute")
        end
    end
    ImGui.End()
end)

rom.log.info(LOG .. " trigger plugin loaded")
"""
    with open(os.path.join(plugin_dir, "main.lua"), "w") as f:
        f.write(main_lua)
    with open(os.path.join(plugin_dir, "manifest.json"), "w") as f:
        json.dump({
            "name": TRIGGER_PLUGIN,
            "version_number": "0.1.0",
            "website_url": "https://github.com/Enderclem0/CG3H",
            "description": "v3.11 animation_add fixture trigger",
            "dependencies": ["Hell2Modding-Hell2Modding-1.0.95"],
        }, f, indent=2)
    return plugin_dir


def main():
    game = find_game_path()
    if not game:
        print("FAIL: Hades II install not found"); return 1
    gpk_dir = os.path.join(game, "Content", "GR2", "_Optimized")
    dll = os.path.join(game, "Ship", "granny2_x64.dll")
    char_gpk = os.path.join(gpk_dir, f"{CHARACTER}.gpk")
    if not os.path.isfile(char_gpk):
        print(f"FAIL: {CHARACTER}.gpk missing at {char_gpk}"); return 1

    src_root = os.path.join(os.path.expanduser("~"), "Documents", "CG3H_Mods")
    src_dir = os.path.join(src_root, MOD_NAME)
    if os.path.isdir(src_dir):
        shutil.rmtree(src_dir)
    os.makedirs(src_dir, exist_ok=True)
    print(f"[1] Source workspace: {src_dir}")

    glb_path = os.path.join(src_dir, f"{CHARACTER}.glb")
    print(f"[2] Writing stub GLB (byte-clone path needs no GLB content)...")
    _write_stub_glb(glb_path)

    # mod.json
    mod_json = {
        "format": "cg3h-mod/1.0",
        "metadata": {
            "name": MOD_NAME,
            "author": "Enderclem",
            "version": "1.0.0",
            "description": "v3.11 animation_add end-to-end fixture",
        },
        "type": "animation_add",
        "target": {
            "character": CHARACTER,
            "new_animations": TEST_ENTRIES,
        },
        "assets": {"glb": f"{CHARACTER}.glb"},
    }
    with open(os.path.join(src_dir, "mod.json"), "w") as f:
        json.dump(mod_json, f, indent=2)
    print(f"[3] Wrote mod.json with {len(TEST_ENTRIES)} new animation(s)")

    yazy_pd = _yazy_plugins_data()
    if not os.path.isdir(yazy_pd):
        print(f"FAIL: yazy plugins_data not found: {yazy_pd}"); return 1
    deployed = os.path.join(yazy_pd, MOD_ID)
    if os.path.isdir(deployed):
        shutil.rmtree(deployed)
    shutil.copytree(src_dir, deployed)
    print(f"[4] Installed fixture: {deployed}")

    # Bust Melinoe's cache so CG3HBuilder rebuilds with the new mod.
    cache_key = os.path.join(yazy_pd, "Enderclem-CG3HBuilder",
                             f"{CHARACTER}.cache_key")
    if os.path.isfile(cache_key):
        os.unlink(cache_key)
        print(f"  bust cache key: {cache_key}")

    builder_exe = os.path.join(yazy_pd, "Enderclem-CG3HBuilder",
                               "cg3h_builder.exe")
    if os.path.isfile(builder_exe):
        print(f"[5] Triggering rebuild via {builder_exe}...")
        proc = subprocess.run(
            [builder_exe, "--scan-all", yazy_pd, "--character", CHARACTER],
            capture_output=True, text=True, timeout=600,
        )
        last = "\n".join(proc.stdout.splitlines()[-25:])
        print(last)
        if proc.returncode != 0:
            print(f"FAIL: builder exited {proc.returncode}")
            print(proc.stderr[-2000:])
            return 1
    else:
        print(f"WARNING: cg3h_builder.exe not found at {builder_exe}; "
              f"the rebuild will run on next game launch instead.")

    plugins_dir = _yazy_plugins()
    plugin_dir = _build_trigger_plugin(plugins_dir, yazy_pd)
    print(f"[6] Trigger plugin deployed: {plugin_dir}")

    print()
    print("=" * 64)
    print("PASS: animation_add fixture deployed.")
    print()
    print("Next steps:")
    print("  1. Launch the game on the yazy profile.")
    print("  2. Watch the launch log for "
          "'CG3H N custom animation alias(es) registered'")
    print("     (N should equal len(TEST_ENTRIES) =", len(TEST_ENTRIES), ")")
    print("  3. Reach the hub.  Open H2M GUI (INSERT) -> 'Anim Add Fixture'.")
    print("  4. Click each Hero button; expected results:")
    print("     - TestPureCloneSalute     -> Melinoe salutes (alias maps")
    print("                                  to a clone of MelinoeSalute)")
    print("     - TestPureCloneIdle       -> Melinoe re-snaps to idle (alias")
    print("                                  maps to a clone of stock idle)")
    print("     - MelinoeSalute (control) -> stock salute, should match #1")
    print("  5. None of the buttons should make Melinoe disappear.  If")
    print("     all three play correctly, the v3.11 builder->status.json->")
    print("     main.lua->SJSON-injection->SetAnimation pipeline is proven.")
    print()
    print("To remove the fixture:")
    print(f"  rm -rf '{deployed}'")
    print(f"  rm -rf '{src_dir}'")
    print(f"  rm -rf '{plugin_dir}'")
    print(f"  rm -f  '{cache_key}'")
    return 0


if __name__ == '__main__':
    sys.exit(main())
