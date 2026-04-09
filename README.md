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
| Custom textures on new meshes | Working |
| Animation export and import | Working |
| Texture export (per-mesh DDS + embedded PNG in GLB) | Working |
| Texture import (PNG/DDS -> standalone .pkg) | Working |
| Multi-texture per character (per-mesh material chain) | Working |
| Parallel batch export (auto-scaled across CPU cores) | Working |
| H2M mod packages (Thunderstore ZIP) | Working |
| Runtime GPK building (no copyrighted data distributed) | Working |
| Multi-mod merging (multiple mods on same character) | Working |
| Blender addon (self-contained, Import/Export) | Working |
| Multi-entry GPK (characters with multiple mesh entries) | Working |
| Per-entry mesh routing (battle-only / hub-only meshes) | Working |
| Skeleton merge across entries | Working |
| Multi-mod animation merging | Working |

## Known Limitations

- **New mesh bone bindings** — new meshes inherit bone bindings from the best-matching existing mesh. If your mesh uses bones not in any existing mesh's bindings, weights fall back to root. Bone binding expansion planned for v4.0.
- **65,535 vertices max per mesh** — the engine uses uint16 index buffers.
- **Adding/removing bones** is not supported — the skeleton is read-only.
- **Animation export is slow** — characters have 600-850 clips. Use `--anim-filter` and `--anim-workers` to limit scope.
- **Extreme deformations** may cause frustum culling artifacts despite OBB recomputation.
- **Requires H2M patches** — `rom.data.add_granny_file` API and GPK exact-match fix (PRs pending).

## Requirements

For development / CLI usage:

```
pip install -r requirements.txt
pip install pyinstaller  # only needed to build executables
```

The game's `granny2_x64.dll` is required (auto-detected from Steam registry + `libraryfolders.vdf`).

For end users: **no Python needed** — install the [CG3HBuilder](https://thunderstore.io/c/hades-ii/p/Enderclem/CG3HBuilder/) plugin via r2modman, which handles GPK building and texture loading at runtime.

---

## Quick Start

### CG3H Mod Builder GUI

```bash
python tools/converter_gui.py
# or double-click tools/Start_gui.bat
```

Three tabs:

- **Create** — Pick a character, select mesh entries (checkboxes for multi-entry characters), export to a mod workspace.
- **Build** — Set mod name + author, build Thunderstore package (GLB + .pkg + manifest). One-click r2modman install.
- **Mods** — View installed CG3H mods, CG3HBuilder status, GPK build state.

### Building a mod from the CLI

```bash
# Build H2M folder structure
python tools/cg3h_build.py <mod_dir>

# Build + create Thunderstore-ready ZIP
python tools/cg3h_build.py <mod_dir> --package
```

The builder reads `mod.json` from the specified directory, auto-detects the game path, and produces an H2M-ready package.

### What the build produces

All mods are **data-only** packages (no exe, no Lua):
- `.glb` with new/modified meshes
- `mod.json` descriptor
- Standalone `.pkg` for custom textures (auto-detected from GLB)
- H2M plugin stub (manifest with CG3HBuilder dependency)
- Ready for Thunderstore upload

The shared **CG3HBuilder** plugin (installed once) handles all runtime logic: scanning mods, merging GLBs per character, building GPKs, and loading textures.

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

Self-contained addon (55MB ZIP) — no Python dependencies needed.

1. In Blender: Edit > Preferences > Add-ons > Install from Disk
2. Select `cg3h_blender_addon.zip` from the [releases page](https://github.com/Enderclem0/CG3H/releases)
3. Enable "CG3H — Hades II Model Tools"
4. Set the game path in addon preferences

Features:
- **File > Import > Hades II Model (.gpk)** — import all mesh entries, textures, and animations
- **File > Export > Hades II Mod (CG3H)** — export as a complete mod (GLB + mod.json + PKG + Thunderstore ZIP)
- **CG3H sidebar panel** — assign new meshes to specific entries (e.g. battle-only, hub-only) for multi-entry characters
- Game path auto-detected from Steam registry

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
  cg3h/__init__.py          Blender addon (Import/Export + mod packaging)
tools/
  gr2_to_gltf.py            Exporter: .gpk + .sdb -> .glb (textures + animations)
  gltf_to_gr2.py            Importer: .glb + .gpk + .sdb -> .gpk
  pkg_texture.py            PKG texture tools + standalone .pkg builder
  cg3h_build.py             H2M mod builder (mod.json -> Thunderstore ZIP)
  mod_merger.py             Multi-mod merger with conflict detection
  converter_gui.py          CG3H Mod Builder GUI (Create/Build/Mods)
  cg3h_builder_entry.py     Runtime GPK builder + GLB merger (PyInstaller entry point)
  cg3h_constants.py         Shared constants (Steam paths, dependency versions)
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

1. LZ4-decompress all `_Mesh` entries from `.gpk`
2. Load `.sdb`, remap strings via `GrannyRemapFileStrings`
3. Walk Granny type definitions at runtime to discover struct offsets
4. Merge skeletons across entries (union of all unique bones)
5. Apply 40-byte physical GPU stride (engine invariant)
6. Walk material chains to find per-mesh texture names
7. Extract textures from `.pkg` files as DDS; embed decoded PNG in GLB (with stale cache fallback)
8. Pack geometry + skeleton + textures into glTF 2.0 binary
9. Write `manifest.json` with per-mesh entry mapping and texture metadata

### Import Pipeline

1. Parse edited `.glb` (positions, normals, UVs, bone weights, indices)
2. Load original `.gpk` + `.sdb` via the Granny DLL (SDB loaded once, shared across entries)
3. Route GLB meshes to GR2 entries via manifest; new meshes routed via `new_mesh_routing` in mod.json
4. For each entry: load GR2, patch vertex data, select template by bone overlap, serialize
5. Remap bone weights from GLB joint order to GR2 BoneBinding order
6. Recompute per-bone OBB for frustum culling
7. LZ4-compress all entries into output `.gpk`

### Build Pipeline (`cg3h_build.py`)

1. Read `mod.json` descriptor
2. Auto-detect textures from GLB (new meshes only)
3. Build standalone `.pkg` for custom textures (BC7/BC3 compressed with mipmaps)
4. Strip unchanged meshes from GLB (Thunderstore packaging only)
5. Generate H2M plugin stub with CG3HBuilder dependency
6. Optionally create Thunderstore ZIP (`--package`)

### Runtime Pipeline (CG3HBuilder)

1. Scan `plugins_data/` for installed CG3H mods
2. Group by character, merge GLBs from multiple mods
3. Build GPK on first launch (cached, rebuilds when mods change)
4. Register GPKs via `rom.data.add_granny_file` (no restart needed)
5. Load custom textures via biome overrides + LoadPackages

## Documentation

- [`docs/mod_spec.md`](docs/mod_spec.md) — Mod types, mod.json format, conflict resolution
- [`docs/file_formats.md`](docs/file_formats.md) — GPK / SDB / GR2 / PKG binary layouts
- [`docs/texture_pipeline.md`](docs/texture_pipeline.md) — Texture pipeline and standalone .pkg builder
- [`docs/rendering_pipeline.md`](docs/rendering_pipeline.md) — Engine rendering pipeline (40-byte stride)
- [`docs/architecture.md`](docs/architecture.md) — Implementation decisions and internals
- [`CHANGELOG.md`](CHANGELOG.md) — Release history
