# CG3H — Custom Geometry 3D for Hades II

[![Release](https://img.shields.io/github/v/release/Enderclem0/CG3H)](https://github.com/Enderclem0/CG3H/releases)

Extract, edit, and repack 3D models and textures for Hades II.
Build non-destructive mod packages for [Hell2Modding (H2M)](https://github.com/SGG-Modding/Hell2Modding) — zero game files modified.

## Features

| Feature | Status |
|---|---|
| Export any character to `.glb` (Blender-ready) | Working |
| Import edited `.glb` back to `.gpk` | Working |
| Vertex, UV, normal, and topology edits | Working |
| Bone weight painting from Blender | Working |
| Add new meshes (accessories, custom geometry) | Working |
| Animation export and import | Working |
| Texture export (per-mesh DDS + embedded PNG in GLB) | Working |
| Texture import (PNG/DDS -> standalone .pkg) | Working |
| Multi-texture per character (per-mesh material chain) | Working |
| Multi-entry GPK (characters with multiple mesh entries) | Working |
| Parallel batch export (auto-scaled across CPU cores) | Working |
| H2M mod packages (`cg3h build` -> Thunderstore ZIP) | Working |
| Standalone .pkg builder (custom textures from scratch) | Working |
| PyInstaller exe (`cg3h_builder.exe`, no Python needed) | Working |
| Blender addon v3.0 with CG3H menu | Working |

## Limitations

- **65,535 vertices max per mesh** — the engine uses uint16 index buffers.
- **Adding/removing bones** is not supported — the skeleton is read-only.
- **mesh_replace / mesh_patch CC-free distribution** requires a diff format (planned for v3.1).
- **Animation export is slow** — characters have 600-850 clips. Use `--anim-filter` and `--anim-workers` to limit scope.
- **Extreme deformations** may cause frustum culling artifacts despite OBB recomputation.

## Requirements

For development / CLI usage:

```
pip install numpy pygltflib lz4 Pillow etcpak xxhash
pip install pyinstaller  # only needed to build cg3h_builder.exe
```

The game's `granny2_x64.dll` is required (auto-detected from Steam path).

For end users: **no Python needed** — mesh mods include `cg3h_builder.exe` (29MB standalone), and texture mods ship pre-built `.pkg` files.

---

## Quick Start

### CG3H Mod Builder GUI

```bash
python tools/converter_gui.py
# or double-click tools/Start_gui.bat
```

Three tabs:

- **Create** — Pick a character, export to a mod workspace (GLB + textures + `mod.json`).
- **Build** — Build an H2M-compatible package (GPK + standalone .pkg + Lua companion). Optional Thunderstore ZIP with `--package`. One-click r2modman install.
- **Mods** — View installed mods, detect conflicts, set merge order, disable/remove/rebuild.

### Building a mod from the CLI

```bash
# Build H2M folder structure
python tools/cg3h_build.py

# Build + create Thunderstore-ready ZIP
python tools/cg3h_build.py --package
```

The builder reads `mod.json` from the current directory, auto-detects the game path, and produces an H2M-ready package.

### What the build produces

For **texture_replace** mods:
- Standalone `.pkg` file (built from scratch, not a modified game file)
- H2M Lua companion (`main.lua`)
- Ready for Thunderstore upload

For **mesh_add** mods:
- `.glb` with new meshes only (no copyrighted geometry)
- `cg3h_builder.exe` for on-device GPK building
- Standalone `.pkg` for custom textures
- H2M Lua companion
- Ready for Thunderstore upload

### Mod types

| Type | Description | CC-free |
|---|---|---|
| `texture_replace` | Custom PNG -> standalone .pkg | Yes |
| `mesh_add` | Append new meshes to character | Yes |
| `mesh_replace` | Swap character meshes | Needs v3.1 diff |
| `mesh_patch` | Edit vertices in-place | Needs v3.1 diff |
| `animation_patch` | Edit animation curves | Yes |

See [`docs/mod_spec.md`](docs/mod_spec.md) for the full mod specification.

---

## Blender Addon

1. In Blender: Edit > Preferences > Add-ons > Install
2. Navigate to `blender_addon/cg3h/` and select `__init__.py`
3. Enable "CG3H — Hades II Model Tools"
4. Set the game path and tools path in addon preferences

Features:
- **File > Import > Hades II Model (.gpk)** — import from `Content/GR2/_Optimized/`
- **File > Export > Hades II Model (.gpk)** — export selected meshes + armature
- **CG3H menu** — texture options, animation export, topology change toggle, Build for H2M

## CLI Tools

```bash
# Export a character
cd "C:/Program Files (x86)/Steam/steamapps/common/Hades II/Ship"
python /path/to/tools/gr2_to_gltf.py Melinoe -o Melinoe.glb

# Export with animations
python /path/to/tools/gr2_to_gltf.py Melinoe -o Melinoe.glb --animations

# Import (same topology)
python /path/to/tools/gltf_to_gr2.py Melinoe_edited.glb \
    --gpk ../Content/GR2/_Optimized/Melinoe.gpk \
    --sdb ../Content/GR2/_Optimized/Melinoe.sdb \
    --output-gpk Melinoe_mod.gpk

# Import (topology changes)
python /path/to/tools/gltf_to_gr2.py Melinoe_sculpted.glb \
    --gpk ../Content/GR2/_Optimized/Melinoe.gpk \
    --sdb ../Content/GR2/_Optimized/Melinoe.sdb \
    --output-gpk Melinoe_mod.gpk \
    --allow-topology-change
```

---

## Blender Workflow

### Editing Modes

**Same-topology edits** (default): Move, scale, rotate vertices, sculpt, edit UVs. Vertex count must match exactly.

**Topology changes** (`--allow-topology-change`): Subdivide, decimate, remesh, boolean, extrude — anything that changes vertex/face count. When deleting vertices, use Delete > Faces to avoid dangling geometry.

### Export Settings

```
Format:             glTF Binary (.glb)
Include:            Selected Objects only
Mesh > Normals:     OFF  <-- prevents vertex splitting
Mesh > UVs:         ON
Mesh > Apply Mods:  OFF  (or ON if you used modifiers)
Armature:           ON
```

Normals OFF is important: exporting normals causes Blender to split vertices at normal seams, changing the vertex count.

---

## Repository Layout

```
blender_addon/
  cg3h/__init__.py          Blender addon (v3.0, CG3H menu + Import/Export)
tools/
  gr2_to_gltf.py            Exporter: .gpk + .sdb -> .glb (textures + animations)
  gltf_to_gr2.py            Importer: .glb + .gpk + .sdb -> .gpk
  pkg_texture.py            PKG texture tools + standalone .pkg builder
  cg3h_build.py             H2M mod builder (mod.json -> Thunderstore ZIP)
  mod_merger.py             Multi-mod merger with conflict detection
  converter_gui.py          CG3H Mod Builder GUI (Create/Build/Mods)
  cg3h_builder_entry.py     PyInstaller entry point for cg3h_builder.exe
  gpk_pack.py               GPK archive pack/unpack
  granny_types.py           Dynamic Granny struct offset resolver
  Start_gui.bat             Windows launcher
  debug/                    Diagnostic probes (development only)
tests/
  test_core.py              25 unit tests
  test_integration.py       15 integration tests
  TEST_CHECKLIST.md         Manual test checklist
docs/
  mod_spec.md               Mod specification (types, mod.json, conflicts)
  file_formats.md           Binary format reference (GPK, SDB, GR2, PKG)
  texture_pipeline.md       Texture pipeline (material chains, export/import)
  rendering_pipeline.md     Engine rendering pipeline (40-byte stride)
  architecture.md           Implementation decisions and internals
```

## How It Works

### Export Pipeline

1. LZ4-decompress `.gpk` entries to raw `.gr2` bytes
2. Load `.sdb`, remap strings via `GrannyRemapFileStrings`
3. Walk Granny type definitions at runtime to discover struct offsets
4. Apply 40-byte physical GPU stride (engine invariant)
5. Walk material chains to find per-mesh texture names
6. Extract textures from `.pkg` files as DDS; embed decoded PNG in GLB
7. Pack geometry + skeleton + textures into glTF 2.0 binary
8. Write `manifest.json` with entry mapping and texture metadata

### Import Pipeline

1. Parse edited `.glb` (positions, normals, UVs, bone weights, indices)
2. Load original `.gpk` + `.sdb` via the Granny DLL
3. Match GLB meshes to GR2 meshes by name; route to correct entry via manifest
4. Remap bone weights from GLB joint order to GR2 BoneBinding order
5. Patch vertex/index buffers in DLL memory
6. Recompute per-bone OBB for frustum culling
7. Serialize via Granny write API, LZ4-compress into output `.gpk`

### Build Pipeline (`cg3h build`)

1. Read `mod.json` descriptor
2. Auto-detect game directory from Steam paths
3. For texture_replace: build standalone `.pkg` (BC7/BC3 compressed with mipmaps)
4. For mesh_add: bundle GLB + `cg3h_builder.exe` for on-device GPK building
5. Generate H2M Lua companion and Thunderstore manifest
6. Optionally create Thunderstore ZIP (`--package`)

## Documentation

- [`docs/mod_spec.md`](docs/mod_spec.md) — Mod types, mod.json format, conflict resolution
- [`docs/file_formats.md`](docs/file_formats.md) — GPK / SDB / GR2 / PKG binary layouts
- [`docs/texture_pipeline.md`](docs/texture_pipeline.md) — Texture pipeline and standalone .pkg builder
- [`docs/rendering_pipeline.md`](docs/rendering_pipeline.md) — Engine rendering pipeline (40-byte stride)
- [`docs/architecture.md`](docs/architecture.md) — Implementation decisions and internals
- [`CHANGELOG.md`](CHANGELOG.md) — Release history
