# Architecture & Implementation Notes

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
| Test suite (25 unit + 15 integration = 40 tests) | Done |
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

### Multi-entry GPK support
Characters with multiple mesh entries (e.g. Hecate has `HecateBattle_Mesh` +
`HecateHub_Mesh`) export ALL entries by default. The `--mesh-entry` flag filters
to specific entries; `--list-entries` inspects what is available. The importer
routes GLB meshes to the correct GR2 entry via the manifest's exact mesh-to-entry
mapping. Each entry is patched and serialized separately, then packed into a
single output GPK.

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

CG3H is a mod builder that produces H2M-compatible packages. H2M provides the runtime
layer: it loads standalone `.pkg` files and runs Lua companions without touching the
game's original assets. This eliminates DLL injection, checksum management,
backup/restore, and direct file modification.

### `cg3h build` Pipeline

The `cg3h_build.py` script reads a `mod.json` descriptor and produces an
H2M-compatible folder structure (optionally packaged as a Thunderstore ZIP):

1. Parse `mod.json` — validate type, character, asset references
2. Auto-detect game directory from Steam library paths
3. **texture_replace**: compress PNG to BC7/BC3 DDS, build standalone `.pkg`
   from scratch (not a modified game package)
4. **mesh_add**: bundle GLB containing only new geometry (no copyrighted meshes),
   include `cg3h_builder.exe` for on-device GPK building
5. **animation_patch**: bundle GLB with modified animation curves
6. Generate `main.lua` (H2M Lua companion)
7. Generate Thunderstore `manifest.json`
8. `--package` flag: ZIP everything for upload

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
- H2M's `rom.game.LoadPackages` loads the standalone `.pkg` at runtime
- No `.pkg_manifest` needed for H2M loading

### H2M Lua companion

Auto-generated `main.lua` included in every mod package:
- `rom.game.LoadPackages` for custom .pkg loading
- `rom.on_import.post` for deferred initialization
- Auto-build GPK on first launch if missing (runs `cg3h_builder.exe`)

### PyInstaller standalone builder

`cg3h_builder.exe` is a PyInstaller-frozen version of the mesh build pipeline:
- Bundles Python + numpy + pygltflib + lz4 + ctypes Granny DLL loader
- 29MB single-file executable
- Included in Thunderstore ZIP for mesh mods only
- Uses the end user's local `granny2_x64.dll` (auto-detected from Steam path)

## Future Work

| Feature | Approach |
|---|---|
| v3.1 diff format | Distribute mesh deltas instead of full geometry (CC-free mesh_replace/mesh_patch) |
| Bone changes | Build new bone array + update every mesh's `BoneBindings` |
| String-stripped output | Fix sec[3] descriptor patching (zeroing f0/f1 isn't sufficient) |
