# CG3H — Custom Geometry 3D for Hades II

[![Release](https://img.shields.io/github/v/release/Enderclem0/CG3H)](https://github.com/Enderclem0/CG3H/releases)

Extract, edit, and repack 3D models for Hades II (Supergiant Games).
Reshape characters, paint bone weights, add/remove geometry, and install mods — all from Blender or the standalone GUI.

## What Works

| Feature | Status |
|---|---|
| **Export** any character to `.glb` (Blender-ready) | Working |
| **Import** edited `.glb` back to `.gpk` | Working |
| **Vertex position edits** (reshape mesh) | Working |
| **UV edits** (texture coordinate changes) | Working |
| **Normal edits** (if exported from Blender) | Working |
| **Topology changes** (add/remove vertices, subdivide, decimate) | Working (experimental) |
| **Triangle/face edits** | Working |
| **Bone weight painting** from Blender | Working |
| **Multi-part mesh patching** (all same-name mesh parts) | Working |
| **GUI** with Export, Import, and Install tabs | Working |
| **Blender addon** (File > Import/Export) | Working |
| **Animation export** (`--animations`) | Working |
| **Animation import** (`--patch-animations`) | Working |
| **Add new meshes** (accessories, armor, custom geometry) | Working |
| **Backup & restore** originals before installing | Working |
| **Texture export** (per-mesh DDS + embedded PNG in GLB) | Working |
| **Parallel batch export** (N subprocesses, auto-scaled) | Working |
| Any character model (all 144 pairs in `_Optimized/`) | Working |

## What Doesn't Work (Yet)

| Feature | Why |
|---|---|
| **Adding/removing bones** | Skeleton is read-only from the original GR2 |
| **Textures** | Export works (DDS + embedded PNG in GLB); import/replacement is manual |

## Limits

- **65,535 vertices max per mesh** — engine uses uint16 index buffers. The tool rejects meshes over this limit.
- **Scaling mesh without skeleton** causes animation stretching — bones still pivot at original positions. This is expected; skeleton editing is not yet supported.
- **Extreme deformations** may cause edge-case frustum culling artifacts despite OBB recomputation.
- **Animation export is slow** — each character has 600-850 animation entries. Exporting all of them takes several minutes. Use `--anim-filter` to limit scope (e.g. `--anim-filter Idle`). Use `--anim-workers N` to control parallelism (0=auto, 1=sequential).

## Requirements

```
pip install numpy pygltflib lz4 Pillow
```

The game's `granny2_x64.dll` is required (auto-detected from Steam path).

## Quick Start

### GUI (recommended)

```bash
python tools/converter_gui.py
# or double-click tools/Start_gui.bat
```

Three tabs:
- **Export** — select character(s), export `.glb` files for Blender (with textures and optional animations). Batch export runs in parallel across CPU cores.
- **Import** — select character + your edited `.glb`, produce a `_mod.gpk`
- **Install** — backup originals and copy your mod into the game directory

### Blender Addon

1. In Blender: Edit > Preferences > Add-ons > Install
2. Navigate to `blender_addon/cg3h/` and select `__init__.py`
3. Enable "CG3H — Hades II Model Tools"
4. Set the game path and tools path in addon preferences

Then use:
- **File > Import > Hades II Model (.gpk)** — browse to a `.gpk` in `Content/GR2/_Optimized/`
- **File > Export > Hades II Model (.gpk)** — select meshes + armature, pick the original character, save

### CLI

```bash
# Export
cd "C:/Program Files (x86)/Steam/steamapps/common/Hades II/Ship"
python C:/path/to/tools/gr2_to_gltf.py Melinoe -o Melinoe.glb

# Import (same topology — default, safe)
python C:/path/to/tools/gltf_to_gr2.py Melinoe_edited.glb \
    --gpk ../Content/GR2/_Optimized/Melinoe.gpk \
    --sdb ../Content/GR2/_Optimized/Melinoe.sdb \
    --output-gpk ~/Documents/Hades2Mods/Melinoe_mod.gpk

# Import (topology changes — subdivide, decimate, sculpt)
python C:/path/to/tools/gltf_to_gr2.py Melinoe_sculpted.glb \
    --gpk ../Content/GR2/_Optimized/Melinoe.gpk \
    --sdb ../Content/GR2/_Optimized/Melinoe.sdb \
    --output-gpk ~/Documents/Hades2Mods/Melinoe_mod.gpk \
    --allow-topology-change
```

## Blender Workflow

### Importing the `.glb`

1. File > Import > glTF 2.0 (.glb)
2. Select the exported `.glb` file
3. The model imports with skeleton and all meshes

### Editing Modes

#### Same-topology edits (default, safe)

Move vertices without changing the mesh structure. This is the most reliable mode.

- **Safe operations**: Move (G), Scale (S), Rotate (R), proportional editing,
  sculpt mode, UV editing, smooth vertices
- **Do NOT**: Subdivide, Decimate, Merge by Distance, dissolve edges, or any
  operation that adds/removes vertices
- Vertex count must match exactly — the tool will error if it doesn't

#### Topology changes (experimental)

Enable with `--allow-topology-change` on CLI or the checkbox in the GUI.
Allows adding/removing vertices and faces.

- **Supported**: Subdivide, Decimate, Sculpt with Dyntopo, Remesh, Boolean,
  Knife tool, Extrude — anything that changes vertex/face count
- **When deleting vertices**: use **Delete > Faces** to also remove connected
  triangles. Dangling faces create corrupt geometry the game will reject.
- Bone weights are remapped by name from the GLB — weight painting works

### Exporting from Blender

1. Select **only the meshes and armature** (not cameras, lights, empties)
2. File > Export > glTF 2.0 (.glb)
3. Export settings:

```
Format:             glTF Binary (.glb)
Include:            Selected Objects only
Mesh > Normals:     OFF  <-- critical, prevents vertex splitting
Mesh > UVs:         ON
Mesh > Apply Mods:  OFF  (or ON if you used modifiers like Decimate)
Mesh > Vertex Colors: OFF
Armature:           ON
```

**Why Normals OFF?** Exporting normals causes Blender to split vertices at
normal seams, which changes the vertex count. With same-topology mode this
breaks the import. With topology-change mode it works but produces unnecessary
extra vertices. Turn normals off to keep the mesh clean.

## Repository Layout

```
blender_addon/
  cg3h/__init__.py     Blender addon (File > Import/Export for .gpk)
tools/
  converter_gui.py     Standalone GUI (Export + Import + Install)
  gr2_to_gltf.py       CLI exporter: .gpk + .sdb -> .glb (with textures)
  gltf_to_gr2.py       CLI importer: .glb + .gpk + .sdb -> .gpk
  pkg_texture.py        PKG texture extractor/replacer + texture index builder
  gpk_pack.py           GPK archive pack/unpack (pure Python)
  granny_types.py       Dynamic Granny struct offset resolver
  Start_gui.bat         Windows launcher
  debug/                Diagnostic probes (development/debugging only)
docs/
  file_formats.md       Binary format reference (GPK, SDB, GR2, PKG)
  texture_pipeline.md   Texture pipeline (material chains, PKG format, DDS export)
  architecture.md       Implementation decisions and write API details
```

## Technical Details

### How Export Works

1. LZ4-decompress `.gpk` entry to get raw `.gr2` bytes
2. Load `.sdb` string database, remap string indices via `GrannyRemapFileStrings`
3. Walk Granny type definitions at runtime to discover struct offsets
4. Apply 40-byte physical GPU stride (engine forces this for all meshes)
5. Normalize indices via `GrannyCopyMeshIndices` (handles 16-bit and 32-bit)
6. Walk material chains (mesh -> MaterialBindings -> Material -> Maps -> Material -> Texture -> FromFileName) to find per-mesh texture names
7. Extract textures from `.pkg` files as DDS (with mipmaps) and embed decoded PNG in GLB for Blender preview
8. Pack geometry + skeleton + textures into glTF 2.0 binary
9. Duplicate mesh names (split parts) get `_1`, `_2` suffixes (not LOD suffixes -- they are parts of the same mesh, not LODs)

### How Import Works

1. Parse the edited `.glb` (positions, normals, UVs, bone weights, indices)
2. Load the original `.gpk` + `.sdb` via the DLL
3. Match GLB meshes to GR2 meshes by name, pair same-name parts by position order
4. Remap bone weights from GLB joint order to GR2 BoneBinding order by name
5. Patch vertex buffers in DLL memory (in-place or new allocation for topology changes)
6. Patch index buffers if triangle connectivity changed
7. Recompute per-bone bounding boxes (OBB) for frustum culling
8. Serialize modified data tree back to `.gr2` via the Granny DLL write API
9. LZ4-compress into output `.gpk`

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
