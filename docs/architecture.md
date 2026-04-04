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
| `pkg_texture.py` — PKG texture extractor/replacer/compressor + standalone .pkg builder | Done |
| `converter_gui.py` — GUI with Export/Install + parallel batch + mod registry | Done |
| `cg3h_build.py` — H2M mod builder (mod.json -> Thunderstore ZIP) | Done |
| Standalone .pkg builder (custom textures from scratch) | Done |
| H2M Lua companion generation | Done |
| PyInstaller exe (`cg3h_builder.exe`, 29MB standalone) | Done |
| Texture import pipeline (PNG/DDS -> BC7 DDS -> PKG replacement) | Done |
| Multi-PKG replacement + XXH64 checksum validation | Done |
| Export manifest (`manifest.json`) with per-mesh entry mapping | Done |
| Lua GrannyTexture override parsing | Done |
| End-to-end DLL verification | Done |
| In-game testing | Done (bounding box culling issue — see README) |
| Test suite (25 unit tests) | Done |

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

### Texture import pipeline
Three edit paths are supported (Blender, PNG, DDS). Change detection uses
`png_hash` from the export manifest. PNG-to-DDS compression uses `etcpak`
(BC7/BC3/BC1 with mipmaps). DDS files from external editors are auto-truncated
if they have more mip levels than the original (paint.net compatibility).

### Multi-PKG texture replacement
Textures are often duplicated across biome packages (BiomeF, BiomeHub,
BiomeIHouse, etc.). The manifest tracks ALL `.pkg` files containing each
texture. The installer replaces the texture in every listed package.

### XXH64 checksum validation
The game checks `checksums.txt` at startup. After every `.pkg` replacement,
the tool recalculates the XXH64 hash and updates the corresponding line.
The original `checksums.txt` is backed up and restored on mod uninstall.

### Lua GrannyTexture overrides
48 entities override their GR2 texture at runtime via Lua scripts in the
game's `Scripts/` directory. The exporter parses these overrides and uses
the Lua-specified texture when it differs from the material chain value.

### Mod registry
The GUI maintains `_mods.json` tracking all changes per character (mesh GPK +
texture PKGs + checksums). One-click uninstall restores ALL files at once.
Multi-mod safe: uninstalling one mod re-applies remaining mods' textures.

### GLB texture extraction
The installer can extract edited textures from a GLB file by comparing the
embedded PNG hash against the manifest's original hash. This enables the
Blender editing path without requiring manual PNG export.

## Hell2Modding (H2M) Integration (v3.0)

### Design Philosophy

v3.0 shifts CG3H from a "modify game files" tool to a "build mod packages" tool.
H2M provides the runtime layer: it loads standalone `.pkg` files and runs Lua
companions without touching the game's original assets. This eliminates DLL
injection, checksum management, backup/restore, and direct file modification.

### `cg3h build` Pipeline

The `cg3h_build.py` script reads a `mod.json` descriptor and produces an
H2M-compatible folder structure (optionally packaged as a Thunderstore ZIP):

1. Parse `mod.json` — validate type, character, asset references
2. Auto-detect game directory from Steam library paths
3. **texture_replace**: compress PNG to BC7/BC3 DDS, build standalone `.pkg`
   from scratch (not a modified game package)
4. **mesh_add**: bundle GLB containing only new geometry (no copyrighted meshes),
   include `cg3h_builder.exe` for on-device GPK building
5. Generate `main.lua` (H2M Lua companion)
6. Generate Thunderstore `manifest.json`
7. `--package` flag: ZIP everything for upload

### Standalone .pkg Builder

Creates `.pkg` files from scratch rather than patching game packages:

- Each custom texture is compressed to BC7/BC3/BC1 DDS with full mipchain
- Wrapped in 0xAD (Texture2D) chunks with correct XNB headers
- Written as a new `.pkg` with proper chunk table and offsets
- H2M's `rom.game.LoadPackages` loads the standalone `.pkg` at runtime
- Game resolves textures by name (same `FromFileName` hash lookup)

This means texture mods never touch any game file. The standalone `.pkg` is
side-loaded by H2M and the game's texture resolver picks it up transparently.

### H2M Lua Companion

Auto-generated `main.lua` that CG3H includes in every mod package:

```lua
-- Load standalone texture package
rom.game.LoadPackages({"path/to/custom.pkg"})

-- For mesh mods: auto-build GPK on first launch
rom.on_import.post(function()
    if not file_exists("path/to/Character_mod.gpk") then
        os.execute("cg3h_builder.exe")
    end
end)
```

- `rom.game.LoadPackages` — H2M API for loading additional `.pkg` files
- `rom.on_import.post` — deferred hook, runs after game import phase
- `cg3h_builder.exe` — PyInstaller-bundled builder (29MB), runs the
  `gltf_to_gr2.py` pipeline without requiring Python on the end user's machine

### mod.json Specification

Four mod types, each with different distribution and copyright properties:

| Type | What it does | Assets distributed | CC-free |
|---|---|---|---|
| `texture_replace` | Custom PNG -> standalone .pkg | PNG textures only | Yes |
| `mesh_add` | Append new meshes to character | GLB with new geometry only | Yes |
| `mesh_replace` | Swap character meshes | GLB with replacement geometry | Needs v3.1 diff |
| `mesh_patch` | Edit vertices in-place | GLB with modified geometry | Needs v3.1 diff |

`texture_replace` and `mesh_add` are fully CC-free: no copyrighted game content
is included in the distribution. `mesh_replace` and `mesh_patch` currently require
distributing modified versions of original meshes; a v3.1 diff format will allow
distributing only the delta.

### PyInstaller Standalone Builder

`cg3h_builder.exe` is a PyInstaller-frozen version of the mesh build pipeline:

- Bundles Python + numpy + pygltflib + lz4 + ctypes Granny DLL loader
- 29MB single-file executable
- Included in Thunderstore ZIP for mesh mods only (texture mods don't need it)
- The Lua companion auto-runs it on first launch when the built GPK is missing
- Uses the end user's local `granny2_x64.dll` (auto-detected from Steam path)

### What's No Longer Needed

The H2M integration removes several v2.x requirements:

| v2.x Requirement | Why it's gone |
|---|---|
| DLL injection | H2M handles runtime asset loading natively |
| `checksums.txt` patching | Standalone `.pkg` files bypass checksum validation |
| `_backups/` directory | No game files are modified |
| Mod registry (`_mods.json`) | H2M manages mod lifecycle; uninstall = remove mod folder |

The v2.x legacy workflow (GUI Install tab, direct file modification) still works
for users who prefer it, but is no longer the recommended path.

## Future Work

| Feature | Approach |
|---|---|
| v3.1 diff format | Distribute mesh deltas instead of full geometry (CC-free mesh_replace/mesh_patch) |
| Bone changes | Build new bone array + update every mesh's `BoneBindings` |
| String-stripped output | Fix sec[3] descriptor patching (zeroing f0/f1 isn't sufficient) |
