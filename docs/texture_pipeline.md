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
| 0xAA | Texture3D | `ReadTexture3D` |
| 0xAD | Texture2D | `ReadTexture2D` |
| 0xBB | Bink video | `ReadBink` |
| 0xBE | End of chunk | Stop processing current chunk |
| 0xCC | Include package | `ReadIncludePackage` (references another .pkg) |
| 0xDE | **Atlas** | `ReadAtlas` (character textures) |
| 0xEE | Bink atlas | `ReadBinkAtlas` |
| 0xFF | End of file | Stop processing entirely |

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
    Melinoe.pkg              — character texture atlas (15MB)
    Melinoe.pkg_manifest     — human-readable entry index
  720p/
    Melinoe.pkg              — lower resolution variant
```

## Texture Replacement Approach

To replace a character's texture:
1. Open .pkg, read header
2. For each LZ4-compressed chunk: decompress
3. Scan for atlas asset (type 0xDE)
4. Parse atlas sub-entries to find the target texture
5. Locate the inline texture data (after 0xDD marker)
6. Read: format(4) + width(4) + height(4) + depth(4) + size(4) + pixels
7. Replace pixel data with custom BCn-compressed texture (same format + dimensions)
8. LZ4-recompress the chunk
9. Write back to .pkg

Keeping the same format and dimensions avoids changing any offsets or sizes
upstream. For dimension changes, the atlas sub-entry rectangles would also
need updating.
