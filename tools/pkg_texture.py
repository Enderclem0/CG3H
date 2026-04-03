"""
Hades II PKG Texture Extractor/Replacer

Extracts and replaces textures from .pkg package files.

Usage:
    python pkg_texture.py list    <pkg_file>
    python pkg_texture.py extract <pkg_file> [--output-dir DIR]
    python pkg_texture.py replace <pkg_file> <texture_name> <new_dds> [--output FILE]

The extract command saves textures as raw .dds files (BC7 compressed).
The replace command swaps the texture data in the .pkg while keeping
the same format and dimensions.
"""

import argparse
import os
import struct
import sys

try:
    import lz4.block
except ImportError:
    sys.exit("ERROR: lz4 not installed. Run: pip install lz4")


# ── PKG format constants ──────────────────────────────────────────────────────

TEXTURE_FORMATS = {
    0x00: ('B8G8R8A8', 4),   # 4 bytes per pixel
    0x04: ('BC1', 0.5),       # 0.5 bytes per pixel (4bpp)
    0x05: ('BC2', 1),         # 1 byte per pixel (8bpp)
    0x06: ('BC3', 1),         # 1 byte per pixel (8bpp)
    0x0C: ('R8', 1),
    0x0E: ('R8G8B8A8', 4),
    0x1A: ('R8', 1),
    0x1C: ('BC7', 1),         # 1 byte per pixel (8bpp)
    0x1E: ('R8G8', 2),
    0x20: ('Native', 0),
}


# ── PKG reading ───────────────────────────────────────────────────────────────

def _swap32(v):
    """Byte-swap a 32-bit integer (big-endian ↔ little-endian)."""
    return (((v >> 8) & 0xFF00) | ((v & 0xFF00) << 8) |
            (v >> 24) | ((v << 24) & 0xFF000000))


def _read_7bit_int(data, off):
    """Read a 7-bit encoded integer (like .NET BinaryReader.Read7BitEncodedInt)."""
    result = 0
    shift = 0
    while True:
        b = data[off]
        off += 1
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            break
        shift += 7
    return result, off


def _read_csstring(data, off):
    """Read a length-prefixed string (7-bit encoded length + UTF-8 bytes)."""
    length, off = _read_7bit_int(data, off)
    s = data[off:off + length].decode('utf-8', 'replace')
    return s, off + length


def read_pkg_chunks(pkg_path):
    """
    Read a .pkg file and decompress all LZ4 chunks.
    Returns list of (chunk_bytes, chunk_file_offset) tuples.
    """
    with open(pkg_path, 'rb') as f:
        raw = f.read()

    header_raw = struct.unpack_from('<I', raw, 0)[0]
    header = _swap32(header_raw)
    version = header & 0xFFFF
    compressed = (header & 0x20000000) != 0

    if version < 5:
        raise ValueError(f"Unsupported PKG version {version}")

    chunks = []
    off = 4

    while off < len(raw):
        chunk_start = off
        flag = raw[off]
        off += 1

        if flag == 0:
            # Uncompressed — read remaining
            chunks.append((raw[off:], chunk_start))
            break

        if compressed:
            comp_size = _swap32(struct.unpack_from('<I', raw, off)[0])
            off += 4
            if comp_size > len(raw) - off:
                break
            decomp = lz4.block.decompress(raw[off:off + comp_size],
                                           uncompressed_size=0x2020020)
            chunks.append((decomp, chunk_start))
            off += comp_size
        else:
            # Read raw
            chunks.append((raw[off:], chunk_start))
            break

    return chunks, raw, header


def scan_textures(chunks):
    """
    Scan decompressed chunks for texture entries.
    Returns list of TextureInfo dicts.
    """
    textures = []

    for ci, (chunk, _) in enumerate(chunks):
        doff = 0
        while doff < len(chunk):
            tag = chunk[doff]
            doff += 1

            if tag == 0xFF or tag == 0xBE:
                break
            elif tag == 0xAD:  # Texture2D
                name, doff = _read_csstring(chunk, doff)
                total_sz = _swap32(struct.unpack_from('<I', chunk, doff)[0])
                data_start = doff + 4
                doff += 4

                # XNB header (10 bytes)
                xnb_off = doff
                if chunk[doff:doff + 3] == b'XNB':
                    xnb_version = chunk[doff + 4]
                    xnb_flags = chunk[doff + 5]
                    xnb_size = struct.unpack_from('<I', chunk, doff + 6)[0]
                    doff += 10

                    # Texture header
                    tex_fmt = struct.unpack_from('<I', chunk, doff)[0]
                    tex_w = struct.unpack_from('<I', chunk, doff + 4)[0]
                    tex_h = struct.unpack_from('<I', chunk, doff + 8)[0]
                    tex_d = struct.unpack_from('<I', chunk, doff + 12)[0]
                    tex_pix_sz = struct.unpack_from('<I', chunk, doff + 16)[0]
                    tex_data_off = doff + 20

                    fmt_name = TEXTURE_FORMATS.get(tex_fmt, (f'0x{tex_fmt:X}', 0))[0]

                    mip_count = _compute_mip_count(tex_w, tex_h, tex_fmt, tex_pix_sz)

                    textures.append({
                        'name': name,
                        'format': tex_fmt,
                        'format_name': fmt_name,
                        'width': tex_w,
                        'height': tex_h,
                        'depth': tex_d,
                        'pixel_size': tex_pix_sz,
                        'mip_count': mip_count,
                        'chunk_idx': ci,
                        'data_offset': tex_data_off,
                        'entry_offset': data_start - 4,
                        'xnb_offset': xnb_off,
                    })

                doff = data_start + total_sz

            elif tag == 0xAA:  # Texture3D
                name, doff = _read_csstring(chunk, doff)
                total_sz = struct.unpack_from('<i', chunk, doff)[0]
                doff += 4 + total_sz

            elif tag == 0xDE:  # Atlas
                total_sz = struct.unpack_from('<i', chunk, doff)[0]
                doff += 4 + total_sz

            elif tag == 0xCC:  # Include
                _, doff = _read_csstring(chunk, doff)

            elif tag == 0xBB:  # Bink
                # Complex format, skip by seeking
                break

            elif tag == 0xEE:  # Bink atlas
                total_sz = struct.unpack_from('<i', chunk, doff)[0]
                doff += 4 + total_sz

            else:
                break

    return textures


def extract_texture_raw(chunks, tex_info):
    """Extract raw pixel data for a texture."""
    chunk = chunks[tex_info['chunk_idx']][0]
    off = tex_info['data_offset']
    size = tex_info['pixel_size']
    return chunk[off:off + size]


def _compute_mip_count(width, height, fmt_code, pixel_size):
    """Compute how many mip levels are in the pixel data based on total size."""
    total = 0
    w, h = width, height
    mips = 0
    # BC formats: 16 bytes per 4x4 block. Uncompressed: bpp * w * h
    bpp = TEXTURE_FORMATS.get(fmt_code, ('', 1))[1]
    while w >= 4 and h >= 4:
        if fmt_code in (0x04, 0x05, 0x06, 0x1C):  # BCn
            blocks = max(w // 4, 1) * max(h // 4, 1)
            mip_size = blocks * 16 if fmt_code != 0x04 else blocks * 8
        else:
            mip_size = int(w * h * bpp)
        total += mip_size
        mips += 1
        if total >= pixel_size:
            break
        w //= 2
        h //= 2
    return mips


def build_dds_header(width, height, fmt_code, pixel_size):
    """Build a DDS header with correct mipmap count for the given texture."""
    mip_count = _compute_mip_count(width, height, fmt_code, pixel_size)

    # DDS magic
    header = bytearray(128)
    struct.pack_into('<I', header, 0, 0x20534444)  # "DDS "
    struct.pack_into('<I', header, 4, 124)  # header size
    # flags: CAPS | HEIGHT | WIDTH | PIXELFORMAT | MIPMAPCOUNT | LINEARSIZE
    flags = 0x1 | 0x2 | 0x4 | 0x1000
    if mip_count > 1:
        flags |= 0x20000  # DDSD_MIPMAPCOUNT
    struct.pack_into('<I', header, 8, flags)
    struct.pack_into('<I', header, 12, height)
    struct.pack_into('<I', header, 16, width)
    struct.pack_into('<I', header, 20, pixel_size)  # pitch or linear size
    struct.pack_into('<I', header, 28, mip_count)   # mipMapCount

    # Pixel format at offset 76
    struct.pack_into('<I', header, 76, 32)  # pf size

    if fmt_code == 0x1C:  # BC7
        struct.pack_into('<I', header, 80, 0x4)  # DDPF_FOURCC
        struct.pack_into('<4s', header, 84, b'DX10')
        # DX10 extended header
        dx10 = bytearray(20)
        struct.pack_into('<I', dx10, 0, 98)  # DXGI_FORMAT_BC7_UNORM
        struct.pack_into('<I', dx10, 4, 3)   # D3D10_RESOURCE_DIMENSION_TEXTURE2D
        struct.pack_into('<I', dx10, 8, 0)   # misc flags
        struct.pack_into('<I', dx10, 12, 1)  # array size
        struct.pack_into('<I', dx10, 16, 0)  # misc flags 2
        return bytes(header) + bytes(dx10)
    elif fmt_code == 0x06:  # BC3/DXT5
        struct.pack_into('<I', header, 80, 0x4)
        struct.pack_into('<4s', header, 84, b'DXT5')
    elif fmt_code == 0x04:  # BC1/DXT1
        struct.pack_into('<I', header, 80, 0x4)
        struct.pack_into('<4s', header, 84, b'DXT1')
    elif fmt_code in (0x00, 0x0E):  # BGRA/RGBA
        struct.pack_into('<I', header, 80, 0x41)  # DDPF_RGB | DDPF_ALPHAPIXELS
        struct.pack_into('<I', header, 88, 32)  # bits per pixel
        struct.pack_into('<I', header, 92, 0x00FF0000)  # R mask
        struct.pack_into('<I', header, 96, 0x0000FF00)  # G mask
        struct.pack_into('<I', header, 100, 0x000000FF)  # B mask
        struct.pack_into('<I', header, 104, 0xFF000000)  # A mask
    else:
        struct.pack_into('<I', header, 80, 0x4)
        struct.pack_into('<4s', header, 84, b'DX10')
        dx10 = bytearray(20)
        struct.pack_into('<I', dx10, 0, fmt_code)
        struct.pack_into('<I', dx10, 4, 3)
        struct.pack_into('<I', dx10, 12, 1)
        return bytes(header) + bytes(dx10)

    # Caps
    caps = 0x1000  # DDSCAPS_TEXTURE
    if mip_count > 1:
        caps |= 0x8 | 0x400000  # DDSCAPS_COMPLEX | DDSCAPS_MIPMAP
    struct.pack_into('<I', header, 108, caps)
    return bytes(header)


# ── CLI ───────────────────────────────────────────────────────────────────────

def cmd_list(args):
    chunks, _, header = read_pkg_chunks(args.pkg)
    textures = scan_textures(chunks)
    print(f"PKG: {args.pkg}")
    print(f"Header: 0x{_swap32(header):08X}  Chunks: {len(chunks)}")
    print(f"Textures: {len(textures)}")
    for t in textures:
        print(f"  {t['width']:>5}x{t['height']:<5}  {t['format_name']:6s}  "
              f"{t['mip_count']} mips  {t['pixel_size']:>12,} bytes  {t['name']}")


def cmd_extract(args):
    chunks, _, _ = read_pkg_chunks(args.pkg)
    textures = scan_textures(chunks)

    out_dir = args.output_dir or '.'
    os.makedirs(out_dir, exist_ok=True)

    for t in textures:
        pixels = extract_texture_raw(chunks, t)
        # Build DDS with header
        dds_header = build_dds_header(t['width'], t['height'], t['format'], t['pixel_size'])
        basename = os.path.basename(t['name']).replace('\\', '_').replace('/', '_')
        out_path = os.path.join(out_dir, f"{basename}.dds")
        with open(out_path, 'wb') as f:
            f.write(dds_header)
            f.write(pixels)
        print(f"  Extracted: {out_path} ({t['width']}x{t['height']} {t['format_name']} "
              f"{len(pixels):,} bytes)")

    print(f"\nExtracted {len(textures)} textures to {out_dir}")


def main():
    parser = argparse.ArgumentParser(description="Hades II PKG Texture Tool")
    sub = parser.add_subparsers(dest='command')

    p_list = sub.add_parser('list', help='List textures in a .pkg file')
    p_list.add_argument('pkg', help='.pkg file path')

    p_extract = sub.add_parser('extract', help='Extract textures as .dds files')
    p_extract.add_argument('pkg', help='.pkg file path')
    p_extract.add_argument('--output-dir', '-o', default=None, help='Output directory')

    args = parser.parse_args()
    if args.command == 'list':
        cmd_list(args)
    elif args.command == 'extract':
        cmd_extract(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
