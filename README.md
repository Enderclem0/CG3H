# CG3H — Custom Geometry 3D for Hades II

Extract, edit, and repack 3D models for Hades II (Supergiant Games).

## What Works

| Feature | Status |
|---|---|
| **Export** any character to `.glb` (Blender-ready) | Working |
| **Import** edited `.glb` back to `.gpk` | Working |
| **Vertex position edits** (reshape mesh) | Working |
| **UV edits** (texture coordinate changes) | Working |
| **Normal edits** (if exported from Blender) | Working |
| **Multi-LOD patching** (all resolution levels) | Working |
| **GUI** with Export, Import, and Install tabs | Working |
| **Backup & restore** originals before installing | Working |
| Any character model (all 144 pairs in `_Optimized/`) | Working |

## What Doesn't Work (Yet)

| Feature | Why |
|---|---|
| **Adding/removing vertices** | Fixed-size vertex buffer; needs DLL mesh allocation |
| **Adding/removing bones** | Skeleton is read-only from the original GR2 |
| **Adding/removing meshes** | Mesh count is fixed at load time |
| **Bone weight painting** | Blender reorders joint lists; needs index remapping |
| **Textures** | Not stored in GR2 files (separate engine asset pipeline) |
| **Animations** | Complex Granny curve formats, separate from mesh data |
| **Triangle/index changes** | Not yet implemented (vertex count constraint applies) |

## Requirements

```
pip install numpy pygltflib lz4
```

The game's `granny2_x64.dll` is required (auto-detected from Steam path).

## Quick Start

### GUI (recommended)

```bash
python tools/converter_gui.py
# or double-click tools/Start_gui.bat
```

Three tabs:
- **Export** — select character(s), export `.glb` files for Blender
- **Import** — select character + your edited `.glb`, produce a `_mod.gpk`
- **Install** — backup originals and copy your mod into the game directory

### CLI

```bash
# Export
cd "C:/Program Files (x86)/Steam/steamapps/common/Hades II/Ship"
python C:/path/to/tools/gr2_to_gltf.py Melinoe -o Melinoe.glb

# Import (after editing in Blender)
python C:/path/to/tools/gltf_to_gr2.py Melinoe_edited.glb \
    --gpk ../Content/GR2/_Optimized/Melinoe.gpk \
    --sdb ../Content/GR2/_Optimized/Melinoe.sdb \
    --output-gpk ~/Documents/Hades2Mods/Melinoe_mod.gpk
```

## Blender Workflow

### Importing the `.glb`

1. File > Import > glTF 2.0 (.glb)
2. Select the exported `.glb` file
3. The model imports with skeleton and all meshes

### Editing Rules

**You MUST keep the same vertex count.** The pipeline patches vertex data
in-place — adding or removing vertices will fail with a strict error.

- **Edit in Edit Mode only** — do NOT apply transforms, modifiers, or
  operations in Object Mode that change vertex count
- **Do NOT** use Subdivide, Decimate, Merge by Distance, dissolve edges,
  or any tool that changes the number of vertices
- **Safe operations**: Move vertices (G), scale (S), rotate (R), sculpt
  (proportional editing), edit UVs, smooth vertices
- **Bone weights** are always preserved from the original (Blender reorders
  joints on re-export, so the tool ignores GLB weights)

### Exporting from Blender

1. Select **only the meshes and armature** (not cameras, lights, empties)
2. File > Export > glTF 2.0 (.glb)
3. Export settings:
   - Format: **glTF Binary (.glb)**
   - Include: **Selected Objects** (check this!)
   - Mesh > **Normals: OFF** (uncheck "Normals")
     - Exporting normals causes Blender to split vertices at normal seams,
       which changes the vertex count and breaks the import
   - Mesh > UVs: ON
   - Mesh > Vertex Colors: OFF
   - Armature: ON (if you selected it)

### Summary of Export Settings

```
Format:             glTF Binary (.glb)
Include:            Selected Objects only
Mesh > Normals:     OFF  <-- critical, prevents vertex splitting
Mesh > UVs:         ON
Mesh > Apply Mods:  OFF
Armature:           ON
```

## Repository Layout

```
tools/
  converter_gui.py     GUI (Export + Import + Install)
  gr2_to_gltf.py       CLI exporter: .gpk + .sdb -> .glb
  gltf_to_gr2.py       CLI importer: .glb + .gpk + .sdb -> .gpk
  gpk_pack.py           GPK archive pack/unpack (pure Python)
  granny_types.py       Dynamic Granny struct offset resolver
  Start_gui.bat         Windows launcher
  debug/                Diagnostic probes (development/debugging only)
docs/
  file_formats.md       Binary format reference (GPK, SDB, GR2)
```

## Technical Details

### How Export Works

1. LZ4-decompress `.gpk` entry to get raw `.gr2` bytes
2. Load `.sdb` string database, remap string indices via `GrannyRemapFileStrings`
3. Walk Granny type definitions at runtime to discover struct offsets
4. Apply 40-byte physical GPU stride (engine forces this for all meshes)
5. Normalize indices via `GrannyCopyMeshIndices` (handles 16-bit and 32-bit)
6. Pack geometry + skeleton into glTF 2.0 binary

### How Import Works

1. Parse the edited `.glb` (positions, normals, UVs)
2. Load the original `.gpk` + `.sdb` via the DLL
3. Match GLB meshes to GR2 meshes by name (fuzzy matching with LOD awareness)
4. `ctypes.memmove` new 40-byte vertex buffers into DLL memory
5. Serialize modified data tree back to `.gr2` via the Granny DLL write API
6. LZ4-compress into output `.gpk`

## Game Asset Layout

```
<Hades II>/
  Ship/
    granny2_x64.dll           Granny 3D runtime (975 exported functions)
  Content/GR2/_Optimized/
    Melinoe.gpk + .sdb        Character mesh + string database
    Zagreus.gpk + .sdb        (~144 model pairs total)
    _backups/                  Created by Install tab (originals stored here)
```

## Documentation

- [`docs/file_formats.md`](docs/file_formats.md) — GPK / SDB / GR2 binary layouts
- [`docs/rendering_pipeline.md`](docs/rendering_pipeline.md) — Engine pipeline (40-byte stride, index normalization)
- [`docs/architecture.md`](docs/architecture.md) — Implementation decisions and write API details
