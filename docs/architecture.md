# Architecture & Implementation Notes

## CG3H's Dual Role

CG3H operates at two stages of the mod lifecycle:

- **Build-time** (mod creator): Export game models to GLB, edit in Blender, then `cg3h build` produces an H2M-compatible package (GPK + standalone PKG + Lua companion) ready for Thunderstore upload.
- **Runtime** (end user): On the player's machine, the shared **CG3HBuilder Thunderstore plugin** scans all installed CG3H mods, merges GLBs targeting the same character via `_merge_glbs`, builds the GPK with a single `convert()` call, and loads custom textures. When multiple mods target the same character, they are fused into a single merged GPK + merged PKG per character, applied in priority order.

Neither stage modifies game files. Build-time strips copyrighted content from distribution. Runtime reconstructs it locally and merges all active mods into one coherent package that H2M loads.

## Core Principle

All struct offsets are discovered at runtime by walking Granny's exported type-definition
symbols (`GrannyFileInfoType`, `GrannyMeshType`, etc.). No hardcoded offsets except the
40-byte physical GPU stride (a Hades II engine invariant, not a Granny SDK property).

## Implementation Status

| Component | Status |
|---|---|
| GPK format (Format B, no `uncompressed_size`) | Done |
| `granny_types.py` — dynamic offset resolver | Done |
| `gr2_to_gltf.py` — exporter (skinned + rigid + textures + animations) | Done |
| `gltf_to_gr2.py` — importer (golden path, multi-entry GPK) | Done |
| `gpk_pack.py` — GPK pack/unpack | Done |
| `pkg_texture.py` — PKG texture extractor/replacer + standalone .pkg builder | Done |
| `converter_gui.py` — CG3H Mod Builder GUI (Create/Build/Mods tabs) | Done |
| `cg3h_build.py` — H2M mod builder (mod.json -> Thunderstore ZIP) | Done |
| `mod_merger.py` — multi-mod merger with conflict detection | Done |
| `cg3h_builder_entry.py` — PyInstaller entry point for cg3h_builder.exe | Done |
| Standalone .pkg builder (custom textures from scratch) | Done |
| H2M Lua companion generation | Done |
| PyInstaller exe (`cg3h_builder.exe`, 29MB standalone) | Done |
| Texture import pipeline (PNG/DDS -> BC7 DDS -> standalone .pkg) | Done |
| Export manifest (`manifest.json`) with per-mesh entry mapping | Done |
| Lua GrannyTexture override parsing | Done |
| End-to-end DLL verification | Done |
| In-game testing | Done |
| Test suite (25 unit + 34 integration + 15 e2e = 74 tests) | Done |
| GitHub Actions release pipeline | Done |

## Key Design Decisions

### Dynamic type walking (`granny_types.py`)
Walk `GrannyBoneType`, `GrannyMeshType`, etc. from the DLL's exported symbol table.
Validation against known-correct offsets runs at startup — fails loudly, never silently.

### 40-byte stride override (hardcoded)
`GrannyGetTotalObjectSize` returns the **logical** stride (32 for rigid meshes). The
**physical** stride is always 40. Detection: `if logical_stride == 32 then physical = 40`.

### Index extraction via `GrannyCopyMeshIndices`
Raw `Indices16` reading only works for meshes already storing 16-bit indices.
`GrannyCopyMeshIndices(mesh, 2, buf)` handles both 16-bit and 32-bit sources.

### Golden path serialization (param3=0)
The DLL's `GrannyBeginFileDataTreeWriting` accepts a param3 argument:
- **param3=0**: embeds strings inline, produces valid section descriptors
- **param3=1**: writes SDB indices but produces broken section descriptors
  (sec[3] becomes file-resident at wrong offset, crashes `GrannyRemapFileStrings`)

We use param3=0. `GrannyRemapFileStrings` returns False (no-op), but all data is valid.

### Vertex patching in DLL memory
Vertex data is modified by `ctypes.memmove` directly into the DLL's loaded data buffers
before serialization. The DLL's writer then serializes the modified data tree, including
the patched vertices, into a new GR2 file.

## Write API Call Sequence

```
GrannyGetFileInfoType()                     -> Blueprint ptr
GrannyBeginFile(SectionCount, 0, Magic)     -> builder
GrannyBeginFileDataTreeWriting(Blueprint, fi, 0, 0) -> tree_writer
GrannyWriteDataTreeToFileBuilder(tree_writer, builder) -> bool
GrannyEndFileDataTreeWriting(tree_writer)
GrannyCreatePlatformFileWriter(path, 1)     -> pw
GrannyEndFileToWriter(builder, pw)          -> bool
vtable[0](pw)                               -> cleanup (flush + close + free)
```

Do NOT use `GrannyInitializeFileWriter` (corrupts the dispatch table).

### Per-mesh texture assignment
Textures are resolved per mesh by walking the GR2 material chain:
`mesh -> MaterialBindings -> Material -> Maps -> nested Material -> Texture -> FromFileName`.
Each mesh gets its own glTF material with the correct embedded PNG texture.
Three fallback levels: material chain, fi->Textures, name-based guessing.

### New material creation (mesh_add)
New meshes added in Blender can have custom textures. The importer creates new
`granny_material`, `granny_material_map`, and `granny_texture` structs in DLL memory,
wires them into the material chain, and expands `fi->Materials` and `fi->Textures`.
The game resolves custom textures via the `FromFileName` hash lookup (`"GR2/" + basename`).

### Texture index for batch export
`pkg_texture.py` builds a `_texture_index.json` by scanning all `.pkg` files once.
Each subsequent export reads the small JSON instead of rescanning hundreds of MB.
The GUI pre-builds this index before launching parallel export workers.

### PKG format: 0xAA (Texture3D) parsing fix
The size field in 0xAA entries is big-endian (byte-swapped), same as 0xAD (Texture2D).
Reading it as little-endian produced a wrong offset that caused the scanner to skip
the rest of the chunk, missing all subsequent textures. Both 0xAA and 0xAD are
XNB-wrapped with identical internal texture format.

### Parallel batch export
The GUI runs up to `cpu_count` parallel subprocesses for batch export.
Each subprocess gets its own `gr2_to_gltf.py` invocation. Non-blocking stdout
is handled via reader threads per subprocess. When animations are enabled,
the GUI auto-scales: fewer outer workers, more inner `--anim-workers` per
character. Animation extraction within each character uses `multiprocessing.Pool`.

### Duplicate mesh naming
GR2 files often contain multiple meshes with the same name (split parts of one
mesh, not LODs). The exporter appends `_2`, `_3` suffixes to disambiguate.
The importer strips both these and legacy `_LOD` suffixes when matching GLB
meshes back to GR2 meshes.

### Multi-entry GPK (export only — import is single-entry)
Characters with multiple mesh entries (e.g. Hecate has `HecateBattle_Mesh` +
`HecateHub_Mesh`) export ALL entries by default. The `--mesh-entry` flag filters
to specific entries; `--list-entries` inspects what is available.

The **importer** currently patches the body entry only (`{Character}_Mesh`).
All GLB meshes (existing + new) are routed to this single entry and serialized
in one pass.  This is required for custom MaterialBindings to survive Granny's
serialization — a double load→serialize cycle corrupts custom material pointers.
Full multi-entry import support is planned for v3.1.

### Animation export sanitization
Several channel-level fixes prevent corrupt animation data from producing
broken glTF output:
- **Scale channels**: Skip if the 3x3 matrix has significant off-diagonal elements
  (shear/rotation, not pure scale). Skip NaN/Inf/extreme values (>100).
- **Rotation channels**: Normalize quaternions to unit length. Non-unit quaternions
  cause mesh stretching in Blender and other viewers.
- **Translation channels**: Skip NaN/Inf and extreme values (>1000).
- **Gap-fill partial channels**: If a bone has any animated channel, emit all three
  (position + rotation + scale) with rest pose constants. Prevents Blender from
  producing bogus scale on partially-animated bones.
- **_static suffix stripping**: Some animation skeletons use a `_static` suffix on
  bone names; stripped for track-to-bone matching.
- **bone_index field**: Preserved in track data for correct bone targeting.

## H2M Integration

### Design Philosophy

CG3H produces H2M-compatible packages **and** runs on end-user machines to assemble
the final mod artifacts. H2M provides the game-side runtime: it loads standalone `.pkg`
files and runs Lua companions without touching game assets. This eliminates DLL
injection, checksum management, backup/restore, and direct file modification.

### Build-Time: `cg3h build` Pipeline

The `cg3h_build.py` script reads a `mod.json` descriptor and produces an
H2M-compatible folder structure (optionally packaged as a Thunderstore ZIP):

1. Parse `mod.json` — validate type, character, asset references
2. Auto-detect game directory from Steam library paths
3. **texture_replace**: compress PNG to BC7/BC3 DDS, build standalone `.pkg`
   from scratch (not a modified game package)
4. **mesh_add**: bundle GLB containing only new geometry (no copyrighted meshes);
   the shared CG3HBuilder plugin handles on-device GPK building at runtime
5. **animation_patch**: bundle GLB with modified animation curves
6. Generate Thunderstore `manifest.json`
7. `--package` flag: ZIP everything for upload (mods are data-only, no `main.lua`)

### Operation-based system

Mods infer their operations from the assets present. A single mod can perform
multiple operations (e.g. mesh_add + texture_replace). `_infer_operations`
examines what assets the mod ships and determines what build steps to run.

### Multi-mod merger (`mod_merger.py`)

When multiple mods target the same character:
1. Scans installed mods, groups by character
2. Sequential merge: each mod applied to previous output
3. Merged PKG combines all custom textures
4. `cg3h_mod_priority.json` controls merge order (higher index = applied later = wins)

### Conflict detection

Per-operation analysis:
- Same texture replaced by multiple mods = conflict
- Multiple mesh_replace for same character = conflict
- mesh_add + mesh_add = compatible (merged)
- Different animation filters = compatible

### Smart data stripping

Thunderstore ZIPs only contain changed data:
- Meshes: compared by vertex/index count against manifest
- Textures: compared by PNG hash against manifest
- Animations: compared by content hash
- Unchanged assets stripped from distribution

### Standalone .pkg builder

Creates `.pkg` files from scratch rather than patching game packages:
- Each custom texture is compressed to BC7/BC3/BC1 DDS with full mipchain
- Wrapped in 0xAD (Texture2D) chunks with correct XNB headers
- Written as a new `.pkg` with proper chunk table and offsets
- No `.pkg_manifest` needed (GR2 model textures are NOT in manifest files)

### H2M texture loading (confirmed via decompilation + hooks)

Two loading mechanisms exist — they are NOT interchangeable:

**`rom.game.LoadPackages` (Lua):**
- Calls `sgg::GameAssetManager::LoadPackage` but does NOT call `ReadTexture2D`
- Registers package metadata only — 0 bytes of texture data processed ("0Mb" logged)
- Can REPLACE existing textures via `mLoadedTexture2DHash` cache pre-seeding
- Cannot register NEW texture names (hash never created in `mTextureHandles`)

**`load_package_overrides_set` (Lua) → triggers REAL `LoadPackage` C++ pipeline:**
- Hooks `sgg::GameAssetManager::LoadPackage` at C++ level
- When target package loads: `ReadPackageFile` → `ReadTexture2D` is called
- Creates new `TextureHandle` entries for NEW texture names (Path A in ReadTexture2D)
- Works for characters that load AFTER override is set (second pass / scene transition)
- Does NOT work for always-loaded characters (Melinoe) — they load before hashes are ready

**`rom.data.get_hash_guid_from_string` returns 0 on first Lua load** (hash system not initialized).
Valid hashes only available after scene transition (second Lua load).

### H2M Lua companion (CG3HBuilder plugin)

The shared CG3HBuilder Thunderstore plugin contains a single `main.lua` that handles
all runtime logic. Individual mods are data-only and ship no Lua code.
- Scans all installed CG3H mods on startup
- Merges GLBs targeting the same character, builds GPK with cache invalidation
- `load_package_overrides_set` for custom texture loading (biome override approach)
- `LoadPackages` via `rom.on_import.post` for replacing existing texture names

### Custom texture limitation: always-loaded characters

Characters like Melinoe whose GPK loads during the first pass (before `get_hash_guid_from_string`
works) cannot currently have NEW custom textures. Their `AddModelData` runs once and is rejected
on subsequent passes ("Failed to insert existing ModelFile"). Texture REPLACEMENT (same name)
works via `LoadPackages` cache pre-seeding. See `docs/texture_loading_deep_dive.md` for full
decompilation findings.

### PyInstaller standalone builder

`cg3h_builder.exe` is a PyInstaller-frozen version of the mesh build pipeline:
- Bundles Python + numpy + pygltflib + lz4 + ctypes Granny DLL loader
- 29MB single-file executable
- Included in the shared CG3HBuilder Thunderstore plugin (not in individual mods)
- Uses the end user's local `granny2_x64.dll` (auto-detected from Steam path)

### Runtime: On-Device GPK Build

When a mesh mod is installed, the end user does not receive a pre-built GPK (that would
contain copyrighted geometry). Instead, the CG3HBuilder plugin always runs `cg3h_builder.exe`
on startup with cache invalidation, which:

1. Reads the shipped GLB (custom meshes only) + the player's local `.gpk` + `.sdb`
2. Appends new meshes / replaces meshes / patches vertices using the same import pipeline as `gltf_to_gr2.py`
3. Writes the output GPK into CG3HBuilder's `plugins_data` directory
4. H2M picks up the GPK on the next load

The built GPK is cached locally and rebuilt when mod content changes.

### Runtime: Multi-Mod Merger (`mod_merger.py`)

When multiple mods target the same character, CG3H fuses them into a single package.
Two mods cannot each ship their own `Melinoe.gpk` — the merger resolves this.

**Merge flow:**

1. **Scan** — walks the H2M `plugins` + `plugins_data` directories for all installed CG3H mods (identified by `mod.json` with `format: "cg3h-mod"`)
2. **Group** — clusters mods by target character
3. **Order** — reads `cg3h_mod_priority.json` for merge order (higher index = applied later = wins conflicts). Auto-generates a default alphabetical order if missing.
4. **Conflict check** — per-operation analysis before merging:
   - Multiple `mesh_replace` for the same character → hard conflict (blocked)
   - Same texture replaced by multiple mods → hard conflict (blocked)
   - `mesh_add` + `mesh_add` → compatible (both appended)
   - `mesh_add` + `mesh_replace` → warning (may not interact well)
   - Different animation filters → compatible
5. **Single-pass merge** — uses `_merge_glbs` to fuse all mods' GLBs into one merged GLB per character, then a single `convert()` call builds the final GPK (no intermediate GPKs or chaining)
6. **PKG merge** — collects all custom textures from all mods and builds a single `CG3H-Merged-<Character>.pkg`

**Output:** `plugins_data/CG3H-Merged-<Character>/` containing the merged GPK, merged PKG, and merged Lua companion. H2M loads this as a single mod.

**Trigger points:**
- GUI Mods tab → Refresh, Open Folder, Remove buttons
- CLI: `python mod_merger.py <r2_dir> [--game-dir DIR] [--character NAME]`
- CG3HBuilder plugin auto-build on startup (with cache invalidation)

## Future Work

| Feature | Approach |
|---|---|
| v3.1 diff format | Distribute mesh deltas instead of full geometry (CC-free mesh_replace/mesh_patch) |
| Bone changes | Build new bone array + update every mesh's `BoneBindings` |
| String-stripped output | Fix sec[3] descriptor patching (zeroing f0/f1 isn't sufficient) |
