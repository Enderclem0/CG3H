"""Deploy an animation-only fixture mod into the yazy r2modman profile,
then trigger a build so CG3HBuilder picks it up via the normal flow.

What this proves
================
- An animation_patch mod with NO mesh content survives the full
  pipeline: scan -> classify -> _merge_glbs -> convert(patch_animations)
  -> output GPK -> register via add_granny_file -> game loads.
- Mixed case: when a character already has mesh mods installed, an
  animation_patch mod layered on top doesn't break the mesh path.

What it does NOT prove
======================
This is a "round-trip" test — the animations are extracted, stripped
of mesh data, and re-patched without value modification.  The patched
GPK should produce visually identical animations to stock.  If the
game launches and the character animates normally, the runtime path
is healthy.  For visible-change verification, use a real Blender-edited
mod (task 83 e2e).

Run from the repo root:
    python tests/scripts/deploy_animation_only_fixture.py [character]

Default character: Melinoe (always visible in the hub).  Pass YoungMel
or any other character GPK basename to target a different one.

Side effects
============
- Creates fixture at:
    Documents/CG3H_Mods/<Character>AnimRoundtrip/
- Installs into yazy profile:
    %APPDATA%/r2modmanPlus-local/HadesII/profiles/yazy/ReturnOfModding/
        plugins_data/Enderclem-<Character>AnimRoundtrip/
- Forces a per-character rebuild via cg3h_builder.exe --character.
- Existing mods in yazy are untouched.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'tools'))

import pygltflib  # type: ignore
from cg3h_constants import find_game_path
from gr2_to_gltf import main as exporter_main


def _strip_meshes(glb_path):
    g = pygltflib.GLTF2().load(glb_path)
    n_meshes = len(g.meshes or [])
    n_anims = len(g.animations or [])
    g.meshes = []
    if g.nodes:
        for node in g.nodes:
            node.mesh = None
    if g.scenes:
        for scene in g.scenes:
            scene.nodes = [
                ni for ni in (scene.nodes or []) if ni < len(g.nodes)
            ]
    g.save(glb_path)
    print(f"  stripped {n_meshes} meshes, kept {n_anims} animations")


def main():
    character = sys.argv[1] if len(sys.argv) > 1 else "Melinoe"
    mod_id = f"Enderclem-{character}AnimRoundtrip"
    mod_name = f"{character}AnimRoundtrip"

    game = find_game_path()
    if not game:
        print("FAIL: Hades II install not found"); return 1
    gpk_dir = os.path.join(game, "Content", "GR2", "_Optimized")
    dll = os.path.join(game, "Ship", "granny2_x64.dll")
    char_gpk = os.path.join(gpk_dir, f"{character}.gpk")
    if not os.path.isfile(char_gpk):
        print(f"FAIL: {character}.gpk not in {gpk_dir}"); return 1

    src_root = os.path.join(os.path.expanduser("~"), "Documents", "CG3H_Mods")
    src_dir = os.path.join(src_root, f"{character}AnimRoundtrip")
    if os.path.isdir(src_dir):
        shutil.rmtree(src_dir)
    os.makedirs(src_dir, exist_ok=True)
    print(f"[1] Source workspace: {src_dir}")

    # Extract character animations into the workspace as the fixture GLB.
    glb_path = os.path.join(src_dir, f"{character}.glb")
    print(f"[2] Extracting {character} + animations...")
    sys.argv = [
        "exporter", character,
        "--gpk-dir", gpk_dir,
        "--dll", dll,
        "-o", glb_path,
        "--animations",
    ]
    old_cwd = os.getcwd()
    try:
        os.chdir(os.path.join(game, "Ship"))
        exporter_main()
    finally:
        os.chdir(old_cwd)
    if not os.path.isfile(glb_path):
        print("FAIL: exporter produced no GLB"); return 1

    # Move the manifest the exporter writes alongside (so cg3h_build's
    # _sync_mod_json can hash-check animations against it).
    tmp_manifest = os.path.join(src_dir, "manifest.json")
    if not os.path.isfile(tmp_manifest):
        # exporter may write it in cwd or alongside; find and move
        for cand in (
            os.path.join(os.path.dirname(glb_path), "manifest.json"),
            os.path.join(game, "Ship", "manifest.json"),
        ):
            if os.path.isfile(cand) and cand != tmp_manifest:
                shutil.move(cand, tmp_manifest)
                break

    print("[3] Stripping mesh content from GLB...")
    _strip_meshes(glb_path)

    # Write mod.json (cg3h_build's _sync_mod_json will populate
    # target.animations on first build).
    mod_json = {
        "format": "cg3h-mod/1.0",
        "metadata": {
            "name": mod_name,
            "author": "Enderclem",
            "version": "1.0.0",
            "description": (
                "Round-trip animation_patch mod for runtime-path "
                "validation (v3.10 smoke test)."
            ),
        },
        "type": "animation_patch",
        "target": {"character": character},
        "assets": {"glb": f"{character}.glb"},
    }
    mod_json_path = os.path.join(src_dir, "mod.json")
    with open(mod_json_path, "w") as f:
        json.dump(mod_json, f, indent=2)
    print(f"  wrote {mod_json_path}")

    # Install into yazy profile's plugins_data so CG3HBuilder finds it.
    yazy_pd = os.path.expandvars(
        r"%APPDATA%\r2modmanPlus-local\HadesII\profiles\yazy"
        r"\ReturnOfModding\plugins_data"
    )
    if not os.path.isdir(yazy_pd):
        print(f"FAIL: yazy plugins_data dir not found: {yazy_pd}")
        return 1
    deployed = os.path.join(yazy_pd, mod_id)
    if os.path.isdir(deployed):
        shutil.rmtree(deployed)
    shutil.copytree(src_dir, deployed)
    print(f"[4] Installed fixture: {deployed}")

    # Force a per-character rebuild.  cg3h_builder.exe lives next to the
    # plugin's exe; we use the deployed copy in plugins_data.
    builder_exe = os.path.join(yazy_pd, "Enderclem-CG3HBuilder", "cg3h_builder.exe")
    if not os.path.isfile(builder_exe):
        print(f"WARNING: cg3h_builder.exe not found at {builder_exe} -- "
              f"skipping forced rebuild.  CG3HBuilder will rebuild on "
              f"next game launch.")
    else:
        print(f"[5] Triggering rebuild via {builder_exe}...")
        proc = subprocess.run(
            [builder_exe, "--scan-all", yazy_pd, "--character", character],
            capture_output=True,
            text=True,
            timeout=600,
        )
        # Show last 20 lines so we can see "Done!" or errors
        last = "\n".join(proc.stdout.splitlines()[-20:])
        print(last)
        if proc.returncode != 0:
            print(f"FAIL: builder exited {proc.returncode}")
            print(proc.stderr[-2000:])
            return 1

    print()
    print("=" * 60)
    print(f"PASS: fixture deployed for {character}.")
    print()
    print("Next steps:")
    print(f"  1. Launch the game on the yazy profile.")
    print(f"  2. Watch CG3HBuilder's startup log for "
          f"'Registered GPK: {character}.gpk'.")
    print(f"  3. Walk to a scene where {character} appears and check "
          f"animations look normal.")
    print()
    print("To remove the fixture:")
    print(f"  rm -rf '{deployed}'")
    print(f"  rm -rf '{src_dir}'")
    print(f"  delete the {character}.cache_key in plugins_data/Enderclem-CG3HBuilder/")
    return 0


if __name__ == '__main__':
    sys.exit(main())
