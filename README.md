# CG3H — Custom Geometry 3D for Hades II

[![Release](https://img.shields.io/github/v/release/Enderclem0/CG3H)](https://github.com/Enderclem0/CG3H/releases)

Extract, edit, and repack 3D models and textures for Hades II (Supergiant Games).
Reshape characters, paint bone weights, add/remove geometry, replace textures, and install mods — all from Blender or the standalone GUI.

**v3.0**: CG3H now integrates with [Hell2Modding (H2M)](https://github.com/SGG-Modding/Hell2Modding) for non-destructive mod distribution. Zero game files modified.

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
| **GUI** with Export and Install tabs | Working |
| **Blender addon** (File > Import/Export) | Working |
| **Animation export** (`--animations`) | Working |
| **Animation import** (`--patch-animations`) | Working |
| **Add new meshes** (accessories, armor, custom geometry) | Working |
| **Texture export** (per-mesh DDS + embedded PNG in GLB) | Working |
| **Texture import** (PNG/DDS/Blender edits -> standalone .pkg) | Working |
| **Multi-texture per character** (per-mesh material chain) | Working |
| **Multi-entry GPK** (characters with multiple mesh entries) | Working |
| **Parallel batch export** (N subprocesses, auto-scaled) | Working |
| **H2M mod packages** (`cg3h build` -> Thunderstore ZIP) | Working |
| **Standalone .pkg builder** (custom textures, no game files modified) | Working |
| **PyInstaller exe** (cg3h_builder.exe, no Python needed) | Working |
| Any character model (all 144 pairs in `_Optimized/`) | Working |

## What Doesn't Work (Yet)

| Feature | Why |
|---|---|
| **Adding/removing bones** | Skeleton is read-only from the original GR2 |
| **mesh_replace / mesh_patch CC-free distribution** | Requires v3.1 diff format (planned) |

## Limits

- **65,535 vertices max per mesh** — engine uses uint16 index buffers. The tool rejects meshes over this limit.
- **Scaling mesh without skeleton** causes animation stretching — bones still pivot at original positions. This is expected; skeleton editing is not yet supported.
- **Extreme deformations** may cause edge-case frustum culling artifacts despite OBB recomputation.
- **Animation export is slow** — each character has 600-850 animation entries. Exporting all of them takes several minutes. Use `--anim-filter` to limit scope (e.g. `--anim-filter Idle`). Use `--anim-workers N` to control parallelism (0=auto, 1=sequential).

## Requirements

For development / CLI usage:

```
pip install numpy pygltflib lz4 Pillow etcpak xxhash
```

The game's `granny2_x64.dll` is required (auto-detected from Steam path).

For end users installing mods: **no Python needed** — mesh mods include `cg3h_builder.exe` (29MB standalone), and texture mods are pre-built `.pkg` files.

---

## Quick Start — H2M Workflow (recommended)

The H2M workflow is the primary way to build and distribute mods in v3.0. It produces Thunderstore-ready ZIP packages that install via Hell2Modding with zero game file modification.

### Building a mod package

1. Create a `mod.json` descriptor (see [mod.json specification](#modjson-specification) below)
2. Place your assets alongside it (GLB for meshes, PNG for textures)
3. Run the build command:

```bash
# Build H2M folder structure
python tools/cg3h_build.py

# Build + create Thunderstore-ready ZIP
python tools/cg3h_build.py --package
```

The builder auto-detects the game directory from Steam paths.

### What `cg3h build` produces

For **texture_replace** mods:
- Standalone `.pkg` file (built from scratch, not a modified game file)
- H2M manifest + Lua companion
- Ready for Thunderstore upload

For **mesh_add** mods:
- `.glb` with new meshes only (no copyrighted geometry)
- `cg3h_builder.exe` (29MB standalone builder)
- Lua companion that auto-builds the GPK on first launch
- Standalone `.pkg` for any custom textures
- Ready for Thunderstore upload

### Thunderstore ZIP contents

```
MyMod/
  mod.json                  Mod descriptor
  manifest.json             Thunderstore manifest
  main.lua                  H2M Lua companion (auto-generated)
  *.glb                     New meshes (mesh_add only, CC-free)
  *.png                     Custom textures
  *.pkg                     Standalone texture package
  cg3h_builder.exe          Standalone builder (mesh mods only)
```

### H2M Lua companion

The auto-generated `main.lua` handles runtime loading:
- Loads standalone `.pkg` via `rom.game.LoadPackages`
- Auto-builds GPK on first launch if missing (runs `cg3h_builder.exe`)
- Deferred loading via `rom.on_import.post`

### mod.json specification

```json
{
  "name": "MyMod",
  "version": "1.0.0",
  "author": "YourName",
  "description": "Description of the mod",
  "type": "texture_replace",
  "character": "Melinoe",
  "assets": {
    "textures": ["custom_color.png"]
  }
}
```

Four mod types are supported:

| Type | Description | CC-free | Status |
|---|---|---|---|
| `texture_replace` | Custom PNG -> standalone .pkg | Yes | Working |
| `mesh_add` | Append new meshes (original geometry not distributed) | Yes | Working |
| `mesh_replace` | Swap character meshes | Needs v3.1 diff format | Planned |
| `mesh_patch` | Edit vertices in-place | Needs v3.1 diff format | Planned |

### What's no longer needed (thanks to H2M)

The v3.0 H2M workflow eliminates several pain points from v2.x:
- No DLL injection
- No checksum management
- No backup/restore system
- No game file modification

H2M's `LoadPackages` API loads standalone `.pkg` files at runtime, and the Lua companion handles GPK building on the end user's machine.

---

## Advanced / Legacy Workflow (v2.x)

The GUI and CLI tools from v2.x still work for direct game file modification. This workflow requires Python and modifies files in the game directory.

### GUI

```bash
python tools/converter_gui.py
# or double-click tools/Start_gui.bat
```

Two tabs:
- **Export** — select character(s), export `.glb` files for Blender (with textures and optional animations). Batch export runs in parallel across CPU cores. Multi-entry characters export all entries by default (`--list-entries` to inspect, `--mesh-entry` to filter).
- **Install** — browse to an export folder, reads `manifest.json`, imports mesh + installs textures in one click. One-click uninstall restores all files (GPK + PKGs + checksums). Mod registry (`_mods.json`) tracks changes per character.

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

---

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
  cg3h_build.py        H2M mod builder (mod.json -> Thunderstore ZIP)
  converter_gui.py     Standalone GUI (Export + Install tabs)
  gr2_to_gltf.py       CLI exporter: .gpk + .sdb -> .glb (with textures + animations)
  gltf_to_gr2.py       CLI importer: .glb + .gpk + .sdb -> .gpk
  pkg_texture.py        PKG texture extractor/replacer + standalone .pkg builder
  gpk_pack.py           GPK archive pack/unpack (pure Python)
  granny_types.py       Dynamic Granny struct offset resolver
  Start_gui.bat         Windows launcher
  debug/                Diagnostic probes (development/debugging only)
tests/
  test_core.py           25 unit tests
  TEST_CHECKLIST.md      Manual test checklist
docs/
  file_formats.md       Binary format reference (GPK, SDB, GR2, PKG)
  texture_pipeline.md   Texture pipeline (material chains, PKG format, DDS export)
  architecture.md       Implementation decisions, write API details, H2M integration
```

## Technical Details

### How Export Works

1. LZ4-decompress `.gpk` entry (or all entries for multi-entry characters) to get raw `.gr2` bytes
2. Load `.sdb` string database, remap string indices via `GrannyRemapFileStrings`
3. Walk Granny type definitions at runtime to discover struct offsets
4. Apply 40-byte physical GPU stride (engine forces this for all meshes)
5. Normalize indices via `GrannyCopyMeshIndices` (handles 16-bit and 32-bit)
6. Walk material chains (mesh -> MaterialBindings -> Material -> Maps -> Material -> Texture -> FromFileName) to find per-mesh texture names
7. Check Lua GrannyTexture overrides from game `Scripts/` (48 entities override texture at runtime)
8. Extract textures from `.pkg` files as DDS (with mipmaps) and embed decoded PNG in GLB for Blender preview
9. Export variant textures (e.g. HecateEM_Color for Extreme Measures) as standalone files, marked in manifest
10. Pack geometry + skeleton + textures into glTF 2.0 binary
11. Duplicate mesh names (split parts) get `_2`, `_3` suffixes (not LOD suffixes -- they are parts of the same mesh, not LODs)
12. Write `manifest.json` with per-mesh GR2 entry mapping, texture metadata (pkg source, format, dimensions, mip count, png_hash), and variant flags

### How Import Works

1. Parse the edited `.glb` (positions, normals, UVs, bone weights, indices)
2. Load the original `.gpk` + `.sdb` via the DLL (each entry separately for multi-entry GPKs)
3. Match GLB meshes to GR2 meshes by name, pair same-name parts by position order
4. Route meshes to correct GR2 entry via manifest (exact mesh-to-entry mapping)
5. Remap bone weights from GLB joint order to GR2 BoneBinding order by name
6. Patch vertex buffers in DLL memory (in-place or new allocation for topology changes)
7. Patch index buffers if triangle connectivity changed
8. Recompute per-bone bounding boxes (OBB) for frustum culling
9. Serialize each entry back to `.gr2` via the Granny DLL write API
10. LZ4-compress all entries into output `.gpk`

### How `cg3h build` Works

1. Read `mod.json` descriptor (type, character, asset list)
2. Auto-detect game directory from Steam paths
3. For **texture_replace**: build standalone `.pkg` from scratch with custom PNG textures (BC7/BC3 compressed with mipmaps)
4. For **mesh_add**: bundle GLB (new geometry only) + `cg3h_builder.exe` for on-device GPK building
5. Generate H2M Lua companion (`main.lua`) with `rom.game.LoadPackages` calls and deferred GPK build logic
6. Generate Thunderstore `manifest.json`
7. If `--package`: create Thunderstore-ready ZIP with all artifacts

### How Standalone .pkg Builder Works

Creates `.pkg` files from scratch rather than modifying game packages:
1. Compress custom PNG to BC7/BC3/BC1 DDS with mipmaps via `etcpak`
2. Wrap each texture in a 0xAD (Texture2D) chunk with correct XNB headers
3. Write `.pkg` file with proper chunk table and offsets
4. H2M loads the standalone `.pkg` at runtime via `rom.game.LoadPackages`

### How Texture Install Works (Legacy)

1. Read `manifest.json` from the export folder
2. Detect edited textures via `png_hash` comparison (GLB embedded textures) or file modification (standalone PNG/DDS)
3. For PNG edits: compress to BC7/BC3/BC1 DDS with mipmaps via `etcpak`
4. For DDS edits: auto-truncate extra mip levels (paint.net compatibility)
5. Replace texture data in all `.pkg` files containing it (textures are duplicated across biome packages)
6. Update `checksums.txt` with new XXH64 hashes (game validates on load)
7. Register changes in mod registry (`_mods.json`) for uninstall tracking

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

- [`docs/file_formats.md`](docs/file_formats.md) — GPK / SDB / GR2 / PKG binary layouts
- [`docs/texture_pipeline.md`](docs/texture_pipeline.md) — Texture pipeline (material chains, PKG format, DDS export/import)
- [`docs/rendering_pipeline.md`](docs/rendering_pipeline.md) — Engine pipeline (40-byte stride, index normalization)
- [`docs/architecture.md`](docs/architecture.md) — Implementation decisions, write API details, H2M integration
- [`CHANGELOG.md`](CHANGELOG.md) — Release history
