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
| `mod_info.py` — mod metadata + conflict detection helpers | Done |
| `cg3h_builder_entry.py` — runtime GLB merger + GPK builder (CG3HBuilder plugin) | Done |
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

### BoneBindings data flow

Each GR2 mesh stores a `BoneBindings` array — the subset of skeleton bones whose
indices can appear in that mesh's vertex bone indices. Weights painted on bones
outside this set silently fall back to bone 0 (root) at engine load time.

- **Exporter** (`tools/gr2_to_gltf.py`): `read_mesh_data()` extracts the bone
  binding name list from the GR2 mesh; `build_manifest()` writes it as
  `meshes[i].bb_names` in `manifest.json`.
- **Blender addon import** (`blender_addon/cg3h/__init__.py`): `CG3H_OT_Import`
  caches the manifest JSON on `scene.cg3h_manifest_json`. Pure helpers in
  `blender_addon/cg3h/cg3h_core.py` consume the cache:
  - `select_template(manifest, active_bones, restrict_entries=None)` — picks
    the existing mesh with the best bone overlap as a template for new meshes.
  - `find_weight_violations(mesh_vertex_data, bb_names_lookup)` — flags any
    non-zero weight on a bone outside the resolved BoneBindings.
- **Bone visibility presets**: the scene-level `cg3h_bone_preset` enum drives
  a sidebar dropdown with arrow-button cycling.  Presets are `WHOLE` (every
  bone), `ALL` (union of routed bb_names), `E:<entry>` (one entry's union),
  and `M:<mesh>` (one template's bones).  Items filter by the active mesh's
  routing checkboxes (`cg3h_entry_*`), and the apply callback toggles
  `bone.hide` directly so the chosen subset is visible in Object/Pose/Weight
  Paint mode.  Original hide state is saved on the armature
  (`["_cg3h_saved_hide"]`) and restored on `WHOLE` or addon disable.
- **Pre-export validation**: `CG3H_OT_Export._check_bone_bindings()` walks
  selected meshes' vertex groups, resolves the allowed bones per mesh
  (template's for new, own for originals), counts non-zero weighted vertices,
  and surfaces violations through a confirmation dialog
  (`CG3H_OT_ExportViolationsConfirm`).

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

The **importer** patches all `_Mesh` entries (multi-entry shipped in v3.1).
Each entry runs an independent load → patch → serialize cycle, with the SDB
loaded once and shared across entries.  GLB meshes are routed to the right
entry via `_build_entry_routing()`, which reads `manifest.json` for original
meshes and `target.new_mesh_routing` in mod.json for new meshes added by
modders.  `_keepalive` is held until after every entry has been serialized
so custom MaterialBindings survive the per-entry cycles.

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

### Multi-mod merging at runtime (CG3HBuilder)

When multiple mods target the same character, merging happens at game launch
inside the CG3HBuilder plugin (`cg3h_builder_entry.py`):
1. Scans `plugins_data/` for installed CG3H mods
2. Groups by target character
3. Single-pass GLB merge via `_merge_glbs()` (avoids the double-serialize
   problem of the old sequential approach)
4. Single `convert()` call produces the final GPK

The build-time helpers (`scan_cg3h_mods`, `group_by_character`,
`check_conflicts`) live in `tools/mod_info.py` and are used by the GUI
and tests for pre-flight conflict reporting.

### Conflict detection

Per-operation analysis:
- Same texture replaced by multiple mods = conflict
- Multiple mesh_replace for same character = conflict
- mesh_add + mesh_add = compatible (merged)
- Different animation filters = compatible

### Smart data stripping (v3.3+)

Thunderstore ZIPs only contain changed data:
- **Meshes**: `_is_mesh_changed()` in `cg3h_build.py` compares the GLB
  against `.baseline_positions.npz` (a per-mesh `(N, 3) float32` array
  saved at export time).  Same-count meshes use a 1e-4 tolerance to absorb
  Blender's ~1e-5 float noise.  Different-count meshes are still considered
  unchanged if the unique `(UV, coarse position)` count is less than the
  original — this handles Blender's normal-split vertex inflation, which
  otherwise would falsely tag every roundtripped mesh as edited.
- **Textures**: PNG hash compared against the manifest's `png_hash` field.
- **Animations**: content hash compared against the per-animation hash
  stored in the GLB's `extras` field.
- The baseline file is local-only — excluded from Thunderstore ZIPs and
  added to `.gitignore`.

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

### Runtime: Multi-Mod Merger (`cg3h_builder_entry.py`)

When multiple mods target the same character, CG3HBuilder fuses them into a
single GPK at game launch.  Two mods cannot each ship their own `Melinoe.gpk`
— the runtime merger resolves this.

**Merge flow:**

1. **Scan** — walks `plugins_data/` for all installed CG3H mods (identified by `mod.json` with `format: "cg3h-mod"`)
2. **Group** — clusters mods by target character
3. **Conflict check** — per-operation analysis before merging:
   - Multiple `mesh_replace` for the same character → hard conflict (blocked)
   - Same texture replaced by multiple mods → hard conflict (blocked)
   - `mesh_add` + `mesh_add` → compatible (both appended; same mesh names auto-prefixed with mod id)
   - `mesh_add` + `mesh_replace` → warning (may not interact well)
   - Different animation filters → compatible
4. **Single-pass merge** — uses `_merge_glbs` to fuse all mods' GLBs into one merged GLB per character (meshes, materials, textures, and animations), then a single `convert()` call builds the final GPK.  Animations are merged with name-based node remapping; same animation name across mods: last mod wins.

**Output:** `plugins_data/CG3HBuilder/{character}.gpk` registered with H2M via `rom.data.add_granny_file`.

**Trigger points:**
- CG3HBuilder plugin auto-build on startup (with cache invalidation)
- Build-time conflict reporting via `tools/mod_info.py:check_conflicts()`

## Future Work

| Feature | Approach |
|---|---|
| In-game mod manager | NativeMenu integration to enable/disable mods at runtime; outfit cycling between mesh variants |
| Skeleton editing | Build new bone arrays + update every mesh's `BoneBindings`, expand binding sets so new meshes can use bones not in the template |
| Native animation codec | Use Granny's curve evaluation/builder API directly instead of the Python decoder for sub-second full-character animation export |
