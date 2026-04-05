# The Hades II Rendering Pipeline

How models reach the GPU. Reverse-engineered from the game executable and `granny2_x64.dll`.

## Overview

Hades II uses Granny 3D (RAD Game Tools / Epic) for model data and The Forge as its GPU
framework. The engine enforces strict memory uniformity: every vertex is forced to a 40-byte
physical layout regardless of what Granny's metadata says.

---

## Step 1: Decompression and String Linking

1. **LZ4 Decompression**: The engine loads the `.gpk` archive and decompresses each entry
   using raw LZ4 block format (no frame header).

2. **String Remapping**: The engine loads the paired `.sdb` (String Database) via
   `GrannyGetStringDatabase`, then maps names back onto the GR2 data using
   `GrannyRemapFileStrings`.

## Step 2: The 40-Byte Stride Override

Granny reports a **32-byte** vertex for rigid meshes (position + normal + UV). The engine
**ignores this** and forces every vertex to **40 bytes**:

| Offset | Field | Skinned Mesh | Rigid Mesh | Size |
|--------|-------|-------------|------------|------|
| +0 | Position | XYZ float32 | XYZ float32 | 12 |
| +12 | Bone Weights | uint8 x 4 | padding (zero) | 4 |
| +16 | Bone Indices | uint8 x 4 | padding (zero) | 4 |
| +20 | Normal | XYZ float32 | XYZ float32 | 12 |
| +32 | UV | float32 x 2 | float32 x 2 | 8 |
| **40** | **Total** | | | |

The extraction loop uses `pointer = pointer + 10` (10 floats x 4 bytes = 40 bytes).
For rigid meshes, 8 bytes of padding sit where bone weights/indices would be.

## Step 3: Index Normalization

The engine demands 16-bit (2-byte) indices for all meshes:

1. Allocates a buffer: `MeshIndexCount * 2 bytes`
2. Calls `GrannyCopyMeshIndices(mesh, 2, dest)` — converts 32-bit to 16-bit if needed

This means meshes are limited to 65,535 vertices.

## Step 4: GPU Upload via The Forge

The sanitized buffers (uniform 40-byte vertices, uniform 16-bit indices) are pushed to
GPU VRAM via `ForgeRenderer::PrepareVertexData` and `ForgeRenderer::RequestBufferUpdate`
through DirectX or Vulkan.

---

## Implications for Modding

| Issue | Cause | Fix |
|-------|-------|-----|
| Rigid meshes export as "spiky" geometry | Reading with 32-byte Granny stride instead of 40 | Override stride to 40 when `GrannyGetTotalObjectSize` returns 32 |
| Large mesh indices corrupt | Reading raw `Indices16` — misses 32-bit sources | Use `GrannyCopyMeshIndices(mesh, 2, buf)` |
| LSLib fails on Hades II rigid models | LSLib respects Granny metadata (32 bytes); engine has 40 | Use CG3H with hardcoded 40-byte override |

## Key DLL Functions

```
GrannyReadEntireFileFromMemory(int size, void* data) -> granny_file*
GrannyGetStringDatabase(granny_file*) -> granny_string_database*
GrannyRemapFileStrings(granny_file*, granny_string_database*) -> bool
GrannyGetFileInfo(granny_file*) -> granny_file_info*
GrannyFreeFile(granny_file*)
GrannyGetTotalObjectSize(granny_data_type_definition*) -> int   // returns LOGICAL stride
GrannyGetMeshTriangleCount(granny_mesh*) -> int
GrannyCopyMeshIndices(granny_mesh*, int bytes_per_index, void* dest)
```
