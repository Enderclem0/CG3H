"""Bulk-export every Hades II character GPK to a single reference dump.

Drives `gr2_to_gltf.py` as a subprocess for each character (4 in parallel),
writes one subfolder per character containing the .glb + manifest +
last-30-lines export log.  Idempotent: re-running skips characters whose
GLB already exists with non-zero size.

Output: C:/Users/ender/Documents/Hades2_Reference_Dump/<Character>/

Run from anywhere:
    python C:/Users/ender/GhidraHades2/tools/bulk_export_reference.py
"""
import concurrent.futures
import datetime
import os
import subprocess
import sys
import time

GAME = r"C:/Program Files (x86)/Steam/steamapps/common/Hades II"
GPK_DIR = os.path.join(GAME, "Content", "GR2", "_Optimized")
DLL = os.path.join(GAME, "Ship", "granny2_x64.dll")
EXPORTER = r"C:/Users/ender/GhidraHades2/tools/gr2_to_gltf.py"
DUMP_ROOT = r"C:/Users/ender/Documents/Hades2_Reference_Dump"
PARALLEL = 4
# 60-min ceiling per character.  Melinoe alone has 843 animations and
# her texture set is the largest in the game; with --textures and 8
# anim-workers she runs ~25-35 min.  Other characters finish in
# seconds, so the headroom only kicks in when needed.
TIMEOUT_SEC = 3600


def list_characters():
    """Top-level *.gpk files only; skip _backups and stray files."""
    out = []
    for entry in sorted(os.listdir(GPK_DIR)):
        full = os.path.join(GPK_DIR, entry)
        if not os.path.isfile(full):
            continue
        if not entry.lower().endswith(".gpk"):
            continue
        out.append(entry[:-4])  # strip ".gpk"
    return out


def export_character(name):
    """Run the exporter for one character.  Returns (name, success, msg)."""
    out_dir = os.path.join(DUMP_ROOT, name)
    out_glb = os.path.join(out_dir, f"{name}.glb")

    if os.path.isfile(out_glb) and os.path.getsize(out_glb) > 0:
        return (name, True, "skipped (already exported)")

    os.makedirs(out_dir, exist_ok=True)

    cmd = [
        sys.executable, EXPORTER, name,
        "--gpk-dir", GPK_DIR,
        "--dll", DLL,
        "--animations",
        "--textures",  # embeds the 3D model textures in the GLB + saves DDS alongside
        "-o", out_glb,
    ]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SEC,
            cwd=os.path.dirname(EXPORTER),
        )
    except subprocess.TimeoutExpired:
        msg = f"timeout after {TIMEOUT_SEC}s"
        with open(os.path.join(out_dir, "_export.log"), "w", encoding="utf-8") as f:
            f.write("=== TIMEOUT ===\n")
        return (name, False, msg)
    except Exception as e:
        return (name, False, f"subprocess error: {e}")

    elapsed = time.monotonic() - started
    log_lines = (proc.stdout or "").splitlines()[-30:]
    if proc.stderr:
        log_lines.append("--- STDERR ---")
        log_lines.extend((proc.stderr or "").splitlines()[-30:])
    with open(os.path.join(out_dir, "_export.log"), "w", encoding="utf-8") as f:
        f.write(f"exit={proc.returncode}  elapsed={elapsed:.1f}s\n\n")
        f.write("\n".join(log_lines))

    if proc.returncode != 0:
        return (name, False, f"exit {proc.returncode}")

    if not os.path.isfile(out_glb) or os.path.getsize(out_glb) == 0:
        return (name, False, "no GLB produced")

    return (name, True, f"{elapsed:.1f}s")


def write_readme(results, total_wall_time):
    succ = [r for r in results if r[1]]
    fail = [r for r in results if not r[1]]
    skipped = [r for r in succ if "skipped" in r[2]]
    fresh = [r for r in succ if "skipped" not in r[2]]

    lines = [
        "# Hades II reference dump",
        "",
        f"Generated: {datetime.datetime.now().isoformat(timespec='seconds')}",
        f"Total wall time: {total_wall_time:.1f}s ({total_wall_time/60:.1f} min)",
        "",
        f"- Characters attempted: {len(results)}",
        f"- Succeeded:            {len(succ)}",
        f"  ({len(fresh)} freshly exported, {len(skipped)} skipped via cache)",
        f"- Failed:               {len(fail)}",
        "",
        "## Layout",
        "",
        "Each `<Character>/` subfolder contains:",
        "- `<Character>.glb` — mesh + skeleton + animations + textures",
        "- `manifest.json` — per-character metadata (mesh entries, hashes, etc.)",
        "- `.baseline_positions.npz` — baseline vertex positions (used by mod-build edit-detection)",
        "- `_export.log` — last 30 lines of the exporter run",
        "",
        "## Failed characters",
        "",
    ]
    if fail:
        for name, _, msg in fail:
            lines.append(f"- **{name}** — {msg}")
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("## Re-run")
    lines.append("")
    lines.append("`python C:/Users/ender/GhidraHades2/tools/bulk_export_reference.py`")
    lines.append("")
    lines.append("Already-exported characters are skipped; only failures + new GPKs run.")

    with open(os.path.join(DUMP_ROOT, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    if not os.path.isdir(GPK_DIR):
        print(f"FAIL: GPK dir missing: {GPK_DIR}")
        return 1
    if not os.path.isfile(DLL):
        print(f"FAIL: DLL missing: {DLL}")
        return 1
    if not os.path.isfile(EXPORTER):
        print(f"FAIL: exporter missing: {EXPORTER}")
        return 1

    os.makedirs(DUMP_ROOT, exist_ok=True)

    chars = list_characters()
    print(f"[bulk] {len(chars)} characters to process; up to {PARALLEL} in parallel")
    print(f"[bulk] dump root: {DUMP_ROOT}")

    started = time.monotonic()
    results = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=PARALLEL) as pool:
        futures = {pool.submit(export_character, name): name for name in chars}
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            name, ok, msg = fut.result()
            done += 1
            tag = "OK  " if ok else "FAIL"
            print(f"[bulk] {done:>3}/{len(chars)} {tag} {name:30s} {msg}",
                  flush=True)
            results.append((name, ok, msg))

    total = time.monotonic() - started
    write_readme(results, total)
    succ = sum(1 for r in results if r[1])
    fail = len(results) - succ
    print()
    print(f"[bulk] DONE — {succ} succeeded, {fail} failed in {total/60:.1f} min")
    print(f"[bulk] dump:    {DUMP_ROOT}")
    print(f"[bulk] readme:  {os.path.join(DUMP_ROOT, 'README.md')}")
    if fail:
        print("[bulk] failed characters:")
        for name, _, msg in results:
            if not msg:
                continue
            if any(name == n for n, ok, _ in results if not ok):
                print(f"  - {name}: {msg}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
