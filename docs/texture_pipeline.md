# Hades II Texture Pipeline

Reverse-engineered from Ghidra decompilation of the game executable.

## Overview

```
GR2 material name ("Mat_Melinoe")
  → AddModelData: Material → Maps[0] → Material → Texture → FromFileName
    → fsGetPathFileName extracts filename
    → prepends "GR2/" → StringIntern hash
      → resolves to .pkg texture atlas at runtime
```

Textures are NOT stored in GR2 files. They live in `.pkg` package files.

## GR2 Material Chain

```
granny_mesh
  → MaterialBindings[0] (ptr to Material)
    → granny_material (44 bytes)
        Name*: "Mat_Melinoe"
        Maps: {count=1, ptr}
        Texture*: NULL (not used directly)
        ExtendedData: empty
      → Maps[0] → granny_material_map (16 bytes)
          Usage*: "color"
          Map*: ptr to nested material
        → granny_material "Melinoe_Model_file1"
            Texture*: ptr to granny_texture
          → granny_texture (92 bytes)
              FromFileName*: "D:/.../Melinoe_Color512.png"
```

AddModelData extracts the texture filename:
```c
fsGetPathFileName(texture->FromFileName, buf);  // "Melinoe_Color512"
path = "GR2/" + buf;                            // "GR2/Melinoe_Color512"
hash = HashGuid::StringIntern(path);
mesh_data->texture_id = hash;
```

## Mesh Type Detection (AddModelData)

Determined by mesh name string matching:
```c
if (strstr(mesh->Name, "Outline"))    → type = 1 (outline shader)
if (strstr(mesh->Name, "ShadowMesh")) → type = 2 (shadow shader)
else                                  → type = 0 (Granny3D body shader)
```

## Struct Sizes (confirmed via GrannyGetTotalObjectSize)

| Struct | Size |
|--------|------|
| granny_material | 44 bytes |
| granny_texture | 92 bytes |
| granny_material_map | 16 bytes |

## fi->Materials and fi->Textures

Example from Melinoe:
```
fi->Materials (count=5):
  [0] "Mat_Outline"
  [1] "Melinoe_Model_place2dTexture1"
  [2] "defaultColorMgtGlobals"
  [3] "Melinoe_Model_file1"
  [4] "Mat_Melinoe"

fi->Textures (count=1):
  [0] "D:/work/.../Melinoe_Color512.png"
```

---

## PKG File Format

Textures are stored in `Content/Packages/1080p/<name>.pkg` with a companion
`<name>.pkg_manifest` file.

### PKG Structure

```
[uint32]           — header flags (bit check: (val & 0x60000000) == 0x20000000)

Chunks (repeated until EndOfFile):
  [byte]           — has_compressed_data (non-zero = LZ4 compressed)

  If compressed:
    [uint32]       — compressed_size
    [bytes]        — LZ4-compressed chunk data
    → LZ4_decompress_safe → raw chunk (up to 33MB)

  If uncompressed:
    [raw bytes]    — read directly

  Within decompressed chunk:
    Loop ReadNextAsset until EndOfChunk/EndOfFile:
      [byte]       — asset type tag
      [asset data...]
```

### Asset Type Tags

| Tag | Type | Function |
|-----|------|----------|
| 0xAA | **Texture3D** | `ReadTexture3D` — big-endian size + XNB wrapper (same as 0xAD) |
| 0xAD | **Texture2D** | `ReadTexture2D` — big-endian size + XNB wrapper |
| 0xBB | Bink video | `ReadBink` |
| 0xBE | End of chunk | Stop processing current chunk |
| 0xCC | Include package | `ReadIncludePackage` (references another .pkg) |
| 0xDE | **Atlas** | `ReadAtlas` (sprite atlases — 2D portraits, not 3D model textures) |
| 0xEE | Bink atlas | `ReadBinkAtlas` |
| 0xFF | End of file | Stop processing entirely |

**Important:** 0xAA (Texture3D) entries have a big-endian size field, just like 0xAD (Texture2D). Reading the size as little-endian produces a wrong value that causes the scanner to skip the rest of the chunk. Both entry types are XNB-wrapped with the same internal texture format.

### Atlas Entry Format (type 0xDE)

Character textures use atlas entries:

```
[int32]            — total_data_size
[int32]            — magic (0x7FB1776B = versioned format)
[int32]            — version (if magic matched)
[int32]            — sub_entry_count

Per sub-entry (sprite/region in the atlas):
  [CSString]       — name (e.g. "Portraits\Melinoe\Portraits_Melinoe_01")
  [int32]          — width
  [int32]          — height
  [int32]          — originX
  [int32]          — originY
  [int32]          — rectX
  [int32]          — rectY
  [int32]          — rectW
  [int32]          — rectH
  [float32]        — scaleX
  [float32]        — scaleY
  [byte]           — flags (version > 0): bit0=multiTexture, bit1=type, bit2=mip
  [int32+pairs]    — hull points (version > 2): count then (x,y) int32 pairs

After all sub-entries:
  [byte]           — 0xDD = inline texture follows, else = texture name reference
  
  If 0xDD (inline):
    → Read2DTextureWithName → texture data follows
  Else:
    [CSString]     — texture name (looked up from loaded textures)
```

### Texture Data Format (from AddTexture3DResourceFromStream)

```
[uint32]           — format code
[uint32]           — width (pixels)
[uint32]           — height (pixels)
[uint32]           — depth (1 for 2D)
[uint32]           — pixel_data_size (bytes)
[bytes]            — raw pixel data (BCn compressed or uncompressed)
```

If format code is 0x20 (native), calls `loadTextureNative` instead (DDS/KTX).

### Texture Format Codes

| Code | TinyImageFormat | Description |
|------|----------------|-------------|
| 0x00 | B8G8R8A8_UNORM | 32-bit BGRA uncompressed |
| 0x04 | DXBC1_RGB_UNORM | BC1/DXT1, 4bpp, no alpha |
| 0x05 | DXBC2_UNORM | BC2/DXT3, 8bpp, sharp alpha |
| 0x06 | DXBC3_UNORM | BC3/DXT5, 8bpp, smooth alpha |
| 0x0C | R8_UNORM | 8-bit grayscale |
| 0x0E | R8G8B8A8_UNORM | 32-bit RGBA uncompressed |
| 0x1A | R8_UNORM | 8-bit grayscale (variant) |
| 0x1C | **DXBC7_UNORM** | BC7, high quality block compression |
| 0x1E | R8G8_UNORM | 16-bit RG |
| 0x20 | Native | DDS/KTX via loadTextureNative |

### XNB Container (wraps texture data in Texture3D entries)

```
[3 bytes]          — "XNB" magic
[1 byte]           — platform ('w' = Windows)
[1 byte]           — version (must be 6)
[1 byte]           — flags
[4 bytes]          — file_size (uint32)
→ texture data follows (format + width + height + depth + size + pixels)
```

---

## File Locations

```
<game>/Content/Packages/
  1080p/
    Fx.pkg                   — contains some 3D model textures (534MB)
    <Character>.pkg          — may contain character-specific model textures + 2D sprites
    Melinoe.pkg_manifest     — human-readable entry index
  720p/
    ...                      — lower resolution variants
```

### 3D Model Textures

3D model textures are spread across **multiple .pkg files** (not only `Fx.pkg`). All character textures are stored as Texture2D (0xAD) or Texture3D (0xAA) entries with a `GR2\` or `Models\` path prefix.

Example:
```
Fx.pkg → "Models\Melinoe\MelinoeTransform_Color"
  512x512 BC7, 6 mip levels (349,440 bytes total)
  Mip 0: 512×512 = 262,144 bytes
  Mip 1: 256×256 =  65,536 bytes
  Mip 2: 128×128 =  16,384 bytes
  Mip 3:  64×64  =   4,096 bytes
  Mip 4:  32×32  =   1,024 bytes
  Mip 5:  16×16  =     256 bytes
```

Mipmaps are stored inline (sequentially after the base level) within the
single pixel data block. The `pixel_data_size` field includes all mip levels.

### Texture Index

To avoid scanning all `.pkg` files on every export, a texture index
(`_texture_index.json`) can be built once with `pkg_texture.py build-index`.
It maps lowercase texture base names to their `.pkg` location, format, and
dimensions. Subsequent exports read this small JSON for fast lookups.

### 2D Portrait/Sprite Textures

2D portrait textures are in the character's own .pkg as Atlas (0xDE) entries:
```
Melinoe.pkg → "..\Temp\Win\Atlases\Melinoe_Textures00-06"
  Various sizes (4096x4096 to 372x908), BC7, no mipmaps
```

Atlas entries contain sprite sub-entries (regions) and either an inline
texture (marker 0xDD) or a texture name reference.

---

## Texture Modding Workflow

### Multi-Texture Support

Each mesh can have its own texture, discovered via the GR2 material chain:

```
mesh -> MaterialBindings[0] -> Material -> Maps[0] -> Material -> Texture -> FromFileName
```

The exporter walks this chain per mesh to find each mesh's texture filename.
Multiple textures are embedded in the GLB, each with its own glTF material,
and each mesh is assigned its correct material.

Three fallback levels for texture name resolution:
1. **Material chain** (most reliable) — walks the GR2 struct pointers
2. **fi->Textures** — reads the file_info texture array
3. **Name-based guessing** — derives texture name from character name

### Export

The exporter produces two outputs:
```
Melinoe.glb                              — meshes + skeleton + per-mesh embedded PNG textures
Melinoe_textures/
  MelinoeTransform_Color.dds             — original BC7 + mipmaps (named per texture)
  Melinoe_Glow.dds                       — additional textures if present
```

DDS files are saved with their actual texture names from the material chain.
The PNG in each GLB material is decoded from BC7 (mip 0 only) for Blender preview.
The DDS files are exact copies with all mipmaps preserved for lossless editing.

### Import: Two Paths

#### Path 1: DDS Editing (pixel-perfect, recommended)

For modders who want maximum quality:

1. Edit the `.dds` file in Photoshop, Paint.NET, or GIMP (with DDS plugin)
2. These tools show all mip levels and regenerate them on save
3. Save as BC7 DDS with mipmaps
4. Import: the tool reads the DDS directly and writes it to the .pkg
5. **Zero quality loss** — the DDS data goes in as-is

#### Path 2: PNG Editing (via Blender)

For modders who edit the texture in Blender or as a PNG:

1. The texture is embedded as PNG in the GLB (mip 0 = base 512x512)
2. Edit in Blender's texture paint or export the PNG and edit externally
3. On reimport, the tool must:
   a. Regenerate mipmaps from the edited base (Pillow `Image.resize`)
   b. Recompress each mip to BC7 (requires external encoder)
   c. Pack all mip levels into the .pkg

**BC7 encoding options:**
- `texconv.exe` (Microsoft DirectXTex, free) — best quality, CLI
- `compressonator` (AMD, free) — CLI, also good
- Fallback: write as **uncompressed RGBA** (format 0x0E) — 4x larger but no
  encoder needed, game supports it natively

#### Path 3: No Texture Change

If the texture wasn't modified (same pixels as original):
- Skip texture replacement entirely
- Keep the original .pkg data untouched

---

## Texture Replacement (PKG Modification)

To replace a character's 3D model texture:
1. Look up the texture's `.pkg` file via the texture index (or scan all `.pkg` files)
2. Open the `.pkg`, decompress LZ4 chunks
3. Scan for the target Tex2D/Texture3D entry by name
4. Replace the pixel data (all mip levels, same format + dimensions)
5. LZ4-recompress the modified chunk
6. Write back to the `.pkg`

Keeping the same format, dimensions, and mip count avoids changing any sizes
or offsets in the PKG structure.

For **dimension changes**: the pixel_data_size field, XNB file_size, and
entry total_size all need updating, plus all mip levels regenerated at the
new resolution.
