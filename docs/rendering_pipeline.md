# The Hades II Rendering Pipeline

How models reach the GPU and how textures are bound to them, reverse-engineered
from `Hades2.exe` (with PDB symbols) and `granny2_x64.dll`.

Two halves:
1. **Load path** — GPK → GrannyMeshData → GPU buffers (happens once per model entry).
2. **Draw path** — Thing → ModelAnimation → draw command → DoDraw3D → DX12 draw (every frame).

The load path is well understood (Granny SDK + The Forge). The draw path was the
last piece of RE and is what enables clean variant/outfit switching.

---

# Part 1 — Load Path

Hades II uses Granny 3D (RAD Game Tools / Epic) for model data and The Forge as
its GPU framework. The engine enforces strict memory uniformity: every vertex
is forced to a 40-byte physical layout regardless of what Granny's metadata says.

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

For the in-buffer GPU layout, `sgg::gShaderEffects[shader_byte].+0x95c` holds a
stride *value of 20* — not bytes; interpretation is per-shader and bookkeeping.
The DX12 vertex buffer binding uses 40 bytes/vertex; the per-shader "20" is an
internal engine counter.

## Step 3: Index Normalization

The engine demands 16-bit (2-byte) indices for all meshes:
1. Allocates a buffer: `MeshIndexCount * 2 bytes`
2. Calls `GrannyCopyMeshIndices(mesh, 2, dest)` — converts 32-bit to 16-bit if needed

Meshes are limited to 65,535 vertices.

## Step 4: mModelData registration and GPU upload

`sgg::Granny3D::AddModelData` at `0x1401aa990`:

1. Allocates a new `ModelData` node in `sgg::Granny3D::mModelData` (`@0x140552760`) —
   an `eastl::hashtable<HashGuid, ModelData>` keyed by the entry's string hash.
2. For each mesh in the GR2, writes a 0x50-byte `GrannyMeshData` entry into the
   model's mesh vector. Layout (confirmed per-field by disassembly):

| Offset | Field | Notes |
|---|---|---|
| +0x00 | `MeshBinding*` | Granny MeshBinding object |
| +0x08 | `mBoneIndices*` | From `GrannyGetMeshBindingToBoneIndices` |
| +0x10 | `mBoneBindingCount` | uint32 |
| +0x18 | bone-binding OBB array | `_aligned_malloc`-ed, 16×count bytes |
| +0x30 | **`vertex_handle`** (vh) | uint32, written by RBU |
| +0x34 | **`index_handle`** (ih) | uint32, written by RBU |
| +0x38 | **`index_count`** | uint32 (arg `r9d` to RBU) |
| +0x3C | **`vertex_count`** | uint32 (arg `r8` to RBU) |
| +0x40 | **`tex_name_hash`** | StringIntern result — texture name hash |
| +0x44 | **`texture_handle`** | TextureHandle (filled by PrepDraw, read by DoDraw3D fallback) |
| +0x48 | `mesh_name_hash` | StringIntern of mesh name |
| +0x4C | **`mesh_type`** | byte enum: 0 = main, 1 = outline, 2 = shadow |

3. Calls `sgg::ForgeRenderer::RequestBufferUpdate` (`0x1401f7920`) to queue
   vertex + index uploads into the global bump-allocated GPU pool.

## Step 5: The GPU buffer pool

- `sgg::gStaticDrawBuffers` (`@0x141719410`) — `eastl::vector<ForgeGeometryBuffers>`
  - One `ForgeGeometryBuffers` entry (72 bytes) per shader effect's geometry resource.
  - +0x20: `Buffer*` (vertex buffer)
  - +0x40: uint vertex cursor (next free vertex slot)

- `sgg::gShaderEffects` (`@0x141719cf0`) — inline array, stride `0xf28`.
  - +0x960: `uint32_t geo_idx` (index into `gStaticDrawBuffers`)
  - +0x95c: `uint32_t stride` (internal count; not D3D12 vertex stride)

- `sgg::gStaticIndexBuffers` (`@0x1408310f8`) — single `Buffer*` (one shared index buffer).
- `sgg::gStaticIndexBufferOffset` (`@0x140831260`) — monotonic uint cursor.

- `sgg::gBufferResourceUpdateRequests` (`@0x141719490`) —
  `eastl::vector<BufferResourceUpdateRequest>`. `RequestBufferUpdate` pushes here
  under `sgg::gBufferResourceUpdateLock` (`@0x141719468`); the render thread
  drains the queue.

### D3D12 Buffer struct (The Forge)

| Offset | Field |
|---|---|
| +0x00 | `void* pCpuMappedAddress` (filled by `ID3D12Resource::Map`) |
| +0x08 | `D3D12_GPU_VIRTUAL_ADDRESS mDxGpuAddress` |
| +0x28 | `ID3D12Resource* pDxResource` |
| +0x38 | uint64 — low 32 bits are mSize; upper bits are flags |

Called via `mapBuffer(Renderer*, Buffer*, ReadRange*)` at `0x140024520`. Buffers
are persistent-mapped, so `Buffer[+0x00]` is a valid CPU-writable pointer
throughout the game's lifetime — writes go through to GPU-visible memory.

---

# Part 2 — Draw Path

What happens every frame to turn a scene graph Thing into a DX12 draw call.

## Producer side (building the frame's command list)

```
Thing (scene graph)
   │  ::SubmitDraw walks child Things via vtable+0x18
   ▼
sgg::ModelAnimation::PrepDraw        @ 0x140272ac0
   │  allocates a DrawableExt slot via AtomicInc + offset in DRAWABLE_EXTS
   │  for each type=0 mesh:
   │     if cached handle at DrawableExt+0x30 is still valid (generational-tag check) → reuse it
   │     else                → call GameAssetManager::GetTexture(GMD+0x40) → write into GMD+0x44
   │  writes Thing's drawable_idx into ModelAnimation+0x280
   │
sgg::ModelAnimation::Draw3DModel    @ 0x1402738c0
   │  reads hash from ModelAnimation+0x230 and drawable_idx from +0x280
   │  computes bone count, bone matrix array
   │
sgg::DrawManager::Draw3DModel        @ 0x1401e5320
   │  looks up mModelData entry by hash, derives mesh count
   │
sgg::RenderCommands::Draw3D          @ 0x1401fdfb0
   │  appends a 48-byte entry to sDraw3DCmds (under WRITE_CMD_INDEX)
   │
```

### `sDraw3DCmds` entry layout (48 bytes, stride 0x30)

| Offset | Source | Meaning |
|---|---|---|
| +0x18 | arg3 (uint) | various |
| +0x1c | arg1 (uint) | drawable_idx |
| +0x20 | arg2 (uint) | various |
| +0x24 | arg4 (uint) | bone count |
| +0x28 | arg5 (uint) | **HashGuid.mId** (mModelData key) |
| +0x2c | arg6 (byte) | outline flag |
| +0x2d | arg7 (byte) | shadow-cast flag |
| +0x2e | byte literal 0 | thumbnail flag |

Base address: `sgg::RenderCommands::sDraw3DCmds` (`@0x1417404e0`), double-buffered
via `WRITE_CMD_INDEX` / `READ_CMD_INDEX`.

## Consumer side (the render thread)

The dispatch wrapper at `0x1401eb550` calls Setup (`0x1401f8280`) →
DispatchLoop (`0x1401fd0d0`) → Cleanup (`0x1401f8ac0`). The loop iterates the
`sDraw3DCmds` array and, for each entry, invokes:

```
sgg::DrawManager::DoDraw3D(const vector<RenderMesh*>& vec,
                            uint drawable_idx,
                            int param3,
                            HashGuid hash)         @ 0x1401ea960
```

What DoDraw3D reads:

1. `mModelData.find(hash)` → `ModelData*`
   - Then `[+0x10..+0x18]` gives the `GrannyMeshData` vector.
2. `DRAWABLES[drawable_idx]` (base `sgg::RenderCommands::DRAWABLES` `@0x140831930`,
   stride 320 bytes), accessed via the 2nd argument `edi`:
   ```
   rdi = index * 320 + DRAWABLES[WRITE/READ_CMD_INDEX]
   ```
   - +0x5c: `uint32_t resource_idx` (indexes DRAWABLE_EXTS)
   - +0xe0: transform matrix
   - +0x98..+0xcc: misc draw state

3. `DRAWABLE_EXTS[drawable.resource_idx]` (base `sgg::RenderCommands::DRAWABLE_EXTS`
   `@0x140831990`, stride 0x38 = 56 bytes):
   - +0x30: **primary texture handle** (generational: 24-bit index + 8-bit tag)

## Per-mesh rendering loop inside DoDraw3D

For each mesh in the `GrannyMeshData` vector:

```
case GMD[+0x4C] (mesh_type):
  == 1 (outline):       set flag, skip texture setup
  == 2 (shadow):        special shadow branch
  == 0 (main) default:  texture lookup
```

**Texture lookup order for type=0 meshes:**

```
handle = DRAWABLE_EXTS[resource_idx][+0x30]     ; PRIMARY (often 0 for 3D character meshes)
if (handle_index - 1 >= 0x1d4be ||              ; bounds fail
    generational_tag_check_fails) {
    if (GMD[+0x40] == 0) skip                   ; no texture name → don't draw textured
    handle = GMD[+0x44]                          ; FALLBACK (this is what gets used for characters)
    if (!TextureHandle::IsValid(&GMD[+0x44]))   ; secondary bounds/tag check
        skip
}
texture_ptr = TextureHandleManager::Get(handle)
```

In practice, **3D character rendering uses the fallback path almost exclusively**
(`DRAWABLE_EXTS[+0x30]` is only populated by a separate 2D-sprite-ish path).
Characters rely on `GMD+0x44` being populated by PrepDraw each frame.

## Key resolved-call targets in DoDraw3D

| VA | Symbol |
|---|---|
| `0x1401b1610` | `hashtable<HashGuid, ModelData>::find` |
| `0x1401b36c0` | `sgg::HashGuid::StringIntern` |
| `0x1401c42b0` | `sgg::TextureHandle::IsValid` |
| `0x1401c3bd0` | `sgg::TextureHandleManager::Get` |
| `0x1401c42f0` | `sgg::TextureHandle::GetTexture` |
| `0x1401f4e20` | `sgg::ForgeRenderer::CmdRecord_SetResourceDataFloat` |
| `0x1401f55b0` | `CmdRecord_SetResourceDataFloatPerBatch` |
| `0x1401f5870` | `CmdRecord_SetResourceDataFloat4PerBatch` |
| `0x1401f6150` | `CmdRecord_SetResourceDataMatrixPerBatch` |
| `0x1401f64b0` | `CmdRecord_SetResourceDataMatrixArrayPerBatch` |
| `0x1401fb6b0` | `sgg::ForgeRenderer::CmdRecord_Draw` (final submit) |
| `0x1402b5b10` | `sgg::Camera::Instance` |

---

# Part 3 — What This Means For Variant/Outfit Switching (v3.9)

`mModelData` holds any number of entries, each with its own `GrannyMeshData` vector
and `GMD+0x40` texture-name-hashes. Variants can be added to `mModelData` by
giving them distinct entry names in the merged GPK — the game's own
`LoadAllModelAndAnimationData` registers them naturally.

**The pivot is PrepDraw.** It's the function that turns `GMD+0x40` (texture name
hash) into `GMD+0x44` (a valid TextureHandle) by calling
`sgg::GameAssetManager::GetTexture` (`0x1401a8c10`). PrepDraw only walks the
entries the active scene is rendering — variants are never walked, so their
`GMD+0x44` stays zero.

If a hash remap is active (at `[sDraw3DCmds_entry + 0x28]`) but variant's
`GMD+0x44` is zero, DoDraw3D's fallback path returns no texture → **white mesh**.

**Fix, fully engine-aligned**: mimic PrepDraw exactly. For each type=0 mesh in
the variant entry, call `GameAssetManager::GetTexture(GMD+0x40, &handle)` and
write the result into `GMD+0x44`. Once per variant load (the handle is only
invalidated on asset unload).

This gives the exact same texture resolution path the game uses for stock
characters — if the modder's variant ships its own textures under custom names,
those names live in `GMD+0x40` and `GetTexture` resolves them through the
modder's own PKG. If the variant reuses stock texture names, they resolve to
stock's textures. No special casing either way.

**Result**:

- No vcount / icount / topology constraints
- Textures follow the variant's GR2-embedded `MaterialBindings`
- Shadow/outline paths (`type=1`, `type=2`) keep working unchanged
- No mutation of stock state
- Same code path as native content — survives game updates as long as GetTexture
  and the GMD layout hold

Pool usage per variant: roughly `sum(variant_vcount × 40 + variant_icount × 2)`
across all the variant's meshes.

## Drawable pre-sizing (why "stock" entry must be the biggest)

Per-drawable state (bone palette capacity, per-mesh scratch) is allocated
when the scene spawns a Thing bound to an entry name — sized for **whatever
entry is currently in the merged GPK under that name**. Swapping the entry's
mesh data at runtime (via hash remap) works as long as the new target fits
in the pre-allocated drawable state; swapping to a LARGER entry overflows
and instant-crashes. This is why the builder emits:

- Default-name entry (e.g. `HecateHub_Mesh`) = **merged-all-mods**, so the
  drawable is pre-sized for maximum capacity at scene-load.
- Slim variants (per-mod and `{Character}_Stock_V{N}_Mesh`) = strictly
  smaller — always fit when remapped onto the merged drawable.

---

# Part 3b — Static buffer pool sizes

Default budgets from the shipped binary:

- Per-shader-effect vertex buffer: **64 MB** (0x04000000) — hardcoded as
  the 5th arg of `sgg::addShaderEffect → sgg::addStaticVertexBuffers`.
- Single global index buffer: **32 MB** (0x02000000) — hardcoded inside
  `sgg::addStaticVertexBuffers`, allocated once via null-check on
  `sgg::gStaticIndexBuffers`.

Both live in DX12 upload heap (persistent-mapped, so `Buffer+0x00` is a
valid CPU pointer). That means the capacity cost is **system RAM, not
VRAM** — cheap to grow on modern PCs.

## Why the defaults aren't enough for mods

Every mod variant emitted by CG3H adds an entry to `mModelData` and
commits its vertex/index bytes into these pools during `AddModelData`.
Shipping with only vanilla characters fits easily; shipping with
per-mod slim variants + true-stock variants overflows the budget.
Symptom: `Granny3D.cpp:519 RequestBufferUpdate failed` +
`ForgeRenderer.cpp:5352 Ran out of static index buffer memory`;
later-loaded meshes (weapons, enemies) fall back to a `Blank_Mesh`
placeholder.

## H2M pool patches (Hell2Modding/src/main.cpp::my_main)

Two byte-patches applied at DLL attach (before `InitForgeRenderer`
fires):

| Pool | Default | Raised | Scan pattern | Patch |
|---|---|---|---|---|
| Vertex (per-shader) | 64 MB | 128 MB | `48 C7 44 24 20 00 00 00 04 E8` | imm32 high byte `04`→`08` at scan+8 |
| Index (global) | 32 MB | 64 MB | `48 C7 44 24 40 00 00 00 02 48` | imm32 high byte `02`→`04` at scan+8 |

Patterns are currently unique (1 match each). If a future game update
reshuffles the allocator, the patches fail safely (logged warning, no
effect).

## Runtime inspection

`rom.data.dump_pool_stats()` walks `gStaticDrawBuffers` and
`gStaticIndexBuffers` and logs per-pool usage / capacity /
percentage. Note: the `stride` field at `gShaderEffects+0x95c` used
in the % calculation is a bookkeeping counter, NOT the physical
40-byte vertex size, so the "%" numbers print >100% when content is
actually well under capacity. Reliable signals:

- Zero `RequestBufferUpdate failed` lines in `Hades II.log` → vertex
  pool fine.
- Zero `Ran out of static index buffer memory` lines → index pool
  fine.
- Raw `index buf: X MB / Y MB` line in `LogOutput.log` → true index
  usage (`cursor × 2` bytes).

---

# Part 4 — Address recovery summary

Every address referenced here resolves through `Hades2.pdb` via
`big::hades2_symbol_to_address[...]` (see `hell2modding/src/hades2/pdb_symbol_map.hpp`).
On a game update, only the offsets *inside* a function change; the symbol map
rebuilds automatically. The few hand-found raw offsets (inside DoDraw3D for the
shadow-cast cave) are guarded by byte signatures and fail safely if they no
longer match — see [`address_recovery_after_update.md`](address_recovery_after_update.md).

## Addresses currently used by CG3H tooling

| Symbol | Used for |
|---|---|
| `sgg::Granny3D::mModelData` | Model lookup |
| `sgg::Granny3D::AddModelData` | Understanding GMD layout |
| `sgg::HashGuid::Lookup` / `StringIntern` / `gStringBuffer` | Name → hash |
| `sgg::gStaticDrawBuffers` / `gStaticIndexBuffers` | GPU pool metadata |
| `sgg::gShaderEffects` | Per-shader stride / geo_idx |
| `sgg::GameAssetManager::GetTexture` | **GMD+0x44 population** for variants |
| `sgg::ForgeRenderer::RequestBufferUpdate` | GR2 upload path |
| `sgg::DrawManager::DoDraw3D` / `DoDrawShadow3D` / `DoDraw3DThumbnail` / `DoDrawShadowCast3D` | Draw-call detour hooks (v3.8 visibility gate) |
| `sgg::addShaderEffect` | v3.9 pool-size patch anchor (vertex pool: 64→128 MB) |
| `sgg::addStaticVertexBuffers` | v3.9 pool-size patch anchor (index pool: 32→64 MB, inside this function) |
| `sgg::RenderCommands::DRAWABLES` / `DRAWABLE_EXTS` / `sDraw3DCmds` | Pipeline understanding only (not used directly) |

---

# Key DLL functions (`granny2_x64.dll`)

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

---

# Modding implications (load-path)

| Issue | Cause | Fix |
|-------|-------|-----|
| Rigid meshes export as "spiky" geometry | Reading with 32-byte Granny stride instead of 40 | Override stride to 40 when `GrannyGetTotalObjectSize` returns 32 |
| Large mesh indices corrupt | Reading raw `Indices16` — misses 32-bit sources | Use `GrannyCopyMeshIndices(mesh, 2, buf)` |
| LSLib fails on Hades II rigid models | LSLib respects Granny metadata (32 bytes); engine has 40 | Use CG3H with hardcoded 40-byte override |
| Variant geometry renders white | `GMD+0x44` never populated for non-rendered entries | Call `rom.data.populate_entry_textures(variant_name)` once at load |

---

# Appendix A — Hardcoded offset registry

Single source of truth for every struct offset the CG3H / H2M code
assumes. A game update that shifts any of these silently breaks the
corresponding feature. The `Runtime check?` column flags whether we
detect drift automatically (today: no — see task #47 follow-up).

## GrannyMeshData (GMD) — 0x50-byte entry in the model's mesh vector

Symbol: `sgg::Granny3D::AddModelData` writes each field. Read by
`DoDraw3D` (main render) and `ModelAnimation::PrepDraw` (texture
handle refresh).

| Offset | Size | Field | Used by | Runtime check? |
|---|---|---|---|---|
| +0x00 | 8 | `MeshBinding*` | AddModelData, bone pipeline | no |
| +0x08 | 8 | `mBoneIndices*` | AddModelData | no |
| +0x10 | 4 | `mBoneBindingCount` | AddModelData | no |
| +0x18 | 8 | bone-binding OBB array | AddModelData | no |
| +0x30 | 4 | `vertex_handle` (vh) | DoDraw3D | no |
| +0x34 | 4 | `index_handle` (ih) | DoDraw3D | no |
| +0x38 | 4 | `index_count` | DoDraw3D | no |
| +0x3C | 4 | `vertex_count` | DoDraw3D | no |
| +0x40 | 4 | `tex_name_hash` | PrepDraw (as key), DoDraw3D fallback | no |
| +0x44 | 4 | `texture_handle` (TextureHandle) | DoDraw3D fallback; `populate_entry_textures` writes | no |
| +0x48 | 4 | `mesh_name_hash` | `set_mesh_visible` filter (matches target) | no |
| +0x4C | 1 | `mesh_type` byte enum (0=main, 1=outline, 2=shadow) | DoDraw3D switch; `set_mesh_visible` flips to 2 to hide | no |

GMD stride (0x50) is hardcoded in `set_mesh_visible` and
`populate_entry_textures` — both iterate `vec_begin + i * 0x50`.

## ForgeGeometryBuffers — 72-byte entry in `gStaticDrawBuffers` vector

| Offset | Size | Field | Used by | Runtime check? |
|---|---|---|---|---|
| +0x20 | 8 | `Buffer*` (vertex) | RequestBufferUpdate, `dump_pool_stats` | no |
| +0x40 | 4 | uint vertex cursor | RequestBufferUpdate, `dump_pool_stats` | no |

Vector stride 72 bytes (`rax * 9 * 8` in dispatch code).

## The Forge Buffer struct (DX12)

| Offset | Size | Field | Used by | Runtime check? |
|---|---|---|---|---|
| +0x00 | 8 | `void* pCpuMappedAddress` | Option 4 memcpy path | no |
| +0x08 | 8 | `D3D12_GPU_VIRTUAL_ADDRESS mDxGpuAddress` | — | no |
| +0x28 | 8 | `ID3D12Resource* pDxResource` | — | no |
| +0x38 | 8 | uint64 — low 32 = mSize | Bounds check in RBU | no |

## `sgg::gShaderEffects` entry — 0xF28-byte stride

| Offset | Size | Field | Used by | Runtime check? |
|---|---|---|---|---|
| +0x95c | 4 | stride (per-shader byte/bookkeeping counter) | RequestBufferUpdate size check | no |
| +0x960 | 4 | `geo_idx` (index into gStaticDrawBuffers) | RequestBufferUpdate | no |

`dump_pool_stats` used to use +0x95c as "physical vertex stride" but
that field is a bookkeeping counter, not real byte-stride — fixed to
estimate at 40 B/vert.

## sDraw3DCmds entry — 48-byte stride in the dispatch command list

| Offset | Size | Field | Used by | Runtime check? |
|---|---|---|---|---|
| +0x1c | 4 | drawable_idx (arg1) | DoDraw3D | no |
| +0x24 | 4 | bone count (arg4) | DoDraw3D | no |
| +0x28 | 4 | HashGuid.mId (arg5) | Shadow-cast code cave reads/overwrites this | no |
| +0x2c | 1 | outline flag | DoDraw3D | no |
| +0x2d | 1 | shadow-cast flag | Code cave pivots on this | no |
| +0x2e | 1 | thumbnail flag (literal 0) | DoDraw3D | no |

Base: `sgg::RenderCommands::sDraw3DCmds`, double-buffered via
`WRITE_CMD_INDEX` / `READ_CMD_INDEX` globals.

## GR2 file header

| Offset | Size | Field | Used by | Runtime check? |
|---|---|---|---|---|
| +0x20 | 4 | section count | `build_gr2_bytes` passes to `GrannyBeginFile` | no |
| +0x44 | 4 | runtime type tag (`0x80000039` stock, `0x00000000` convert-pending) | Game's auto-conversion path; `reserialize_stock_entry` patches back to stock | no |

## Granny material chain (inside MeshBinding → MaterialBindings)

| Struct | Offset | Size | Field | Used by | Runtime check? |
|---|---|---|---|---|---|
| Model | +0x54 | 8 | `MeshBindings*` | `gltf_to_gr2._apply_patch` | no |
| Material | +0x0C | 4 | maps count | material chain walk | no |
| Material | +0x10 | 8 | `MaterialMap*` | material chain walk | no |
| MaterialMap | +0x08 | 8 | `Material*` (nested) | material chain walk | no |
| Material | +0x14 | 8 | `Texture*` | material chain walk | no |

Most derived offsets for the material walk come from `granny_types.py`
at runtime via `GrannyGetTotalObjectSize` but a handful of Python
paths (gltf_to_gr2.py lines ~1340, 1350, 1677, 1720) still use
literals. They're the most fragile set post-update.

## H2M byte-pattern patches

| Patch | Where | Pattern (hex) | Patch byte | Runtime check? |
|---|---|---|---|---|
| Vertex pool 64→128 MB | `sgg::addShaderEffect` call site | `48 C7 44 24 20 00 00 00 04 E8` | `+8`: `0x04` → `0x08` | **yes** — patch-status summary log |
| Index pool 32→64 MB | `sgg::addStaticVertexBuffers` one-shot alloc | `48 C7 44 24 40 00 00 00 02 48` | `+8`: `0x02` → `0x04` | **yes** — patch-status summary log |
| Shadow-cast code cave | `DoDraw3D + 0x148E4` | cmp/je 7-byte sequence | jmp to allocated cave | partial — byte signature check, fail-open |

## Follow-up (task #47)

Consider a plugin-init "canary" check that reads a known entry (e.g.
`HecateHub_Mesh`) and asserts:
- GMD stride = 0x50 (vector size divisible by 0x50)
- `mesh_type` byte at +0x4C is 0, 1, or 2 on the first mesh
- `tex_name_hash` at +0x40 is non-zero
- ForgeGeometryBuffers Buffer* at +0x20 is non-null, mSize (Buffer+0x38) > 0

If any fails, log a single ERROR and disable variant/accessory
features so the symptoms are "picker missing" instead of "game corrupted".
