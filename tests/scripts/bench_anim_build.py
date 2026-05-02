"""v3.14 benchmark — animation_patch build time vs anim count.

Builds an `animation_patch`-style call to `convert()` against
Melinoe's stock GR2, varying the number of animations the GLB
carries (5 / 25 / all).  Each size is timed twice — once with the
v3.14 skip-load short-circuit enabled, once with it disabled
(CG3H_NO_SKIP_LOAD=1) — so we can quantify the speedup per case.

Run from the repo root:

    python tests/scripts/bench_anim_build.py
    python tests/scripts/bench_anim_build.py --quick    # only N=5

Doesn't touch the game install; needs the game's GR2/SDB + DLL
discoverable via cg3h_constants.find_game_path().  Each run uses
its own tempdir and cleans up after itself.
"""
import argparse
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'tools'))

import pygltflib  # type: ignore
from cg3h_constants import find_game_path
from gpk_pack import extract_gpk
from gltf_to_gr2 import convert
from gr2_to_gltf import main as exporter_main


def _strip_meshes(glb_path):
    """Drop mesh primitives + node refs; keep animations + skin."""
    g = pygltflib.GLTF2().load(glb_path)
    g.meshes = []
    if g.nodes:
        for node in g.nodes:
            node.mesh = None
    if g.scenes:
        for scene in g.scenes:
            scene.nodes = [ni for ni in (scene.nodes or [])
                           if ni < len(g.nodes)]
    g.save(glb_path)


def _truncate_animations(src_glb, dst_glb, n):
    """Copy src_glb → dst_glb, keep only the first n animations.
    n=None keeps all."""
    g = pygltflib.GLTF2().load(src_glb)
    if n is not None:
        g.animations = (g.animations or [])[:n]
    g.save(dst_glb)
    return len(g.animations or [])


def _time_convert(glb_path, gpk_path, sdb_path, dll_path, work):
    """One convert(patch_animations=True) call, return (elapsed_s, patched_count)."""
    out_gpk = os.path.join(work, f"out_{os.path.basename(glb_path)}.gpk")
    t0 = time.monotonic()
    result = convert(
        glb_path=glb_path,
        gpk_path=gpk_path,
        sdb_path=sdb_path,
        dll_path=dll_path,
        output_gpk=out_gpk,
        allow_topology_change=False,
        patch_animations=True,
    )
    t1 = time.monotonic()
    if not os.path.isfile(out_gpk):
        raise RuntimeError(f"convert() produced no output for {glb_path}")
    entries = extract_gpk(out_gpk)
    n_anim = sum(1 for k in entries if not k.endswith("_Mesh"))
    return t1 - t0, n_anim


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--quick', action='store_true',
                    help='only run N=5 (fast iteration)')
    args = ap.parse_args()

    game = find_game_path()
    if not game:
        print("FAIL: Hades II install not found")
        return 1
    gpk_dir = os.path.join(game, "Content", "GR2", "_Optimized")
    dll = os.path.join(game, "Ship", "granny2_x64.dll")
    mel_gpk = os.path.join(gpk_dir, "Melinoe.gpk")
    mel_sdb = os.path.join(gpk_dir, "Melinoe.sdb")
    for p in (gpk_dir, dll, mel_gpk, mel_sdb):
        if not os.path.exists(p):
            print(f"FAIL: required path missing: {p}")
            return 1

    work = tempfile.mkdtemp(prefix="cg3h_anim_bench_")
    print(f"workspace: {work}")
    try:
        # Step 1: full export of Mel + every animation.
        master_glb = os.path.join(work, "Melinoe_full.glb")
        old_cwd = os.getcwd()
        old_argv = sys.argv
        try:
            os.chdir(os.path.join(game, "Ship"))
            sys.argv = [
                "exporter", "Melinoe",
                "--gpk-dir", gpk_dir,
                "--dll", dll,
                "-o", master_glb,
                "--animations",
            ]
            print("extracting Mel + every animation…")
            t0 = time.monotonic()
            exporter_main()
            print(f"  extract took {time.monotonic() - t0:.1f}s")
        finally:
            os.chdir(old_cwd)
            sys.argv = old_argv
        _strip_meshes(master_glb)

        # Total anim count in source GR2 (= what skip-load would scan
        # with no filter; what's in our master GLB after full export).
        master = pygltflib.GLTF2().load(master_glb)
        total_anims = len(master.animations or [])
        gpk_entries = extract_gpk(mel_gpk)
        gpk_anim_count = sum(1 for k in gpk_entries if not k.endswith("_Mesh"))
        print(f"  Mel.gpk has {gpk_anim_count} anim entries; "
              f"GLB has {total_anims} actions")

        # Step 2: build size-truncated copies of the GLB.
        sizes = [5] if args.quick else [5, 25, None]   # None = all
        glbs = {}
        for n in sizes:
            label = "all" if n is None else str(n)
            dst = os.path.join(work, f"Melinoe_{label}.glb")
            kept = _truncate_animations(master_glb, dst, n)
            glbs[label] = (dst, kept)
            print(f"  N={label}: kept {kept} animations")

        # Step 3: time each (skip-load on, then off).
        results = {}
        for label, (glb, anim_count) in glbs.items():
            results[label] = {}
            for mode_label, env_val in (("ON", "0"), ("OFF", "1")):
                os.environ['CG3H_NO_SKIP_LOAD'] = env_val
                print(f"\n--- N={label} ({anim_count} anims), "
                      f"skip-load={mode_label} ---")
                elapsed, patched = _time_convert(
                    glb, mel_gpk, mel_sdb, dll, work)
                results[label][mode_label] = (elapsed, patched)
                print(f"  -> {elapsed:.2f}s, {patched} entries patched")
            os.environ.pop('CG3H_NO_SKIP_LOAD', None)

        # Step 4: pretty summary
        print()
        print("== summary ==")
        print(f"{'N':<6} {'on':>10} {'off':>10} {'speedup':>10}")
        print("-" * 38)
        for label, (_, kept) in glbs.items():
            on_t, _  = results[label]['ON']
            off_t, _ = results[label]['OFF']
            speedup = off_t / on_t if on_t > 0 else float('inf')
            print(f"N={label:<4} {on_t:>9.2f}s {off_t:>9.2f}s "
                  f"{speedup:>9.1f}x")
        print()
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
