# GhidraHades2 — Hades II Modding Toolkit

Reverse-engineering workspace and asset pipeline for Hades II (Supergiant Games).
Extracts 3D models from the game's proprietary `.gpk` format and exports them as standard glTF 2.0 (`.glb`).

## Repository Layout

```
tools/
  gr2_to_gltf.py      CLI exporter: .gpk + .sdb → .glb
  granny_types.py     Dynamic Granny struct resolver (required by gr2_to_gltf.py)
  converter_gui.py    GUI wrapper for the exporter
  Start_gui.bat       Windows launcher for the GUI
docs/
  file_formats.md     Binary format reference — GPK, SDB, GR2, vertex layout
Hades2_Rendering_Pipeline.md  Engine architecture — 40-byte GPU stride, index normalisation
DLL_FIRST_PLAN.md    Current implementation status and work order
MODDING_PLAN.md      Full research plan with risks, Ghidra sessions, future stages
GhidraHades2.gpr     Ghidra project (Hades2.exe disassembly)
```

## Requirements

```
pip install numpy pygltflib lz4
```

Must be run from (or given access to) the game's `Ship/` directory, which contains `granny2_x64.dll`.

```
C:/Program Files (x86)/Steam/steamapps/common/Hades II/Ship/granny2_x64.dll
```

## Quick Start

### Command Line

```bash
cd "C:/Program Files (x86)/Steam/steamapps/common/Hades II/Ship"
python C:/Users/ender/GhidraHades2/tools/gr2_to_gltf.py Melinoe
# → Melinoe.glb
```

Options:
```
  name              Character name (e.g. Melinoe, Zagreus, ThiefMineLayer)
  --gpk-dir DIR     Path to _Optimized/ folder (default: ../Content/GR2/_Optimized)
  --dll PATH        Path to granny2_x64.dll (default: ./granny2_x64.dll)
  --all-lods        Include lower-resolution LOD duplicates
  --mesh-index N    Export only mesh index N
  --debug           Print per-mesh vertex layout and bone binding detail
  -o PATH           Output .glb path (default: <name>.glb)
```

### GUI

```bash
python C:/Users/ender/GhidraHades2/tools/converter_gui.py
# or double-click tools/Start_gui.bat
```

Detects the Steam installation automatically. Select a model from the list, choose an output folder, click Export.

## How It Works

See [`Hades2_Rendering_Pipeline.md`](Hades2_Rendering_Pipeline.md) for the full engine architecture.

Short version:
1. `.gpk` entries are LZ4-decompressed to get a string-stripped `.gr2`
2. Paired `.sdb` provides bone/mesh/material names via `GrannyRemapFileStrings`
3. Granny's type-definition symbols are walked at runtime to find struct offsets (no hardcoding)
4. **40-byte physical GPU stride** is applied — Granny reports 32 bytes for rigid meshes but the engine writes 8 bytes of padding, so custom tools must override the stride
5. `GrannyCopyMeshIndices(mesh, 2, buf)` normalises 16-bit or 32-bit index buffers to 16-bit
6. Geometry and skeleton are packed into a standard glTF 2.0 binary (`.glb`)

## Game Asset Layout

```
<game>/
  Ship/
    granny2_x64.dll               Granny3D runtime (975 exported functions)
  Content/GR2/_Optimized/
    Melinoe.gpk  Melinoe.sdb      Character mesh + string database
    Zagreus.gpk  Zagreus.sdb
    ... (~144 model pairs total)
```

## Documentation

- [`docs/file_formats.md`](docs/file_formats.md) — GPK / SDB / GR2 binary layouts and confirmed struct offsets
- [`Hades2_Rendering_Pipeline.md`](Hades2_Rendering_Pipeline.md) — Engine pipeline: decompression → 40-byte stride → GPU upload
- [`DLL_FIRST_PLAN.md`](DLL_FIRST_PLAN.md) — Current work status and next steps
- [`MODDING_PLAN.md`](MODDING_PLAN.md) — Full research plan including future import/repack stages
