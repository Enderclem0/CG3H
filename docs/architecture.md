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
| `gltf_to_gr2.py` — importer (golden path) | Done |
| `gpk_pack.py` — GPK pack/unpack | Done |
| `pkg_texture.py` — PKG texture extractor/replacer + index builder | Done |
| `converter_gui.py` — GUI with Export/Import/Install + parallel batch | Done |
| End-to-end DLL verification | Done |
| In-game testing | Done (bounding box culling issue — see README) |

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
mesh, not LODs). The exporter appends `_1`, `_2`, `_3` suffixes to disambiguate.
The importer strips both these and legacy `_LOD` suffixes when matching GLB
meshes back to GR2 meshes.

### Animation export sanitization
Several channel-level fixes prevent corrupt animation data from producing
broken glTF output:
- **Scale channels**: Skip if the 3x3 matrix has significant off-diagonal elements
  (shear/rotation, not pure scale). Skip NaN/Inf/extreme values (>100).
- **Rotation channels**: Normalize quaternions to unit length. Non-unit quaternions
  cause mesh stretching in Blender and other viewers.
- **Translation channels**: Skip NaN/Inf and extreme values (>1000).

## Future Work

| Feature | Approach |
|---|---|
| Texture import (PNG/DDS to PKG) | Replace pixel data in .pkg, regenerate mipmaps if needed |
| Bone changes | Build new bone array + update every mesh's `BoneBindings` |
| String-stripped output | Fix sec[3] descriptor patching (zeroing f0/f1 isn't sufficient) |
