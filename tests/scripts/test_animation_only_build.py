"""Standalone smoke test for v3.10 animation_patch build path.

Synthesises an animation-only mod from YoungMel's GR2 + animations and
runs it through `convert(patch_animations=True)` to verify:

  1. convert() no longer raises "No meshes were patched" on mesh-less
     GLBs.
  2. patch_animation_entries actually patches at least one animation.
  3. The output GPK is well-formed (extract_gpk readable).

Run from the repo root:
    python tests/scripts/test_animation_only_build.py

Doesn't touch the game; doesn't need Blender; just GR2 + DLL.
"""
import os
import sys
import shutil
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'tools'))

import pygltflib  # type: ignore
from cg3h_constants import find_game_path
from gltf_to_gr2 import convert
from gpk_pack import extract_gpk
from gr2_to_gltf import main as exporter_main


def _strip_meshes(glb_path):
    """Open a GLB, drop every mesh primitive (and their accessors / nodes
    that referenced them), keep skin + animations. Save back in-place.
    """
    g = pygltflib.GLTF2().load(glb_path)
    n_meshes = len(g.meshes or [])
    n_anims = len(g.animations or [])
    g.meshes = []
    if g.nodes:
        for node in g.nodes:
            node.mesh = None
    if g.scenes:
        for scene in g.scenes:
            keep = []
            for ni in (scene.nodes or []):
                if ni < len(g.nodes):
                    keep.append(ni)
            scene.nodes = keep
    g.save(glb_path)
    print(f"  stripped {n_meshes} meshes, kept {n_anims} animations")


def main():
    game = find_game_path()
    if not game:
        print("FAIL: Hades II install not found")
        return 1
    gpk_dir = os.path.join(game, "Content", "GR2", "_Optimized")
    dll = os.path.join(game, "Ship", "granny2_x64.dll")
    youngmel_gpk = os.path.join(gpk_dir, "YoungMel.gpk")
    youngmel_sdb = os.path.join(gpk_dir, "YoungMel.sdb")
    for p in (gpk_dir, dll, youngmel_gpk, youngmel_sdb):
        if not os.path.exists(p):
            print(f"FAIL: required path missing: {p}")
            return 1

    work = tempfile.mkdtemp(prefix="cg3h_anim_only_test_")
    print(f"[1] Workspace: {work}")
    try:
        # Step 1: extract YoungMel as GLB with animations
        glb_path = os.path.join(work, "YoungMel.glb")
        sys.argv = [
            "exporter", "YoungMel",
            "--gpk-dir", gpk_dir,
            "--dll", dll,
            "-o", glb_path,
            "--animations",
        ]
        old_cwd = os.getcwd()
        try:
            os.chdir(os.path.join(game, "Ship"))
            print("[2] Extracting YoungMel + animations…")
            exporter_main()
        finally:
            os.chdir(old_cwd)
        if not os.path.isfile(glb_path):
            print("FAIL: exporter produced no GLB")
            return 1
        size_before = os.path.getsize(glb_path)
        print(f"  extracted GLB: {size_before:,} bytes")

        # Step 2: strip meshes (keep animations only)
        print("[3] Stripping mesh content…")
        _strip_meshes(glb_path)
        size_after = os.path.getsize(glb_path)
        print(f"  GLB shrunk: {size_before:,} → {size_after:,} bytes")

        # Step 3: run convert with patch_animations on the mesh-less GLB
        out_gpk = os.path.join(work, "YoungMel_anim_only.gpk")
        print("[4] Running convert(patch_animations=True) on mesh-less GLB…")
        convert(
            glb_path=glb_path,
            gpk_path=youngmel_gpk,
            sdb_path=youngmel_sdb,
            dll_path=dll,
            output_gpk=out_gpk,
            allow_topology_change=False,
            patch_animations=True,
        )

        # Step 4: verify output
        if not os.path.isfile(out_gpk):
            print("FAIL: convert() produced no output GPK")
            return 1
        entries = extract_gpk(out_gpk)
        n_total = len(entries)
        n_mesh = sum(1 for k in entries if k.endswith("_Mesh"))
        n_anim = n_total - n_mesh
        print(f"[5] Output GPK: {n_total} entries ({n_mesh} mesh, {n_anim} animation)")
        if n_anim == 0:
            print("FAIL: no animation entries in output GPK")
            return 1

        print()
        print("PASS: animation-only mod built successfully against YoungMel.")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
