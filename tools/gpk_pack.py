"""
Hades II GPK packer/unpacker.

Pure Python. Zero DLL dependency.

GPK Format (Format B — confirmed):
    [version:4LE=1][file_count:4LE]
    Per entry: [name_len:1][name:name_len UTF-8][compressed_size:4LE][lz4_data:compressed_size]

Usage:
    python gpk_pack.py list    <source.gpk>
    python gpk_pack.py extract <source.gpk>  [--output-dir DIR]
    python gpk_pack.py pack    <dir>         [--output FILE]
    python gpk_pack.py patch   <source.gpk>  <entry_name>  <new_file>  [--output FILE]

Quick round-trip test:
    python gpk_pack.py extract Melinoe.gpk -o /tmp/mel_out
    python gpk_pack.py pack    /tmp/mel_out -o /tmp/mel_rt.gpk
    python gpk_pack.py list    /tmp/mel_rt.gpk
"""
import argparse
import os
import re
import struct
import sys
import tempfile


# Mesh-entry classifier.  Two GPKs in the game use a non-standard
# suffix and a `endswith('_Mesh')` filter silently drops them:
#   Axe.gpk          ::  Melinoe_Axe_Mesh1
#   ToolFishRod.gpk  ::  ToolFishRod_AnimationMesh
# This regex catches `_Mesh`, `_Mesh<digits>`, `_AnimationMesh` and
# anything else ending in `Mesh\d*`.  Animation entries follow the
# `_<C|N>_<digits>` convention and never end in `Mesh`, so the
# inverse cleanly selects animations.
_MESH_ENTRY_RE = re.compile(r'Mesh\d*$')


def is_mesh_entry(name):
    """True if a GPK entry name represents a mesh (not an animation)."""
    return bool(_MESH_ENTRY_RE.search(name))

try:
    import lz4.block
except ImportError:
    sys.exit("ERROR: lz4 not installed. Run: pip install lz4")


# ── Core functions ────────────────────────────────────────────────────────────

def extract_gpk(gpk_path: str) -> dict[str, bytes]:
    """Decompress all entries. Returns {entry_name: raw_decompressed_bytes}."""
    with open(gpk_path, 'rb') as f:
        raw = f.read()
    version = struct.unpack_from('<I', raw, 0)[0]
    if version != 1:
        raise ValueError(f"Unsupported GPK version {version} in {gpk_path!r}")
    count = struct.unpack_from('<I', raw, 4)[0]
    off = 8
    result = {}
    for _ in range(count):
        nl = raw[off]
        name = raw[off + 1: off + 1 + nl].decode('utf-8')
        off += 1 + nl
        cs = struct.unpack_from('<I', raw, off)[0]
        off += 4
        result[name] = lz4.block.decompress(raw[off: off + cs], uncompressed_size=128 * 1024 * 1024)
        off += cs
    return result


def extract_gpk_raw(gpk_path: str) -> dict[str, bytes]:
    """Return {entry_name: raw_lz4_compressed_bytes} without decompressing.

    The bytes returned are the on-disk lz4 payload exactly as the game
    expects to read them from a standalone ``.gr2.lz4`` file via
    ``OpenOptimizedGrannyFile``.  Used by the v3.8 hot-reload pipeline
    to materialize per-entry standalone files alongside the merged GPK,
    so the game's per-entry loader can find them through H2M's file
    redirect.
    """
    with open(gpk_path, 'rb') as f:
        raw = f.read()
    version = struct.unpack_from('<I', raw, 0)[0]
    if version != 1:
        raise ValueError(f"Unsupported GPK version {version} in {gpk_path!r}")
    count = struct.unpack_from('<I', raw, 4)[0]
    off = 8
    result = {}
    for _ in range(count):
        nl = raw[off]
        name = raw[off + 1: off + 1 + nl].decode('utf-8')
        off += 1 + nl
        cs = struct.unpack_from('<I', raw, off)[0]
        off += 4
        result[name] = raw[off: off + cs]
        off += cs
    return result


def list_gpk(gpk_path: str) -> list[tuple[str, int]]:
    """Return [(name, compressed_size)] without decompressing."""
    with open(gpk_path, 'rb') as f:
        raw = f.read()
    version = struct.unpack_from('<I', raw, 0)[0]
    if version != 1:
        raise ValueError(f"Unsupported GPK version {version} in {gpk_path!r}")
    count = struct.unpack_from('<I', raw, 4)[0]
    off = 8
    entries = []
    for _ in range(count):
        nl = raw[off]
        name = raw[off + 1: off + 1 + nl].decode('utf-8')
        off += 1 + nl
        cs = struct.unpack_from('<I', raw, off)[0]
        off += 4
        entries.append((name, cs))
        off += cs
    return entries


def pack_gpk(entries: dict[str, bytes], out_path: str) -> None:
    """
    Write a new GPK from a dict of {entry_name: raw_decompressed_bytes}.

    Entries are written in iteration order.
    """
    with open(out_path, 'wb') as f:
        f.write(struct.pack('<II', 1, len(entries)))
        for name, raw in entries.items():
            compressed = lz4.block.compress(raw, mode='high_compression', store_size=False)
            name_bytes = name.encode('utf-8')
            if len(name_bytes) > 255:
                raise ValueError(f"Entry name too long (> 255 bytes UTF-8): {name!r}")
            f.write(struct.pack('<B', len(name_bytes)))
            f.write(name_bytes)
            f.write(struct.pack('<I', len(compressed)))
            f.write(compressed)


def patch_gpk(
    gpk_path: str,
    replacements: dict[str, bytes],
    out_path: str,
) -> None:
    """
    Load an existing GPK, replace named entries with new raw bytes, write a new GPK.

    Unmodified entries are decompressed then recompressed for format consistency.
    Raises KeyError if any replacement name is not present in the source GPK.
    """
    existing = extract_gpk(gpk_path)
    missing = [n for n in replacements if n not in existing]
    if missing:
        raise KeyError(
            f"Entry/entries not found in {gpk_path!r}: {missing}. "
            f"Available: {sorted(existing.keys())}"
        )
    for name, new_bytes in replacements.items():
        existing[name] = new_bytes
    pack_gpk(existing, out_path)


# ── CLI commands ──────────────────────────────────────────────────────────────

def cmd_list(args):
    entries = list_gpk(args.gpk)
    total_c = sum(cs for _, cs in entries)
    print(f"{args.gpk}  ({len(entries)} entries, {total_c:,} bytes compressed)")
    print(f"\n  {'Name':<42}  {'Compressed':>12}")
    print('  ' + '-' * 57)
    for name, cs in entries:
        print(f"  {name:<42}  {cs:>12,}")


def cmd_extract(args):
    entries = extract_gpk(args.gpk)
    out_dir = args.output_dir or (os.path.splitext(args.gpk)[0] + '_extracted')
    os.makedirs(out_dir, exist_ok=True)
    for name, data in entries.items():
        out_path = os.path.join(out_dir, name)
        with open(out_path, 'wb') as f:
            f.write(data)
        print(f"  {name}  ({len(data):,} bytes)")
    print(f"\nExtracted {len(entries)} entries to {out_dir!r}")


def cmd_pack(args):
    src_dir = args.dir.rstrip('/\\')
    if not os.path.isdir(src_dir):
        sys.exit(f"ERROR: Not a directory: {src_dir!r}")
    files = sorted(f for f in os.listdir(src_dir) if os.path.isfile(os.path.join(src_dir, f)))
    if not files:
        sys.exit(f"ERROR: No files found in {src_dir!r}")
    entries = {}
    for fname in files:
        with open(os.path.join(src_dir, fname), 'rb') as f:
            entries[fname] = f.read()
    out_path = args.output or (src_dir + '.gpk')
    pack_gpk(entries, out_path)
    total_raw = sum(len(v) for v in entries.values())
    print(f"Packed {len(entries)} entries ({total_raw:,} raw bytes) -> {out_path!r}")


def cmd_patch(args):
    if not os.path.isfile(args.new_file):
        sys.exit(f"ERROR: Replacement file not found: {args.new_file!r}")
    with open(args.new_file, 'rb') as f:
        new_bytes = f.read()
    out_path = args.output or args.gpk.replace('.gpk', '_patched.gpk')
    patch_gpk(args.gpk, {args.entry_name: new_bytes}, out_path)
    print(f"Patched {args.entry_name!r} ({len(new_bytes):,} bytes) -> {out_path!r}")


def cmd_roundtrip_test(args):
    """Internal self-test: extract → pack → re-extract, verify all entries match."""
    entries_a = extract_gpk(args.gpk)
    tmp = tempfile.mktemp(suffix='.gpk')
    try:
        pack_gpk(entries_a, tmp)
        entries_b = extract_gpk(tmp)
        if set(entries_a) != set(entries_b):
            sys.exit("FAIL: entry name sets differ")
        mismatches = [k for k in entries_a if entries_a[k] != entries_b[k]]
        if mismatches:
            sys.exit(f"FAIL: content mismatch for entries: {mismatches}")
        print(f"PASS  {len(entries_a)} entries round-tripped correctly")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ── Argument parser ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Hades II GPK archive tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('list', help='List entries in a GPK')
    p.add_argument('gpk', help='.gpk file')

    p = sub.add_parser('extract', help='Extract all entries from a GPK')
    p.add_argument('gpk', help='.gpk file')
    p.add_argument('--output-dir', '-o', help='Output directory (default: <gpk>_extracted/)')

    p = sub.add_parser('pack', help='Pack a directory of files into a GPK')
    p.add_argument('dir', help='Directory of files to pack')
    p.add_argument('--output', '-o', help='Output .gpk path (default: <dir>.gpk)')

    p = sub.add_parser('patch', help='Replace one entry in an existing GPK')
    p.add_argument('gpk', help='Source .gpk file')
    p.add_argument('entry_name', help='Name of the entry to replace')
    p.add_argument('new_file', help='Path to the replacement file')
    p.add_argument('--output', '-o', help='Output .gpk (default: <gpk>_patched.gpk)')

    p = sub.add_parser('test', help='Round-trip self-test: extract/pack/compare')
    p.add_argument('gpk', help='.gpk file to test')

    args = parser.parse_args()
    {
        'list':    cmd_list,
        'extract': cmd_extract,
        'pack':    cmd_pack,
        'patch':   cmd_patch,
        'test':    cmd_roundtrip_test,
    }[args.cmd](args)


if __name__ == '__main__':
    main()
