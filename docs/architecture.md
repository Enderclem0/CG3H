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
| `pkg_texture.py` — PKG texture extractor/replacer/compressor + index builder | Done |
| `converter_gui.py` — GUI with Export/Install + parallel batch + mod registry | Done |
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

## Future Work

| Feature | Approach |
|---|---|
| Bone changes | Build new bone array + update every mesh's `BoneBindings` |
| String-stripped output | Fix sec[3] descriptor patching (zeroing f0/f1 isn't sufficient) |
