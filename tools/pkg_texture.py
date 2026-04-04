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

            elif tag == 0xAA:  # Texture3D (same format as Tex2D, big-endian size)
                name, doff = _read_csstring(chunk, doff)
                total_sz = _swap32(struct.unpack_from('<I', chunk, doff)[0])
                data_start = doff + 4
                doff += 4

                # Parse XNB-wrapped texture data (same as 0xAD)
                if chunk[doff:doff + 3] == b'XNB':
                    doff += 10
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
                        'xnb_offset': data_start,
                    })

                doff = data_start + total_sz

            elif tag == 0xDE:  # Atlas (character textures)
                atlas_start = doff
                total_sz = struct.unpack_from('<i', chunk, doff)[0]
                doff += 4
                atlas_end = atlas_start + 4 + total_sz

                # Parse atlas header
                if doff + 12 <= atlas_end:
                    magic = struct.unpack_from('<I', chunk, doff)[0]
                    atlas_doff = doff
                    version = 0
                    if magic == 0x7FB1776B:
                        version = struct.unpack_from('<I', chunk, doff + 4)[0]
                        sub_count = struct.unpack_from('<I', chunk, doff + 8)[0]
                        atlas_doff = doff + 12
                    else:
                        sub_count = struct.unpack_from('<I', chunk, doff)[0]
                        atlas_doff = doff + 4

                    # Skip sub-entries (sprite regions)
                    atlas_name = None
                    for _ in range(sub_count):
                        if atlas_doff >= atlas_end:
                            break
                        ename, atlas_doff = _read_csstring(chunk, atlas_doff)
                        if atlas_name is None:
                            # Use first sub-entry name as atlas name
                            atlas_name = ename
                        atlas_doff += 4 * 10  # w,h,ox,oy,rx,ry,rw,rh + scaleX,scaleY
                        if version > 0 and atlas_doff < atlas_end:
                            atlas_doff += 1  # flags byte
                        if version > 2 and atlas_doff + 4 <= atlas_end:
                            hull_count = struct.unpack_from('<I', chunk, atlas_doff)[0]
                            atlas_doff += 4 + hull_count * 8  # (x,y) pairs

                    # After sub-entries: check for inline texture (0xDD)
                    if atlas_doff < atlas_end:
                        marker = chunk[atlas_doff]
                        atlas_doff += 1
                        if marker == 0xDD:
                            # Inline texture: name + texture data (no XNB wrapper)
                            tex_name, atlas_doff = _read_csstring(chunk, atlas_doff)
                            if atlas_doff + 20 <= atlas_end:
                                tex_fmt = struct.unpack_from('<I', chunk, atlas_doff)[0]
                                tex_w = struct.unpack_from('<I', chunk, atlas_doff + 4)[0]
                                tex_h = struct.unpack_from('<I', chunk, atlas_doff + 8)[0]
                                tex_d = struct.unpack_from('<I', chunk, atlas_doff + 12)[0]
                                tex_pix_sz = struct.unpack_from('<I', chunk, atlas_doff + 16)[0]
                                tex_data_off = atlas_doff + 20

                                fmt_name = TEXTURE_FORMATS.get(tex_fmt, (f'0x{tex_fmt:X}', 0))[0]
                                mip_count = _compute_mip_count(tex_w, tex_h, tex_fmt, tex_pix_sz)

                                textures.append({
                                    'name': tex_name,
                                    'format': tex_fmt,
                                    'format_name': fmt_name,
                                    'width': tex_w,
                                    'height': tex_h,
                                    'depth': tex_d,
                                    'pixel_size': tex_pix_sz,
                                    'mip_count': mip_count,
                                    'chunk_idx': ci,
                                    'data_offset': tex_data_off,
                                    'entry_offset': atlas_start,
                                    'xnb_offset': None,
                                    'atlas': True,
                                })

                doff = atlas_end

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


def png_to_dds(png_path, fmt_code, width, height, mip_count):
    """
    Compress a PNG image to DDS with BCn compression and mipmaps.
    Uses etcpak for BC7/BC3/BC1 encoding.
    Returns DDS file bytes (header + all mip levels).
    """
    from PIL import Image
    try:
        import etcpak
    except ImportError:
        raise ImportError("etcpak not installed. Run: pip install etcpak")

    img = Image.open(png_path).convert('RGBA')
    if img.size != (width, height):
        print(f"  Resizing {img.size} -> ({width}, {height})")
        img = img.resize((width, height), Image.LANCZOS)

    # Generate mip chain
    pixel_data = b''
    w, h = width, height
    for mip in range(mip_count):
        if w < 4 or h < 4:
            break
        mip_img = img.resize((w, h), Image.LANCZOS) if (w, h) != img.size else img
        rgba = mip_img.tobytes()

        if fmt_code == 0x1C:  # BC7
            compressed = etcpak.compress_bc7(rgba, w, h)
        elif fmt_code == 0x06:  # BC3/DXT5
            compressed = etcpak.compress_bc3(rgba, w, h)
        elif fmt_code == 0x04:  # BC1/DXT1
            compressed = etcpak.compress_bc1(rgba, w, h)
        elif fmt_code in (0x00, 0x0E):  # Uncompressed RGBA
            compressed = rgba
        else:
            raise ValueError(f"Unsupported format 0x{fmt_code:X} for PNG compression")

        pixel_data += compressed
        w //= 2
        h //= 2

    header = build_dds_header(width, height, fmt_code, len(pixel_data))
    return header + pixel_data


def _update_pkg_checksum(pkg_path):
    """Recompute XXH64 hash for a .pkg file and update checksums.txt."""
    try:
        import xxhash
    except ImportError:
        print("  WARNING: xxhash not installed, cannot update checksums.txt. "
              "Run: pip install xxhash")
        return

    pkg_dir = os.path.dirname(pkg_path)
    pkg_name = os.path.basename(pkg_path)
    checksums_path = os.path.join(pkg_dir, 'checksums.txt')

    if not os.path.isfile(checksums_path):
        print(f"  WARNING: checksums.txt not found in {pkg_dir}")
        return

    # Compute new hash
    h = xxhash.xxh64()
    with open(pkg_path, 'rb') as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    new_hash = h.hexdigest()

    # Read and update checksums.txt
    with open(checksums_path, 'r') as f:
        lines = f.readlines()

    updated = False
    for i, line in enumerate(lines):
        parts = line.strip().split('  ', 1)
        if len(parts) == 2 and parts[1] == pkg_name:
            lines[i] = f"{new_hash}  {pkg_name}\n"
            updated = True
            # Don't break — there might be duplicates

    if updated:
        with open(checksums_path, 'w') as f:
            f.writelines(lines)
        print(f"  Updated checksums.txt: {pkg_name} -> {new_hash}")
    else:
        print(f"  WARNING: {pkg_name} not found in checksums.txt")


def find_replacement_targets(pkg_dir, width, height, fmt, count=5):
    """
    Find existing texture entries that can be replaced with a custom texture.
    Prefers exact dimension match, then same pixel_size.
    Returns list of (base_name, info_dict) sorted by best match first.
    """
    index = load_texture_index(pkg_dir)
    if not index:
        return []

    exact = []
    compatible = []
    for key, info in index.items():
        if info['format'] != fmt:
            continue
        if info['width'] == width and info['height'] == height:
            exact.append((key, info))
        elif info['pixel_size'] >= width * height:  # roughly enough space
            compatible.append((key, info))

    # Prefer exact dimension matches, sorted by name (deterministic)
    exact.sort(key=lambda x: x[0])
    compatible.sort(key=lambda x: x[1]['pixel_size'])
    return (exact + compatible)[:count]


def install_custom_texture(pkg_dir, texture_name, png_path_or_bytes, width, height,
                           fmt=0x1C, mip_count=6, target_pkg=None,
                           max_size=512):
    """
    Install a custom texture by adding a NEW entry to a .pkg file.
    texture_name: base name for the entry (e.g. 'MyCustomTexture')
    png_path_or_bytes: path to PNG file or raw PNG bytes
    target_pkg: specific .pkg to add to (default: Fx.pkg)
    max_size: max texture dimension (default 512 to match game standard)
    Returns the texture base name on success, or None on failure.
    """
    import tempfile

    # Clamp dimensions to game standard
    if width > max_size or height > max_size:
        print(f"  Resizing {width}x{height} -> {max_size}x{max_size}")
        width = min(width, max_size)
        height = min(height, max_size)

    # Compress PNG to DDS
    if isinstance(png_path_or_bytes, bytes):
        tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
        tmp.write(png_path_or_bytes)
        tmp.close()
        png_path = tmp.name
        cleanup = True
    else:
        png_path = png_path_or_bytes
        cleanup = False

    try:
        dds_bytes = png_to_dds(png_path, fmt, width, height, mip_count)
    finally:
        if cleanup:
            os.unlink(png_path)

    # Write temp DDS
    dds_tmp = tempfile.NamedTemporaryFile(suffix='.dds', delete=False)
    dds_tmp.write(dds_bytes)
    dds_tmp.close()

    try:
        # Add to target .pkg (default: Fx.pkg — always loaded)
        if target_pkg is None:
            target_pkg = 'Fx.pkg'
        pkg_path = os.path.join(pkg_dir, target_pkg)
        if not os.path.isfile(pkg_path):
            print(f"  ERROR: {target_pkg} not found")
            return None

        entry_name = f"GR2\\{texture_name}"
        if add_texture_entry(pkg_path, entry_name, dds_tmp.name, pkg_path):
            print(f"  Added custom texture '{texture_name}' to {target_pkg}")
            return texture_name
        else:
            print(f"  ERROR: Failed to add texture to {target_pkg}")
            return None
    finally:
        os.unlink(dds_tmp.name)


def add_texture_entry(pkg_path, entry_name, dds_path, output_path=None):
    """
    Add a NEW texture entry to a .pkg file.
    Inserts before the chunk terminator (0xBE/0xFF).
    entry_name: full path like 'GR2\\MyCustomTexture'
    Returns True on success.
    """
    if output_path is None:
        output_path = pkg_path

    # Read DDS
    with open(dds_path, 'rb') as f:
        dds_raw = f.read()
    if dds_raw[:4] != b'DDS ':
        raise ValueError(f"Not a DDS file: {dds_path}")
    dds_header_size = 128
    if dds_raw[84:88] == b'DX10':
        dds_header_size += 20
    dds_w = struct.unpack_from('<I', dds_raw, 16)[0]
    dds_h = struct.unpack_from('<I', dds_raw, 12)[0]
    dds_mips = struct.unpack_from('<I', dds_raw, 28)[0]
    pixel_data = dds_raw[dds_header_size:]

    # Detect format from DDS
    fourcc = dds_raw[84:88]
    if fourcc == b'DX10':
        dxgi = struct.unpack_from('<I', dds_raw, 128)[0]
        if dxgi == 98:
            fmt = 0x1C  # BC7
        else:
            fmt = dxgi
    elif fourcc == b'DXT5':
        fmt = 0x06
    elif fourcc == b'DXT1':
        fmt = 0x04
    else:
        fmt = 0x1C  # default BC7

    # Build the new entry bytes (no XNB wrapper for Tex2D entries... wait, they need XNB)
    # Entry format: tag(1) + CSString(name) + BE_size(4) + XNB(10) + tex_header(20) + pixels

    # CSString: 7-bit length + raw bytes
    name_bytes = entry_name.encode('utf-8')
    if len(name_bytes) < 128:
        cs_string = bytes([len(name_bytes)]) + name_bytes
    else:
        # 7-bit encoding for longer strings
        n = len(name_bytes)
        cs = bytearray()
        while n >= 128:
            cs.append((n & 0x7F) | 0x80)
            n >>= 7
        cs.append(n)
        cs_string = bytes(cs) + name_bytes

    # XNB header (10 bytes)
    xnb_content_size = 20 + len(pixel_data)  # tex header + pixels
    xnb_total = 10 + xnb_content_size
    xnb_header = b'XNBw\x06\x00' + struct.pack('<I', xnb_total)

    # Texture header (20 bytes): fmt, w, h, depth=1, pixel_size
    tex_header = struct.pack('<IIIII', fmt, dds_w, dds_h, 1, len(pixel_data))

    # Total data size (for the BE size field)
    total_data = len(xnb_header) + len(tex_header) + len(pixel_data)

    # Build complete entry
    entry = bytearray()
    entry.append(0xAD)  # Tex2D tag
    entry.extend(cs_string)
    entry.extend(struct.pack('<I', _swap32(total_data)))  # big-endian size
    entry.extend(xnb_header)
    entry.extend(tex_header)
    entry.extend(pixel_data)

    # Read and decompress the .pkg
    with open(pkg_path, 'rb') as f:
        raw = bytearray(f.read())

    header_raw = struct.unpack_from('<I', raw, 0)[0]
    header = _swap32(header_raw)
    compressed = (header & 0x20000000) != 0

    # Find the LAST chunk and insert before its terminator
    chunks = []
    off = 4
    while off < len(raw):
        flag_off = off
        flag = raw[off]; off += 1
        if flag == 0:
            break
        comp_size = _swap32(struct.unpack_from('<I', raw, off)[0])
        size_off = off
        off += 4
        data_off = off
        decomp = lz4.block.decompress(raw[data_off:data_off + comp_size],
                                       uncompressed_size=0x2020020)
        chunks.append((bytearray(decomp), flag_off, size_off, data_off, comp_size))
        off += comp_size

    if not chunks:
        print("  ERROR: No chunks in .pkg")
        return False

    # Use the last chunk — insert before terminator
    chunk, flag_off, size_off, data_off, orig_comp_size = chunks[-1]

    # Find the terminator (0xBE or 0xFF) — scan from end
    insert_pos = len(chunk)
    for i in range(len(chunk) - 1, -1, -1):
        if chunk[i] in (0xBE, 0xFF):
            insert_pos = i
            break

    # Insert the new entry before the terminator
    chunk[insert_pos:insert_pos] = entry
    print(f"  Inserted {len(entry):,} byte entry at chunk offset {insert_pos:,}")

    # Recompress
    new_comp = lz4.block.compress(bytes(chunk), store_size=False)
    new_comp_size = len(new_comp)
    print(f"  Recompressed: {orig_comp_size:,} -> {new_comp_size:,} bytes")

    # Rebuild the pkg
    new_size_be = _swap32(new_comp_size)
    output = bytearray()
    output += raw[:flag_off]
    output += bytes([raw[flag_off]])
    output += struct.pack('<I', new_size_be)
    output += new_comp
    after_off = data_off + orig_comp_size
    output += raw[after_off:]

    with open(output_path, 'wb') as f:
        f.write(output)
    print(f"  Written: {output_path} ({len(output):,} bytes)")

    _update_pkg_checksum(output_path)
    return True


def build_standalone_pkg(textures, output_path, output_manifest_path=None):
    """
    Build a standalone .pkg file from scratch containing only the given textures.
    textures: list of dicts with {name, dds_path} or {name, png_path, width, height, fmt, mip_count}
    output_path: path for the .pkg file
    output_manifest_path: path for the .pkg_manifest (optional, auto-derived if None)
    Returns True on success.
    """
    import tempfile

    # Build the chunk data: sequence of 0xAD entries + 0xFF terminator
    chunk = bytearray()

    for tex in textures:
        entry_name = tex['name']
        dds_path = tex.get('dds_path')

        # Compress PNG to DDS if no DDS provided
        if not dds_path or not os.path.isfile(dds_path):
            png_path = tex.get('png_path', '')
            if not png_path or not os.path.isfile(png_path):
                print(f"  WARNING: No DDS or PNG for '{entry_name}', skipping")
                continue
            fmt = tex.get('fmt', 0x1C)
            w = tex.get('width', 512)
            h = tex.get('height', 512)
            mips = tex.get('mip_count', 6)
            dds_bytes = png_to_dds(png_path, fmt, w, h, mips)
            tmp = tempfile.NamedTemporaryFile(suffix='.dds', delete=False)
            tmp.write(dds_bytes)
            tmp.close()
            dds_path = tmp.name

        # Read DDS
        with open(dds_path, 'rb') as f:
            dds_raw = f.read()
        if dds_raw[:4] != b'DDS ':
            print(f"  WARNING: '{dds_path}' is not a valid DDS, skipping")
            continue

        dds_header_size = 128
        if dds_raw[84:88] == b'DX10':
            dds_header_size += 20
        pixel_data = dds_raw[dds_header_size:]
        dds_w = struct.unpack_from('<I', dds_raw, 16)[0]
        dds_h = struct.unpack_from('<I', dds_raw, 12)[0]

        # Detect format
        fourcc = dds_raw[84:88]
        if fourcc == b'DX10':
            dxgi = struct.unpack_from('<I', dds_raw, 128)[0]
            fmt = 0x1C if dxgi == 98 else dxgi
        elif fourcc == b'DXT5':
            fmt = 0x06
        elif fourcc == b'DXT1':
            fmt = 0x04
        else:
            fmt = 0x1C

        # CSString: 7-bit length + raw bytes
        name_bytes = entry_name.encode('utf-8')
        if len(name_bytes) < 128:
            cs_string = bytes([len(name_bytes)]) + name_bytes
        else:
            n = len(name_bytes)
            cs = bytearray()
            while n >= 128:
                cs.append((n & 0x7F) | 0x80)
                n >>= 7
            cs.append(n)
            cs_string = bytes(cs) + name_bytes

        # XNB header
        xnb_content_size = 20 + len(pixel_data)
        xnb_total = 10 + xnb_content_size
        xnb_header = b'XNBw\x06\x00' + struct.pack('<I', xnb_total)

        # Texture header
        tex_header = struct.pack('<IIIII', fmt, dds_w, dds_h, 1, len(pixel_data))

        # Total data size (big-endian)
        total_data = len(xnb_header) + len(tex_header) + len(pixel_data)

        # Write entry
        chunk.append(0xAD)
        chunk.extend(cs_string)
        chunk.extend(struct.pack('<I', _swap32(total_data)))
        chunk.extend(xnb_header)
        chunk.extend(tex_header)
        chunk.extend(pixel_data)

        print(f"  Added: {entry_name} ({dds_w}x{dds_h} fmt=0x{fmt:X} "
              f"{len(pixel_data):,} bytes)")

    # Terminator
    chunk.append(0xFF)

    # Compress chunk
    comp = lz4.block.compress(bytes(chunk), store_size=False)

    # Build .pkg: header + one compressed chunk
    # Header: version 7 + compressed flag = 0x20000007
    pkg = bytearray()
    pkg.extend(struct.pack('<I', _swap32(0x20000007)))
    pkg.append(0x01)  # flag: compressed
    pkg.extend(struct.pack('<I', _swap32(len(comp))))
    pkg.extend(comp)

    with open(output_path, 'wb') as f:
        f.write(pkg)
    print(f"  Built: {output_path} ({len(pkg):,} bytes, {len(textures)} texture(s))")

    # Build .pkg_manifest (minimal — just needs to exist for H2M)
    if output_manifest_path is None:
        output_manifest_path = output_path + '_manifest'

    # Minimal manifest: header + empty
    # The game's manifest format is complex, but H2M just checks file existence
    # Write a minimal valid manifest
    manifest = bytearray()
    manifest.extend(struct.pack('<I', _swap32(0x20000007)))  # same header
    with open(output_manifest_path, 'wb') as f:
        f.write(manifest)
    print(f"  Manifest: {output_manifest_path}")

    return True


def replace_texture(pkg_path, texture_name, new_dds_path, output_path):
    """
    Replace a texture in a .pkg file with data from a DDS file.
    The DDS must have the same dimensions and format as the original.
    """
    # Read the DDS file — skip header, get raw pixel data
    with open(new_dds_path, 'rb') as f:
        dds_raw = f.read()

    # Parse DDS header to get pixel data offset and mip info
    if dds_raw[:4] != b'DDS ':
        raise ValueError(f"Not a DDS file: {new_dds_path}")
    dds_header_size = 128
    pf_fourcc = dds_raw[84:88]
    if pf_fourcc == b'DX10':
        dds_header_size += 20
    dds_w = struct.unpack_from('<I', dds_raw, 16)[0]
    dds_h = struct.unpack_from('<I', dds_raw, 12)[0]
    dds_mips = struct.unpack_from('<I', dds_raw, 28)[0]
    new_pixels = dds_raw[dds_header_size:]

    # Read the pkg
    with open(pkg_path, 'rb') as f:
        raw = bytearray(f.read())

    header_raw = struct.unpack_from('<I', raw, 0)[0]
    header = _swap32(header_raw)
    compressed = (header & 0x20000000) != 0

    # Parse and decompress all chunks, keeping track of file offsets
    chunks = []  # (decompressed_bytes, flag_offset, size_offset, data_offset, comp_size)
    off = 4
    while off < len(raw):
        flag_off = off
        flag = raw[off]; off += 1
        if flag == 0:
            break
        comp_size = _swap32(struct.unpack_from('<I', raw, off)[0])
        size_off = off
        off += 4
        data_off = off
        decomp = lz4.block.decompress(raw[data_off:data_off + comp_size],
                                       uncompressed_size=0x2020020)
        chunks.append((bytearray(decomp), flag_off, size_off, data_off, comp_size))
        off += comp_size

    # Search for the target texture in decompressed chunks
    target_lower = texture_name.lower().replace('\\', '/').replace('..', '')
    found = False

    for ci, (chunk, flag_off, size_off, data_off, orig_comp_size) in enumerate(chunks):
        doff = 0
        while doff < len(chunk) - 5:
            tag = chunk[doff]; doff += 1
            if tag in (0xFF, 0xBE):
                break
            if tag == 0xAD:  # Tex2D
                name, doff = _read_csstring(chunk, doff)
                total_sz = _swap32(struct.unpack_from('<I', chunk, doff)[0])
                total_sz_off = doff
                doff += 4
                data_start = doff

                name_lower = name.lower().replace('\\', '/').replace('..', '')
                filename = name_lower.split('/')[-1]

                if target_lower in filename or filename in target_lower:
                    # Found the texture — check XNB header
                    if chunk[doff:doff+3] != b'XNB':
                        print(f"  ERROR: No XNB header at expected position")
                        return False

                    tex_header_off = doff + 10  # after XNB header
                    orig_fmt = struct.unpack_from('<I', chunk, tex_header_off)[0]
                    orig_w = struct.unpack_from('<I', chunk, tex_header_off + 4)[0]
                    orig_h = struct.unpack_from('<I', chunk, tex_header_off + 8)[0]
                    orig_pix_sz = struct.unpack_from('<I', chunk, tex_header_off + 16)[0]
                    pixel_data_off = tex_header_off + 20

                    print(f"  Found: {name} ({orig_w}x{orig_h} fmt=0x{orig_fmt:X} "
                          f"{orig_pix_sz:,} bytes) in chunk {ci}")

                    # Validate dimensions
                    if dds_w != orig_w or dds_h != orig_h:
                        print(f"  ERROR: DDS dimensions mismatch: "
                              f"new={dds_w}x{dds_h} vs original={orig_w}x{orig_h}")
                        return False

                    # Auto-truncate if DDS has extra mip levels
                    if len(new_pixels) > orig_pix_sz:
                        orig_mips = _compute_mip_count(orig_w, orig_h, orig_fmt, orig_pix_sz)
                        print(f"  DDS has {dds_mips} mips, game expects {orig_mips} "
                              f"— truncating {len(new_pixels):,} -> {orig_pix_sz:,} bytes")
                        new_pixels = new_pixels[:orig_pix_sz]
                    elif len(new_pixels) < orig_pix_sz:
                        print(f"  ERROR: DDS pixel data too small: "
                              f"new={len(new_pixels):,} vs original={orig_pix_sz:,}")
                        print(f"  DDS needs at least {_compute_mip_count(orig_w, orig_h, orig_fmt, orig_pix_sz)} mip levels")
                        return False

                    # Replace pixel data in the decompressed chunk
                    chunk[pixel_data_off:pixel_data_off + orig_pix_sz] = new_pixels
                    print(f"  Replaced {orig_pix_sz:,} bytes of pixel data")

                    # Recompress the modified chunk
                    new_comp = lz4.block.compress(bytes(chunk), store_size=False)
                    new_comp_size = len(new_comp)
                    print(f"  Recompressed: {orig_comp_size:,} -> {new_comp_size:,} bytes")

                    # Rebuild the pkg file
                    # Everything before this chunk's data stays the same
                    # Replace: flag(1) + size(4) + compressed_data
                    new_size_be = _swap32(new_comp_size)

                    output = bytearray()
                    output += raw[:flag_off]                    # everything before this chunk
                    output += bytes([raw[flag_off]])             # flag byte (unchanged)
                    output += struct.pack('<I', new_size_be)     # new size (big-endian)
                    output += new_comp                           # new compressed data

                    # Everything after this chunk
                    after_off = data_off + orig_comp_size
                    output += raw[after_off:]

                    with open(output_path, 'wb') as f:
                        f.write(output)
                    print(f"  Written: {output_path} ({len(output):,} bytes)")

                    # Update XXH64 checksum in checksums.txt
                    _update_pkg_checksum(output_path)
                    return True

                doff = data_start + total_sz
            elif tag == 0xDE:
                sz = struct.unpack_from('<i', chunk, doff)[0]; doff += 4 + sz
            elif tag == 0xAA:
                nl, doff = _read_7bit_int(chunk, doff); doff += nl
                sz = _swap32(struct.unpack_from('<I', chunk, doff)[0]); doff += 4 + sz
            else:
                break

    if not found:
        print(f"  Texture '{texture_name}' not found in {pkg_path}")
    return False


# ── Texture index (for fast batch lookups) ────────────────────────────────────

def build_texture_index(pkg_dir):
    """
    Scan all .pkg files in pkg_dir and build an index mapping
    texture base name (lowercase) → {pkg_path, name, format, width, height, pixel_size}.
    Returns the index dict.
    """
    index = {}
    pkg_files = sorted(f for f in os.listdir(pkg_dir)
                       if f.endswith('.pkg') and os.path.isfile(os.path.join(pkg_dir, f)))
    for pkg_name in pkg_files:
        pkg_path = os.path.join(pkg_dir, pkg_name)
        try:
            chunks, _, _ = read_pkg_chunks(pkg_path)
        except Exception:
            continue
        textures = scan_textures(chunks)
        for t in textures:
            # Key: lowercase base filename (last path component, no extension)
            fn = t['name'].replace('\\', '/').split('/')[-1]
            fn_base = fn.rsplit('.', 1)[0] if '.' in fn else fn
            key = fn_base.lower()
            if key not in index:
                index[key] = {
                    'pkg': pkg_name,
                    'pkgs': [pkg_name],
                    'name': t['name'],
                    'format': t['format'],
                    'width': t['width'],
                    'height': t['height'],
                    'pixel_size': t['pixel_size'],
                    'mip_count': t['mip_count'],
                }
            else:
                if pkg_name not in index[key]['pkgs']:
                    index[key]['pkgs'].append(pkg_name)
    return index


def save_texture_index(pkg_dir, output_path=None):
    """Build and save the texture index as JSON."""
    import json
    index = build_texture_index(pkg_dir)
    if output_path is None:
        output_path = os.path.join(pkg_dir, '_texture_index.json')
    with open(output_path, 'w') as f:
        json.dump(index, f, indent=1)
    print(f"Texture index: {len(index)} entries -> {output_path}")
    return output_path


def load_texture_index(pkg_dir):
    """Load a previously saved texture index, or return None if not found."""
    import json
    idx_path = os.path.join(pkg_dir, '_texture_index.json')
    if os.path.isfile(idx_path):
        with open(idx_path) as f:
            return json.load(f)
    return None


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

    p_replace = sub.add_parser('replace', help='Replace a texture in a .pkg file')
    p_replace.add_argument('pkg', help='.pkg file path')
    p_replace.add_argument('texture_name', help='Texture name to replace (e.g. MelinoeTransform_Color)')
    p_replace.add_argument('dds', help='New .dds file (same format + dimensions + mips)')
    p_replace.add_argument('--output', '-o', default=None,
                          help='Output .pkg path (default: overwrite original)')

    p_index = sub.add_parser('index', help='Build texture index for all .pkg files in a directory')
    p_index.add_argument('pkg_dir', help='Directory containing .pkg files')
    p_index.add_argument('--output', '-o', default=None, help='Output JSON path')

    args = parser.parse_args()
    if args.command == 'list':
        cmd_list(args)
    elif args.command == 'extract':
        cmd_extract(args)
    elif args.command == 'replace':
        out = args.output or args.pkg
        ok = replace_texture(args.pkg, args.texture_name, args.dds, out)
        if ok:
            print("Texture replaced successfully!")
        else:
            print("Texture replacement failed.")
            sys.exit(1)
    elif args.command == 'index':
        save_texture_index(args.pkg_dir, args.output)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
