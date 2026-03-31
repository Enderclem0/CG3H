# The Hades II Rendering Pipeline: How Models Reach the GPU
*A Reverse-Engineering Architecture Document*

## Overview

Hades II uses a highly customized rendering pipeline built on top of the Granny 3D middleware
and a graphics framework called **The Forge**. To maximize GPU performance, Supergiant's engine
enforces strict memory uniformity. It forces every 3D model — whether it's a fully rigged
character or a rigid sword — into the exact same physical memory layout before sending it to
the graphics card.

This document outlines the 5-step process the engine uses to load, format, and push a `.gr2`
model to the GPU.

---

## Step 1: Decompression and String Linking

Unlike standard `.gr2` files, Hades II models are stored in a proprietary "Optimized" format.

1. **LZ4 Decompression**: The engine loads the raw `.gpk` archive and decompresses each entry
   using the LZ4 algorithm (raw block format, no frame header).

2. **The `.sdb` Database**: To save memory, Supergiant strips all text strings (bone names,
   mesh names, material names) out of the `.gr2` file. The engine finds a matching `.sdb`
   (String Database) file, loads it via `GrannyGetStringDatabase`, and maps the names back
   onto the 3D data using `GrannyRemapFileStrings`.

---

## Step 2: The Metadata Lie (Logical vs. Physical Memory)

Once the file is loaded, the engine asks Granny for the model's blueprint (Metadata).

For a **rigid object** (like Zagreus's sword), Granny reports a **32-byte vertex**:

| Field    | Size   |
|----------|--------|
| Position | 12 bytes |
| Normal   | 12 bytes |
| UV       | 8 bytes |
| **Total**| **32 bytes** |

**The Engine Override**: Modern GPUs hate switching between different memory layouts. To keep
the shaders perfectly uniform, the Hades II engine **ignores Granny's 32-byte blueprint**. It
forces every single vertex in the game to occupy **40 bytes** of physical memory.

---

## Step 3: Extracting the Geometry (The 40-Byte Stride)

Because the engine demands 40 bytes per vertex, it uses a hardcoded C++ loop to read the
memory buffer.

- To jump to the next vertex: `pointer = pointer + 10` (where the pointer is a `float*`)
- 10 floats × 4 bytes = **40 bytes**

**Unified Physical Vertex Layout (40 bytes, all mesh types):**

| Offset | Field          | Skinned Mesh         | Rigid Mesh        | Size |
|--------|----------------|----------------------|-------------------|------|
| +0     | Position       | XYZ float32          | XYZ float32       | 12   |
| +12    | Bone Weights   | uint8 × 4            | **padding (zero)**| 4    |
| +16    | Bone Indices   | uint8 × 4            | **padding (zero)**| 4    |
| +20    | Normal         | XYZ float32          | XYZ float32       | 12   |
| +32    | UV             | float32 × 2          | float32 × 2       | 8    |
| **40** | **Total**      |                      |                   |      |

**The Result**: For rigid meshes, the engine intentionally skips 8 bytes of "dead" padding
space per vertex. For skinned meshes, those 8 bytes perfectly fit the Bone Weights and Bone
Indices.

---

## Step 4: Normalizing the Triangles (The Index Buffer)

Granny files can store triangle connection indices as either **16-bit or 32-bit** numbers
depending on mesh size. The engine refuses mixed formats — it wants exactly **16-bit (2-byte)
indices** for everything.

1. Allocates a fresh buffer: `MeshIndexCount * 2 bytes`
2. Calls `GrannyCopyMeshIndices(mesh, 2, destination_buffer)`

The `2` explicitly commands Granny to safely copy and convert all triangles into 16-bit
integers, completely avoiding any corrupted data or mismatched byte sizes.

---

## Step 5: Uploading to The Forge

At this point the engine has exactly what it wants:
- A uniform vertex buffer where every point is exactly **40 bytes** apart
- A uniform index buffer where every triangle instruction is exactly **2 bytes**

Finally, the engine hands these perfectly sanitized buffers to its graphics wrapper
(`ForgeRenderer::PrepareVertexData` and `ForgeRenderer::RequestBufferUpdate`). The Forge
pushes this data directly to the GPU's VRAM via DirectX or Vulkan.

---

## Implications for Modding Tools

| Issue | Cause | Fix |
|-------|-------|-----|
| Rigid meshes export as corrupted "spiky" geometry | Reading with 32-byte Granny stride instead of 40-byte physical stride | Override stride to 40 when `GrannyGetTotalObjectSize` returns 32 |
| Large mesh indices corrupt | Reading raw `Indices16` field — misses meshes with 32-bit source indices | Use `GrannyCopyMeshIndices(mesh, 2, buf)` |
| LSLib fails on Hades II rigid models | LSLib respects Granny metadata (32 bytes); engine has baked 40 bytes | Use custom tools with hardcoded 40-byte override |

---

## Key DLL Functions (Confirmed Exported)

```
GrannyReadEntireFileFromMemory(int size, void* data) → granny_file*
GrannyGetStringDatabase(granny_file*) → granny_string_database*
GrannyRemapFileStrings(granny_file*, granny_string_database*) → bool
GrannyGetFileInfo(granny_file*) → granny_file_info*
GrannyFreeFile(granny_file*)
GrannyGetTotalObjectSize(granny_data_type_definition*) → int   // returns LOGICAL stride
GrannyGetMeshTriangleCount(granny_mesh*) → int
GrannyCopyMeshIndices(granny_mesh*, int bytes_per_index, void* dest)  // normalizes to any width
```
