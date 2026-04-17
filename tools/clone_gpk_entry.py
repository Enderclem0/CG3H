"""
Task #26 RE tool — clone a GPK entry under a new name (byte-identical).

Splits the "custom entry hangs on frame 2" hypothesis into two halves:
  1. If the clone renders fine under its new name → naming is NOT the trigger;
     the real hang is in builder-emitted custom mesh content.
  2. If the clone hangs like a real custom entry → naming IS the trigger;
     some name-keyed auxiliary table we haven't found yet.

Usage:
    python clone_gpk_entry.py <gpk_path> <src_entry> <dst_entry> [--inplace]

The output GPK replaces the input if --inplace; otherwise it's written next
to the input as <basename>.clone.gpk.  The clone's raw decompressed bytes
are a BIT-FOR-BIT duplicate of the source entry — so any GR2-internal
metadata (bone bindings, material bindings, shader tags) is identical.

After running, in-game (Lua console):
    rom.data.swap_to_variant("<src_entry>", "<dst_entry>")
and observe frame-2 behaviour.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gpk_pack import extract_gpk, pack_gpk, list_gpk


def clone(gpk_path: str, src: str, dst: str, out_path: str) -> None:
    entries = extract_gpk(gpk_path)
    if src not in entries:
        names = ", ".join(sorted(entries))
        raise KeyError(f"Entry {src!r} not in GPK. Available: {names}")
    if dst in entries:
        raise KeyError(f"Entry {dst!r} already exists — refusing to overwrite")
    entries[dst] = entries[src]
    pack_gpk(entries, out_path)
    print(f"Cloned {src!r} -> {dst!r} ({len(entries[src])} decompressed bytes)")
    print(f"Output: {out_path}")
    print(f"Entry count: {len(entries)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("gpk_path")
    ap.add_argument("src_entry")
    ap.add_argument("dst_entry")
    ap.add_argument("--inplace", action="store_true",
                    help="overwrite the input GPK (default: write alongside)")
    args = ap.parse_args()

    if args.inplace:
        out_path = args.gpk_path
    else:
        base, ext = os.path.splitext(args.gpk_path)
        out_path = f"{base}.clone{ext}"

    clone(args.gpk_path, args.src_entry, args.dst_entry, out_path)


if __name__ == "__main__":
    main()
