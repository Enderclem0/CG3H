# Hades II File Formats

All formats confirmed empirically via memory inspection of `granny2_x64.dll` v2.12.0.14 and binary analysis of game assets.

---

## GPK — Game Package Archive

Custom archive format containing LZ4-compressed assets. Located at:
```
<game>/Content/GR2/_Optimized/<name>.gpk
```
Each `.gpk` is paired with a `.sdb` of the same base name.

### Binary Layout

```
[version    : uint32 LE]  Always 1
[file_count : uint32 LE]  Number of entries

Repeated file_count times:
  [name_len  : uint8]       Length of filename
  [name      : uint8 x N]  UTF-8, no null terminator
  [comp_size : uint32 LE]  Byte length of compressed data
  [lz4_data  : uint8 x C] Raw LZ4 block (no frame header)
```

### Compression

Raw LZ4 block format. In Python:
```python
import lz4.block
data   = lz4.block.decompress(raw, uncompressed_size=32*1024*1024)
packed = lz4.block.compress(data, store_size=False)
```

### Entry Naming

| Pattern | Content |
|---|---|
| `<Name>_Mesh` | Skinned character mesh (GR2) |
| `<Name>Battle_Mesh` | Battle-mode mesh variant (multi-entry GPK) |
| `<Name>Hub_Mesh` | Hub-mode mesh variant (multi-entry GPK) |
| `<Name>_Anim_*` | Animation clips (GR2) |

### Multi-Entry GPKs

Some characters have multiple mesh entries in a single GPK (e.g. Hecate has
`HecateBattle_Mesh` + `HecateHub_Mesh`). Each entry is a separate GR2 blob.
The exporter processes all entries by default; `--mesh-entry` selects specific ones.

---

## SDB — String Database

When Granny3D builds a `.gr2` for distribution, all string data (bone names, mesh names, etc.) is stripped out and stored in a paired `.sdb` file. The SDB is itself a valid Granny file; strings are patched back at load time via `GrannyRemapFileStrings`.

**Format:** Opaque binary — handled entirely by `granny2_x64.dll`. Do not parse manually.

---

## GR2 — Granny3D Binary Asset

Granny3D proprietary binary format. The Hades II build uses Granny v2.12.0.14; `granny2_x64.dll` exports 975 functions (full SDK available). Load via `GrannyReadEntireFileFromMemory`, then access data through the struct layouts below.

---

## In-Memory Struct Layouts

All offsets confirmed on Granny 2.12.0.14 / Hades II. All structs are **packed** (no alignment padding).

### `granny_file_info` — root object returned by `GrannyGetFileInfo(file_ptr)`

| Offset | Size | Field |
|--------|------|-------|
| +0x00 | 8 | ArtToolInfo* |
| +0x08 | 8 | ExporterInfo* |
| +0x10 | 8 | FromFileName* |
| +0x18 | 4 | TextureCount |
| +0x1C | 8 | Textures** |
| +0x24 | 4 | MaterialCount |
| +0x28 | 8 | Materials** |
| +0x30 | 4 | SkeletonCount |
| +0x34 | 8 | Skeletons** |
| +0x3C | 4 | VertexDataCount |
| +0x40 | 8 | VertexDatas** |
| +0x48 | 4 | TriTopologyCount |
| +0x4C | 8 | TriTopologies** |
| +0x54 | 4 | MeshCount |
| +0x58 | 8 | Meshes** |
| +0x60 | 4 | ModelCount |
| +0x64 | 8 | Models** |
| +0x6C | 4 | TrackGroupCount |
| +0x70 | 8 | TrackGroups** |
| +0x78 | 4 | AnimationCount |
| +0x7C | 8 | Animations** |

Each `**` field is a pointer to an array of pointers (dereference twice to reach a struct).

---

### `granny_skeleton`

| Offset | Size | Field |
|--------|------|-------|
| +0x00 | 8 | Name* |
| +0x08 | 4 | BoneCount |
| +0x0C | 8 | Bones* (stride = 164 bytes) |

---

### `granny_bone` — stride = **164 bytes**

| Offset | Size | Field |
|--------|------|-------|
| +0x00 | 8 | Name* |
| +0x08 | 4 | ParentIndex  (-1 = root) |
| +0x0C | 4 | Transform.Flags  (0x1=HasPos, 0x2=HasRot, 0x4=HasScale) |
| +0x10 | 12 | Transform.Position  float32x3 |
| +0x1C | 16 | Transform.Orientation  float32x4 (XYZW quaternion) |
| +0x2C | 36 | Transform.ScaleShear  float32x9 (3x3 row-major) |
| +0x50 | 64 | InverseWorld4x4  float32x16 |
| +0x90 | 4 | LODError |
| +0x94 | 16 | ExtendedData (2 pointers) |

**Matrix convention:** Granny uses row-major storage with translation in the last row (DirectX / row-vector convention). The raw bytes map directly to glTF's column-major InverseBindMatrix — no transposition needed.

---

### `granny_mesh`

| Offset | Size | Field |
|--------|------|-------|
| +0x00 | 8 | Name* |
| +0x08 | 8 | PrimaryVertexData* |
| +0x10 | 4 | MorphTargetCount |
| +0x14 | 8 | MorphTargets* |
| +0x1C | 8 | PrimaryTopology* |
| +0x24 | 4 | MaterialBindingCount |
| +0x28 | 8 | MaterialBindings* |
| +0x30 | 4 | BoneBindingCount |
| +0x34 | 8 | BoneBindings* |

Multiple meshes may share the same `Name` — these are split parts of the same mesh, all rendered together. During export, duplicate names get `_2`, `_3` suffixes. The importer strips these when matching back to GR2 meshes.

---

### `granny_bone_binding` — stride = **44 bytes**

| Offset | Size | Field |
|--------|------|-------|
| +0x00 | 8 | BoneName* |
| +0x08 | 12 | OBBMin  float32x3 |
| +0x14 | 12 | OBBMax  float32x3 |
| +0x20 | 4 | TriangleCount |
| +0x24 | 8 | TriangleIndices* |

`BLENDINDICES` values in the vertex buffer are local palette indices into this array. Resolve to global skeleton indices: `BoneBinding[i].BoneName -> skeleton bone index`.

---

### `granny_vertex_data`

| Offset | Size | Field |
|--------|------|-------|
| +0x00 | 8 | VertexType* |
| +0x08 | 4 | VertexCount |
| +0x0C | 8 | Vertices* |

---

### Vertex Layout — stride = **40 bytes**

Fixed format used by all Hades II character meshes. The engine ignores Granny's logical
vertex metadata and forces this layout (see [rendering_pipeline.md](rendering_pipeline.md)).

| Byte | Size | Type | Semantic |
|------|------|------|----------|
| +0 | 12 | float32x3 | POSITION |
| +12 | 4 | uint8x4 | BLENDWEIGHT  (values sum to 255; normalize / sum for glTF) |
| +16 | 4 | uint8x4 | BLENDINDICES  (bone palette local indices) |
| +20 | 12 | float32x3 | NORMAL |
| +32 | 8 | float32x2 | TEXCOORD_0 |

---

### `granny_tri_topology`

| Offset | Size | Field |
|--------|------|-------|
| +0x00 | 4 | GroupCount |
| +0x04 | 8 | Groups* |
| +0x0C | 4 | IndexCount  (32-bit path, unused in Hades II) |
| +0x10 | 8 | Indices*  (32-bit, unused) |
| +0x18 | 4 | Idx16Count  (16-bit index count) |
| +0x1C | 8 | Idx16*  (uint16 triangle list) |

Hades II uses the 16-bit index path exclusively. Count is always a multiple of 3.

---

## PKG — Package Archive (Textures)

Textures and other assets are stored in `.pkg` files under `Content/Packages/1080p/`.

### Binary Layout

```
[header       : uint32 BE]  Version in low 16 bits, flags in high bits
                             (header & 0x20000000) != 0 -> LZ4-compressed chunks

Chunks (repeated until EOF):
  [flag        : uint8]      Non-zero = LZ4 compressed, 0 = uncompressed
  If compressed:
    [comp_size : uint32 BE]  Byte-swapped (big-endian) compressed size
    [lz4_data  : bytes]      Raw LZ4 block
  If uncompressed:
    [raw bytes]              Remaining data is uncompressed

Within each decompressed chunk:
  Loop over asset entries:
    [tag       : uint8]      Asset type (see table below)
    [asset data...]
```

### Asset Type Tags

| Tag | Type | Notes |
|-----|------|-------|
| 0xAA | **Texture3D** | Name + big-endian size + XNB-wrapped texture data |
| 0xAD | **Texture2D** | Name + big-endian size + XNB-wrapped texture data |
| 0xBB | Bink video | Video data |
| 0xBE | End of chunk | Stop processing current chunk |
| 0xCC | Include | References another .pkg file |
| 0xDE | **Atlas** | Sprite atlas with sub-entries and inline or referenced texture |
| 0xEE | Bink atlas | Video atlas |
| 0xFF | End of file | Stop processing entirely |

**Key finding:** Both 0xAA and 0xAD use the same format: name string + big-endian uint32 size + XNB-wrapped texture data. The 0xAA size field is big-endian (byte-swapped), which was previously misread as little-endian, causing the scanner to skip the rest of the chunk.

### Texture Entry Format (0xAA and 0xAD)

```
[CSString]         — texture name (7-bit length prefix + UTF-8)
[uint32 BE]        — total_size (byte-swapped big-endian)
[XNB header]       — 10 bytes: "XNB" + platform + version + flags + file_size
[uint32 LE]        — format code (see texture_pipeline.md for format table)
[uint32 LE]        — width
[uint32 LE]        — height
[uint32 LE]        — depth (1 for 2D textures)
[uint32 LE]        — pixel_data_size (includes all mip levels)
[bytes]            — pixel data (BCn compressed or uncompressed)
```

Mipmaps are stored inline (sequentially after the base level) within the pixel data block.

### 3D Model Texture Locations

All character 3D model textures are stored as Texture2D (0xAD) or Texture3D (0xAA) entries across various `.pkg` files. Texture names use a `GR2\` or `Models\` path prefix. A texture index (`_texture_index.json`) maps texture names to their `.pkg` locations for fast batch lookups.

Textures are often duplicated across biome packages (BiomeF, BiomeHub, BiomeIHouse, etc.). The export manifest tracks ALL `.pkg` files containing each texture.

### Checksum Validation

The game stores XXH64 checksums for `.pkg` files in `Content/Packages/1080p/checksums.txt`. Each line contains a filename and its hash. H2M bypasses this validation for standalone `.pkg` files loaded via `rom.game.LoadPackages`.
