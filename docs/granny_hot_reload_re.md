# Granny3D Hot-Reload RE — Findings for v3.8+

**Goal:** expose a `rom.data.reload_character_model("Hecate")` Lua API in
Hell2Modding that evicts one character's parsed model state from the
game's in-memory caches and calls the game's own `LoadModelData` to
re-read the file via our `fsAppendPathComponent` redirect.

This doc records everything we learned by decompiling `Hades2.exe`
through Ghidra. The eventual goal is a concrete H2M patch that plugs
into the existing file-redirect system from v3.1 (`add_granny_file`).

---

## Why this is hard

Hades II loads every character's Granny model file at startup via
`sgg::Granny3D::LoadAllModelAndAnimationData` (address
`0x1401a9430`). The parsed data lives in three caches that persist for
the whole session. There is **no per-character reload path** in the
shipped game — the only cleanup that runs is `sgg::Granny3D::Exit`
(`0x1401aaf20`) on shutdown, which empties every cache in bulk.

Hell2Modding already hooks `LoadAllModelAndAnimationData` with a
`static bool call_it_once` guard because calling it a second time
crashes. The crash is almost certainly caused by accumulated leaks and
stale pointers inside `mModelData` after the first call mutates state
that the second call then re-enters.

To hot-reload cleanly we have to do ourselves what the game never does:
destroy one character's entry in each cache, then trigger a single
re-load via the per-character `LoadModelData` function (which the game
*does* have).

---

## The three caches

All three are `eastl::hash_map<HashGuid, T>` globals in
`sgg::Granny3D`. Layout common to all of them (because they share the
EASTL hashtable template):

| Offset | Field                    |
| ------ | ------------------------ |
| `+0x00` | allocator / vtable      |
| `+0x08` | bucket array pointer    |
| `+0x10` | bucket count            |
| `+0x18` | element count           |
| `+0x20` | rehash policy state     |

Empty state is the sentinel `eastl::gpEmptyBucketArray` as the bucket
pointer with count = 1.

### `mModelFiles` — `HashGuid → GrannyFile` struct

Small cache, ~48 bytes per entry. Node value (offset `+0x08` into the
node) layout:

| Node offset | Field             |
| ----------- | ----------------- |
| `+0x00`     | `mId` (uint key)  |
| `+0x08`     | `mFile` (`granny_file*`)           |
| `+0x10`     | `mFileInfo` (`granny_file_info*`)  |
| `+0x18`     | `mModel` (`granny_model*` or lightweight stub) |
| `+0x20`     | `isUsingTempModel` (byte) + padding |
| `+0x28`     | `mpNext`          |

This is the cache `LoadModelData` checks first. The critical early-exit
rejection inside `LoadModelData` is:

```
if (cached.mFileInfo != nullptr || cached.mModel != nullptr) return;
```

If **either** field is non-null, `LoadModelData` bails without re-reading.

`UnloadModelFiles` (`0x1401ab3xx`-ish, we didn't note the exact start)
is a **soft** teardown that frees the Granny parse state and nulls
`mFile` and `mFileInfo` — but **allocates a lightweight skeleton stub**
and stores its pointer in `mModel`. So after `UnloadModelFiles`, `mModel`
is still non-null and `LoadModelData` still early-exits. `UnloadModelFiles`
is useless for hot-reload.

### `mModelData` — `HashGuid → ModelData` (the render state)

Large cache. **Node size: `0xD0` bytes** (`_aligned_malloc(0xd0, 0x10)`
in `DoInsertValue`).

Node layout (confirmed from `DoInsertValue` and `AddModelData`):

| Node offset | Field                                            |
| ----------- | ------------------------------------------------ |
| `+0x00`     | `mId` (uint key)                                 |
| `+0x04`     | padding                                          |
| `+0x08`     | start of `ModelData` (0xB0 bytes)                |
| `+0x10`     | `vector<GrannyMeshData>.mpBegin`                 |
| `+0x18`     | `vector<GrannyMeshData>.mpEnd`                   |
| `+0x20`     | `vector<GrannyMeshData>.mpCapacityEnd`           |
| `+0x48`     | nested `mTrackMasks` hashtable: bucket ptr       |
| `+0x50`     | nested `mTrackMasks` hashtable: bucket count     |
| `+0x54`     | nested hashtable field (zeroed on reset)         |
| `+0x68`     | nested hashtable rehash policy field             |
| `+0xB1`     | unknown flag (1 byte). If non-zero, `Exit` skips the entry in its cleanup loop. |
| `+0xC0`     | `mpNext`                                         |

Each `GrannyMeshData` inside the vector is **0x50 bytes (stride 80)**:

| GMD offset | Field                                           |
| ---------- | ----------------------------------------------- |
| `+0x00`    | `mMeshBinding` (Granny `MeshBinding*`)         |
| `+0x08`    | `mBoneIndices` (result of `GrannyGetMeshBindingToBoneIndices`) |
| `+0x10`    | `mBoneBindingCount` (int)                       |
| `+0x18`    | bone-binding OBB array (`_aligned_malloc`-ed; `16 * count` bytes of 16-byte pairs) |
| `+0x30`    | vertex buffer handle (written by `RequestBufferUpdate`) |
| `+0x34`    | index buffer handle (written by `RequestBufferUpdate`) |
| `+0x38`    | `mesh_name_hash` (StringIntern of mesh name)    |
| `+0x48`    | `mMeshFlag` (Granny3D / Shadow3D / Outline / ShadowMesh) |

(Offsets at `+0x20` and `+0x28` carry other per-mesh metadata whose
exact meaning we didn't need for the cleanup recipe.)

### `mAnimationFiles` — `HashGuid → granny_file*`

Separate cache for animation data. Not touched by the character reload
path directly, but `Exit` tears it down in Phase 1 using the same
pattern.

### `mStringDatabaseFiles` — flat vector of `granny_file*`

The SDB cache. Also torn down by `Exit` at the end.

---

## Key function addresses (Hades2.exe)

| Symbol                                          | Address      |
| ----------------------------------------------- | ------------ |
| `sgg::Granny3D::LoadAllModelAndAnimationData`   | `0x1401a9430` |
| `sgg::Granny3D::AddModelData`                   | `0x1401a9b60` |
| `sgg::Granny3D::LoadModelData`                  | `0x1401aaba7` (call site — containing function starts earlier) |
| `sgg::Granny3D::UnloadModelFiles`               | `0x1401ab3xx` (soft unload, NOT usable) |
| `sgg::Granny3D::Exit`                           | `0x1401aaf20` |
| `sgg::Granny3D::AddMask`                        | near `0x1401ac3bd` |
| `sgg::HashGuid::StringIntern`                   | (in H2M symbol map — confirmed present) |
| `GrannyFreeMeshBinding` (IAT)                   | `0x1403a9720` → `granny2_x64.dll` |
| `GrannyFreeTrackMask` (IAT)                     | `0x1403a97f8` → `granny2_x64.dll` |
| `GrannyFreeFile` (IAT)                          | called from `Exit` (address not noted) |

`mModelData`, `mModelFiles`, `mAnimationFiles`, `mStringDatabaseFiles`
are all global data in Hades2.exe's `.data` section. Their exact
addresses are in the H2M symbol map under `sgg::Granny3D::*`.

---

## Hash formula

`HashGuid.mId` → bucket index, vanilla EASTL integer hash:

```
h = (mId >> 16 ^ mId) * 0x7feb352d
h = (h   >> 15 ^ h)   * 0x846ca68b        // -0x7b935975 unsigned
bucket_idx = (h >> 16 ^ h) % bucket_count
```

Used identically for `mModelFiles`, `mModelData`, `mAnimationFiles`,
`mTrackMasks` (nested).

---

## LoadModelData flow

Reconstructed from the decompile at `0x1401aaba7`'s containing function:

```
void Granny3D::LoadModelData(HashGuid name):
    GrannyFile file = { nullptr, nullptr, nullptr, 0 };

    # 1. Cache check
    node = bucket_walk(mModelFiles, name.mId)
    if node != end_sentinel:
        file = node->value.second
        if file.mFileInfo != null || file.mModel != null:
            return                                  # <<< EARLY EXIT

    # 2. Open file from disk via fsAppendPathComponent hook
    OpenOptimizedGrannyFile(name, &file)

    if file.mFileInfo != null:
        # 3. Insert into mModelFiles
        result = mModelFiles.insert({name, file})
        if result.second == false:
            Log::Write("Failed to insert existing ModelFile for %s", name)
            # EXECUTION CONTINUES — log is decorative, not a rejection
        # 4. Build ModelData entry
        AddModelData(name, &file, false)
        return

    # 5. Fallback: copy Blank_Mesh's ModelData into a new entry
    #    (handles "GPK missing from disk" by substituting a placeholder)
    ...
```

Two caches write to per character:
1. `mModelFiles` gets the `GrannyFile` struct keyed by character hash
2. `mModelData` gets the `ModelData` render state keyed by character hash

Both must be evicted for a clean re-load.

---

## Granny3D::Exit — the cleanup recipe

`Exit` is the ONLY function in the binary that destroys `ModelData`
entries. Its body is structured as four phases; Phase 2 (the
`mModelData` tear-down) is the recipe we need for our per-entry
helper.

**Phase 2 inner loop body, applied to one located node:**

### A. Tear down nested `mTrackMasks` hashtable (at node `+0x48..+0x68`)

1. Walk the buckets (pointer at `+0x48`, count at `+0x50`):
   - For each chain node: call `GrannyFreeTrackMask(mask_node[+0x08])`, advance `mask_node = mask_node[+0x10]`
2. Walk the buckets a second time:
   - For each chain node: `_aligned_free(mask_node)`
   - Null the bucket slot
3. If bucket count > 1, `_aligned_free(bucket_array)`
4. Reset nested hashtable to empty state:
   - count = 1, bucket ptr = `eastl::gpEmptyBucketArray`
   - element count, rehash policy fields = 0

### B. Tear down `vector<GrannyMeshData>` (at node `+0x10..+0x20`)

**Two passes** over the range `[mpBegin, mpEnd)`, stride 0x50:

**Pass 1** — free each `MeshBinding`:
- Copy `[+0x18]` → `[+0x20]` (stack-like restore of capacity field)
- If `[+0x00]` (MeshBinding*) is non-null, call `GrannyFreeMeshBinding(*[+0x00])`
- Null `[+0x00]` and `[+0x08]`

**Pass 2** — free each mesh's owned bone-binding array:
- If `[+0x18]` is non-null, `_aligned_free([+0x18])`

Then `eastl::vector::shrink_to_fit` on the vector (at node `+0x10`) frees the backing allocation.

### C. Unlink the node and free it (NOT in Exit — we add this)

`Exit` skips this because it does bulk free via `DoFreeNodes` at the end.
For per-entry eviction:

1. Walk the bucket chain to find the `prev` node whose `mpNext` (`+0xC0`) points at our target
2. `prev->mpNext = target->mpNext` (or update bucket head if no prev)
3. `_aligned_free(target_node)`
4. `mModelData._24_8_--`   (decrement element count)

---

## The GPU buffer pool — RESOLVED

Initially assumed to be a leak. After decompiling
`ForgeRenderer::RequestBufferUpdate` (`0x1401f6670`) it is instead a
**bump allocator over pre-sized global static buffers**:

- `gStaticIndexBuffers` — one global shared index buffer for all 3D models
- `gStaticDrawBuffers[shader_effect.mGeometryResource]` — per-shader-effect vertex buffer pool
- `gStaticIndexBufferOffset` — monotonic write cursor into the shared index buffer
- Per-shader-effect vertex count at `gStaticDrawBuffers[...][+0x40]` — monotonic write cursor per shader effect

`RequestBufferUpdate` does four things:
1. Checks space availability (logs `"Ran out of memory"` and returns false on exhaustion)
2. Writes the **current offset** to `*out_index_handle` / `*out_vertex_handle`
3. Queues a `BufferResourceUpdateRequest` on the `gBufferResourceUpdateLock`-guarded vector for async upload
4. **Advances the monotonic offsets** — never decrements, never frees

The "handles" stored at `GrannyMeshData[+0x30]` / `[+0x34]` are
therefore just **integer offsets** into the global static buffers, not
allocated GPU resources. There is no symmetric release function because
there is nothing to release.

### Confirmed via `ForgeRenderer::*` symbol sweep

| Name | Exists? |
|---|---|
| `AddTexture2DResource`, `UpdateTextureResource`, **`RemoveTextureResource`** | ✓ |
| `AddRenderTarget`, **`RemoveRenderTarget`** | ✓ |
| `PrepareVertexData`, `RequestBufferUpdate` | ✓ |
| `RemoveVertexResource` / `RemoveBuffer` / `FreeMesh` / any symmetric mesh release | **✗** |
| `Unload` (`0x1401f1310`), `Exit` (`0x1401f1490`), `FlushGPUResources` (`0x1401f1f90`) | ✓ (bulk only) |

Textures and render targets have a full lifecycle. Mesh geometry does not.
Hades II's renderer was designed around "upload meshes once at startup;
never unload" — matching what we already knew about the model loading
pipeline.

### Implications for our cleanup recipe

**No buffer release calls are needed.** The per-entry cleanup recipe
from `Granny3D::Exit` Phase 2 is complete. The stale offsets in freed
`GrannyMeshData` entries are harmless because the `GrannyMeshData`
itself is removed from `mModelData`'s vector — the renderer never
dereferences stale offsets because the referring struct is gone.

Old mesh data remains resident in the static buffer at those offsets
as orphaned garbage, but is never read again. No VRAM leak, no crash.

### The real constraint — pool exhaustion

Every hot-reload monotonically advances the write cursors. Eventually
they hit the buffer caps and `RequestBufferUpdate` logs `"Ran out of
memory"` and returns false — the reload fails silently and the new
mesh simply doesn't appear.

Estimated reload budget (order of magnitude — needs empirical measurement):
- Pool is sized for every stock GR2 mesh that loads at startup
- Typical character uses ~20-50k vertices + ~50-100k indices
- If pool has ~10x headroom over startup usage, expect ~10-30 successful
  hot-reloads per character per session before the first exhaustion warning

### Mitigation strategy

**v3.8.0**: accept the finite pool, instrument the UI with reload
counters and show a soft warning after N successful reloads per
session. Hard failures (from the `"Ran out of memory"` log) surface as
red banners with "restart to reclaim" instructions.

**v3.8.1 or v3.9**: hook `sgg::ForgeRenderer::InitForgeRenderer`
(`0x1401f0930`) at startup and patch the buffer size constants before
the pool is allocated. Multiplying the static buffer sizes by 4-8x
gives effectively unlimited reloads for modder workflows. Requires one
more short Ghidra session to identify where the sizes are set.

---

## Concurrency concern

`mModelData` is read every frame by the render and animation systems:

- `sgg::DrawManager::Draw3DModel`, `Draw3DShadow`, `DoDraw3D`, `DoDrawShadow3D`, `DoDraw3DThumbnail`
- `sgg::ModelAnimation::CreateResources`, `PrepDraw`, `ComputeDrawBounds`
- `sgg::Granny3D::AddMask`
- `sgg::applyAdditiveLocomotionHelper`

Any eviction must run when no frame is in flight reading the target
entry. Two viable hook points:

1. **`rom.on_import.post("Main.lua")`** — fires between plugin init and
   first frame; classic CG3H uses this already for texture PKG loads.
2. **A scene-transition callback** — if H2M exposes one, we can defer
   hot-reloads until the next transition boundary.

Worst case we wrap the eviction in a mutex acquire matching whatever
`App::INSTANCE->mLogicFrameAllocator.mMutex` does (we saw
`EnterCriticalSection` calls on that mutex inside `AddModelData`).

---

## The shape of the H2M patch

New Lua binding, probably in `src/lua_extensions/bindings/hades/data.cpp`
or a new `granny_reload.cpp`:

```cpp
// rom.data.reload_character_model("Hecate")
static void reload_character_model(const std::string& name)
{
    // 1. Get hash via HashGuid::StringIntern (already in symbol map)
    uint32_t hash = StringIntern_fn(name.c_str(), 0);

    // 2. Evict the mModelData entry with full cleanup (C plus the
    //    Phase 2 recipe above). Accepts the GPU leak for v3.8.0.
    evict_mModelData_entry(hash);

    // 3. Null mFile/mFileInfo/mModel on the mModelFiles entry so
    //    LoadModelData does not early-exit.
    stub_mModelFiles_entry(hash);

    // 4. Call LoadModelData. It walks the full load path: file open
    //    via our fsAppendPathComponent redirect, parse, insert into
    //    mModelFiles, call AddModelData (which now inserts cleanly
    //    because mModelData has no existing entry).
    LoadModelData_fn({ hash });
}
```

Static symbol fetches at plugin init (mirroring how H2M already does it
for other hooks):

```cpp
static auto LoadModelData_fn =
    big::hades2_symbol_to_address["sgg::Granny3D::LoadModelData"]
        .as_func<void(sgg::HashGuid)>();
static auto StringIntern_fn =
    big::hades2_symbol_to_address["sgg::HashGuid::StringIntern"]
        .as_func<uint32_t(const char*, int)>();
static auto* mModelData_ptr =
    big::hades2_symbol_to_address["sgg::Granny3D::mModelData"].as<eastl_hashtable_header*>();
static auto* mModelFiles_ptr =
    big::hades2_symbol_to_address["sgg::Granny3D::mModelFiles"].as<eastl_hashtable_header*>();
```

The `GrannyFree*` functions are IAT entries pointing into
`granny2_x64.dll`. Call them via `GetProcAddress` on the loaded DLL
handle, or via the IAT thunks that already exist at `0x1403a9720` /
`0x1403a97f8`.

---

## Status

- Phase 1 RE complete. ✓ (Granny3D caches, LoadModelData, Exit recipe)
- Phase 2 RE complete. ✓ (ForgeRenderer — resolved as bump pool, no release needed)
- H2M patch not yet written. Ready to implement — no blockers.

---

## v3.8.0 Implementation Plan (chosen path)

After weighing two architectures (hot-reload only vs. dual-gate with
visibility flags), v3.8.0 ships **hot-reload only** for both modder and
player paths. The dual-gate / visibility-flag architecture is documented
below as the v3.8.1+ direction but is NOT in v3.8.0 scope.

### v3.8.0 scope

**One code path** for both enable/disable and rebuild:

```
toggle_or_rebuild(character):
    1. Update cg3h_mod_state.json
    2. Run cg3h_builder.exe --character {name}
       (rebuild merged GPK from currently-enabled mods)
    3. Call rom.data.add_granny_file(name + ".gpk", new_path)
       (refresh H2M file redirect)
    4. Call rom.data.reload_character_model(name)
       (NEW H2M binding — uses Phase 1/2 RE findings)
    5. Refresh UI
```

UX consequence: every player toggle takes ~1-2 seconds (rebuild
duration). Acceptable for v3.8.0; the visibility-gate fast path lands
in v3.8.1.

### v3.8.0 component breakdown

**1. H2M C++ patch — `src/lua_extensions/bindings/hades/data.cpp`**

New static helpers + new Lua binding:

```cpp
// File-scope state, fetched at plugin init via the symbol map
static void (*LoadModelData_fn)(sgg::HashGuid);
static uint32_t (*StringIntern_fn)(const char*, int);
static eastl_hashtable_header* mModelData_ptr;
static eastl_hashtable_header* mModelFiles_ptr;
static void (*GrannyFreeMeshBinding_fn)(void*);
static void (*GrannyFreeTrackMask_fn)(void*);

// Hash formula matching mModelData / mModelFiles bucket distribution
static uint32_t cg3h_hash(uint32_t mId) {
    uint32_t h = (mId >> 16 ^ mId) * 0x7feb352d;
    h = (h >> 15 ^ h) * 0x846ca68b;
    return h >> 16 ^ h;
}

// Walk a bucket to find a node by HashGuid.mId.  Returns (prev, target).
static std::pair<hash_node*, hash_node*> find_node_in_bucket(
    eastl_hashtable_header* ht, uint32_t mId);

// Run Granny3D::Exit Phase 2 inner block on one node.
// Frees nested mTrackMasks, vector<GrannyMeshData>, then unlinks + frees node.
static void evict_mModelData_entry(uint32_t hash);

// Walk mModelFiles, set the cached entry's mFile/mFileInfo/mModel to null.
// Bypasses LoadModelData's early-exit so it re-reads from disk.
static void stub_mModelFiles_entry(uint32_t hash);

// rom.data.reload_character_model("Hecate")
static void reload_character_model(const std::string& name) {
    uint32_t hash = StringIntern_fn(name.c_str(), 0);
    evict_mModelData_entry(hash);
    stub_mModelFiles_entry(hash);
    LoadModelData_fn({ hash });
}
```

Symbol fetches at H2M init (in the same place where existing Granny3D
hooks are wired up — search for `LoadAllModelAndAnimationData` in
`src/main.cpp` for the pattern):

| Symbol | Purpose |
|---|---|
| `sgg::Granny3D::mModelData` | Hashtable header, character → ModelData |
| `sgg::Granny3D::mModelFiles` | Hashtable header, character → GrannyFile |
| `sgg::Granny3D::LoadModelData` | Per-character load entry point |
| `sgg::HashGuid::StringIntern` | Already in symbol map |

`GrannyFreeMeshBinding` and `GrannyFreeTrackMask` are IAT thunks at
`0x1403a9720` and `0x1403a97f8` respectively. The simplest way to call
them is via `GetProcAddress(LoadLibraryA("granny2_x64.dll"), ...)` —
the DLL is already loaded by the game so `LoadLibraryA` just returns
the existing handle.

Bind alongside the existing `add_granny_file` / `add_package_file`:

```cpp
ns.set_function("reload_character_model", reload_character_model);
```

**Concurrency:** v3.8.0 uses the simplest safety mechanism — wrap the
whole `reload_character_model` body in
`EnterCriticalSection(&App::INSTANCE->mLogicFrameAllocator.mMutex)` /
`LeaveCriticalSection`. We saw `AddModelData` take this lock around its
own buffer-update calls, so it's the canonical "Granny3D state mutation"
mutex. The render thread will block briefly during eviction, which is
acceptable for a player-initiated action.

**2. CG3H runtime.lua update**

Replace the current `hot_reload_character` body with the H2M call:

```lua
function M.hot_reload_character(character, ctx)
    local gpk_file = character .. ".gpk"
    local gpk_path = rom.path.combine(ctx.builder_data_dir, gpk_file)

    if not rom.path.exists(gpk_path) then
        -- All mods disabled, rebuild deleted the merged GPK.
        -- Re-register the redirect at the stock path so subsequent loads
        -- find the original game file.
        local stock_path = compute_stock_gpk_path(character, ctx.game_dir)
        if stock_path then
            rom.data.add_granny_file(gpk_file, stock_path)
        end
        rom.data.reload_character_model(character)
        return "live"
    end

    rom.data.add_granny_file(gpk_file, gpk_path)
    rom.data.reload_character_model(character)
    return "live"
end
```

The `"transition"` outcome from the v3.8 sketch goes away — every
reload is now live, regardless of whether the rebuilt character is the
active hero.

**3. CG3H ui.lua — pool-pressure warning**

Add a session reload counter to `mod_state` and a yellow banner in
`ui.lua` after a threshold:

```lua
M.session_reload_count = 0   -- in mod_state.lua

-- in ui.lua after a successful reload:
if state.session_reload_count >= 10 then
    banner = { kind = "transition",
               text = "Hot-reload pool pressure: "
                      .. state.session_reload_count
                      .. " reloads this session. Restart the game to "
                      .. "reclaim buffer space if you hit \"Ran out of "
                      .. "memory\" errors." }
end
```

**4. Smoke test plan (in-game)**

Before committing, validate:

1. Launch with v3.8.0 H2M + CG3H. Confirm both Hecate and Melinoe load
   their modded versions.
2. Toggle HecateBiMod off. Confirm:
   - Builder logs `Hecate: no enabled mods, removed merged GPK`
   - `[CG3H] [hot-reload]` log shows `gpk exists=false` → stock path
     redirect → `reload_character_model` called
   - Hecate visibly reverts to stock **without restarting** and
     **without walking to a new area**
3. Toggle HecateBiMod back on. Confirm Hecate visibly returns to
   modded form.
4. Same for MelinoeGlasses (always-loaded character — the harder case).
5. Click Rebuild on a character with no toggle change. Confirm reload
   completes without visible artifacts.
6. Reload one character 15+ times in a row. Confirm:
   - First N succeed cleanly
   - Eventually `[ERR] Ran out of memory for 3D model geometry buffers!`
     appears in the H2M console
   - Pool-pressure banner is visible from reload 10 onward
   - Game does not crash on the first failed reload
7. Restart and confirm pool resets.

If any step crashes the game, pause and decompile the crash address —
the most likely culprits are: incorrect node layout offsets in the
evict helper, race with the render thread (mutex acquired at the wrong
scope), or `GrannyFreeMeshBinding` IAT call signature mismatch.

### v3.8.0 risk register

| Risk | Mitigation |
|---|---|
| Node offsets in evict helper are wrong → memory corruption / crash | Triple-check against the doc's "node layout" tables; smoke test before commit |
| `GrannyFreeMeshBinding` calling convention mismatch (we assume `__cdecl` / 1 arg) | Cross-check IAT thunk signature in Ghidra before writing the call |
| Mutex scope wrong → race with render thread → use-after-free on freed `GrannyMeshData` | Hold the lock for the entire `reload_character_model` body, not just the evict step |
| `LoadModelData` returns successfully but new mesh doesn't appear | Most likely cause: file redirect not picked up. Confirm `add_granny_file` was called BEFORE `reload_character_model` in `runtime.lua` ordering |
| Stock-restore path (`gpk_path` doesn't exist) breaks `reload_character_model` because `mModelFiles` stub points at nothing parseable | `add_granny_file` to the stock game GPK first, THEN reload. The redirect makes `LoadModelData` re-read from `Content/GR2/_Optimized/{Char}.gpk` |
| Pool exhaustion during testing iteration | Save before testing; restart when "Ran out of memory" appears |

---

## v3.8.1+ — Visibility-gate fast path (planned, not in v3.8.0)

The v3.8.0 implementation gives modders fast iteration but makes
players wait ~1-2 seconds per toggle for the rebuild. v3.8.1 adds a
**visibility gate** that skips the rebuild for player-initiated
enable/disable while keeping the rebuild path for modder iteration.

### Architecture: two independent gates

**Gate 1 — "merged" state.** Determined at build time. Which mods
contribute geometry to the merged GPK. Changed via rebuild + hot-reload.
**Modder workflow.** Same code as v3.8.0.

**Gate 2 — "visible" state.** Determined at runtime per-frame. Which
of the meshes currently in `mModelData[character]`'s vector actually
draw. Changed via a single `uint` write per affected mesh.
**Player workflow.**

These are independent — a mod can be merged-in but not-visible
(disabled by player), or not-merged and obviously not-visible. The
"not-merged and visible" combination is invalid by construction.

### The vertex-count-zeroing trick

Each `GrannyMeshData` has a vertex count field at `+0x44` (written by
`AddModelData` as `*(uint *)(uVar9 - 0x14) = uVar29;` where `uVar9` is
the end pointer). If we set this to **0**, the renderer's draw call
processes the entry but draws nothing. Restore the original count to
make it visible again.

**Why this is ideal:**
- Atomic uint write — no concurrency hazard
- No vector mutation — `mModelData` shape unchanged
- No GPU work — buffer data already uploaded, just not referenced
- No render-path hook — renderer naturally skips zero-vertex draws
- Reversible without rebuild — toggling on restores the count

**One-day smoke test required first** to verify that vertex count = 0
actually suppresses the draw cleanly without tripping a renderer
assertion or causing driver-level weirdness. If it doesn't, fall back
to one of these alternatives in priority order:

1. Set `MeshBinding*` (`GrannyMeshData[+0x00]`) to null — most rendering
   paths null-check this
2. Set `mMeshFlag` (`GrannyMeshData[+0x48]`) to a value the draw filters
   skip (the existing values are 0 = `Granny3D`, 1 = `Outline`, 2 = `ShadowMesh`;
   we'd need to find a value the renderer treats as "skip" or pick an unused enum)
3. Splice the `GrannyMeshData` out of the vector entirely (back-fill from the end,
   decrement `mpEnd`) — needs a mutex with the render thread
4. Hook one or more of the `DrawManager::Draw3D*` functions and filter their
   render lists — the surface area we wanted to avoid

### v3.8.1 component breakdown

**1. Build pipeline change — `tools/cg3h_build.py` and `cg3h_builder_entry.py`**

The merged GPK build now includes **all installed mods**, regardless of
their `enabled` state in `cg3h_mod_state.json`. The `enabled` flag no
longer gates inclusion at build time — it gates visibility at runtime.

Alongside the merged `{character}.gpk`, write a `{character}.meshmap.json`
that records which mod each mesh in the merged result came from:

```json
{
  "version": 1,
  "character": "Hecate",
  "meshes": {
    "TorusBattleMesh": {
      "mod_id": "Enderclem-HecateBiMod",
      "entry_index": 0
    },
    "TorusHubMesh": {
      "mod_id": "Enderclem-HecateBiMod",
      "entry_index": 1
    },
    "Hecate_Body": {
      "mod_id": null,    // stock game mesh
      "entry_index": 0
    }
  }
}
```

The `entry_index` is needed because Granny meshes share names across
multi-entry characters (e.g. Hecate has `HecateBattle_Mesh` and
`HecateHub_Mesh` entries).

Cache key includes `enabled` state so toggle still triggers a rebuild
when needed (e.g. if a player enables a mod that was never merged, we
need to rebuild). Wait — with the visibility gate, that might not be
true. Decision needed: **does enabling a previously-disabled mod always
need a rebuild, or only when it's a brand-new install?**

If always-merged-when-installed → no rebuild on enable. Player toggle
is purely visibility. Fast.
If merged-on-first-enable → rebuild on first enable, then visibility
toggle for subsequent. Slightly faster startup but slower first toggle.

**Decision (subject to change in v3.8.1 design):** always-merged.
Simpler model, predictable behavior, and the pool pressure cost of
"unused mods in the GPK" is bounded by the number of installed mods —
much smaller than the per-session reload count anyway.

**2. CG3H runtime — load-time meshmap registration**

`mod_state.lua` gains:

```lua
M.mesh_map = {}
-- M.mesh_map[character] = {
--   { mesh_name, mod_id, original_vertex_count, gmd_address }
-- }
```

After the first `LoadModelData` call for each character (during the
runtime pipeline), CG3H reads the `meshmap.json` and walks the in-memory
`GrannyMeshData` vector for that character. For each mesh:
- Match against the meshmap by name + index
- Record `(mod_id, original_vertex_count)` plus the mesh's address

This requires a new H2M helper to expose the GrannyMeshData vector
contents to Lua — probably `rom.data.get_character_meshes(name)` returning
a list of `{name, vertex_count, address}` tuples.

**3. CG3H runtime — visibility apply**

After load and after every `mod_state` change, walk `M.mesh_map` and
for each mesh: if `is_enabled(mod_id)`, restore `original_vertex_count`;
else write `0`. New H2M helper:
`rom.data.set_mesh_vertex_count(address, count)`.

**4. UI split**

`ui.lua` Characters tab grows two controls per mod:

- **Visibility checkbox** (the existing checkbox, now repurposed) →
  fires `set_visible(mod_id, enabled)` → updates mod_state, walks
  mesh_map, writes vertex counts → no rebuild
- **Rebuild button** (per-character header, existing) → unchanged from
  v3.8.0; runs the full rebuild + hot-reload pipeline

**5. UX text update**

Banner messages need to distinguish "visibility flipped" (instant) from
"rebuild + reload" (1-2s). The visibility path produces no banner since
nothing meaningful happened from the player's perspective.

### Backward compatibility

If a character has no `meshmap.json` (e.g. CG3H runs against a v3.8.0
build cache), the visibility path falls back to the v3.8.0 rebuild +
reload flow for that character. Mixed-version safe.

### Tasks for v3.8.1

1. Smoke-test vertex-count-zeroing in a throwaway H2M plugin
   (ten lines of Lua + the new helper). If it doesn't work, switch to
   one of the fallback gates.
2. Build pipeline: always-merge + meshmap.json output
3. H2M: `get_character_meshes` and `set_mesh_vertex_count` Lua helpers
4. CG3H runtime: meshmap registration + visibility apply
5. UI split: visibility checkbox + rebuild button as separate controls
6. CHANGELOG entry, smoke test, ship

### Why v3.8.1 and not v3.9

The visibility gate is a player-UX optimization on top of the v3.8.0
hot-reload foundation. It doesn't change the modder workflow, doesn't
add new features, and is gated on a single design decision (does the
vertex-count trick work). Treating it as a patch lane keeps cadence
high — if the smoke test passes, it ships within a few days of v3.8.0.

v3.9 (outfit switching) is a separate feature. The visibility gate is
the foundation, but outfit switching adds the concept of mutually
exclusive variant groups, a per-character outfit picker UI, and
eventually the PonyMenu integration from `project_melskin_reference.md`.

---

## Future deferred — buffer pool size patch

`InitForgeRenderer` (`0x1401f0930`) is called at startup and allocates
the global static buffers from constants we haven't yet identified.
Hooking that function and patching the size constants (or the
allocation calls themselves) would expand the pool to give effectively
unlimited reloads.

Targeted for v3.8.2 or v3.9, depending on whether v3.8.0/v3.8.1 testing
shows the default pool is acceptable for typical modder workflows. One
more short Ghidra session needed to find the constants.

The pool expansion hook was implemented and tested (`addStaticVertexBuffers`
at DLL init time, 4x multiplier: 56MB→224MB for Default3D, 14MB→56MB
for Shadow3D). See `docs/h2m_hot_reload_patch_experimental.diff`.

---

## Investigation outcome — LoadModelData NOT viable mid-session

After extensive testing (H2M patch + CG3H integration + in-game
iterations over ~6 hours on 2026-04-12), calling
`sgg::Granny3D::LoadModelData` mid-session was confirmed to be
**fundamentally unsafe**.

### What works end-to-end
- Per-entry file extraction (`.gr2.lz4` standalone files) ✓
- H2M file redirect for per-entry opens ✓
- `OpenOptimizedGrannyFile` finds the entry via redirect ✓
- Granny parses the file (SDB remap fail harmless, type tag
  auto-convert works) ✓
- `AddModelData` runs and writes data to mModelData ✓
- Pool expansion (4x vertex buffers via `addStaticVertexBuffers`
  hook) ✓
- Single-entry reload survives multiple rebuilds without crash ✓

### What crashes
- `App::UpdateAndDraw` crashes 1-2 frames after `LoadModelData`
  returns — even when the character isn't being rendered on screen
- The crash is NOT a render-thread race (character off-camera) and
  NOT pool exhaustion (4x expanded). It's from `AddModelData`
  mutating internal state (frame allocator watermarks, MeshBinding
  chains, GrannyMeshData vector operations) that `UpdateAndDraw`
  later reads in an inconsistent state.
- Multiple entries per frame always crashes (second entry's
  vector-zero races with the first entry's deferred side effects)
- Single entry per frame sometimes survives but scene transitions
  after a reload crash (cascading reload from mModelFiles stubs)

### Root cause
`LoadModelData` / `AddModelData` were designed for single-pass
startup initialization. Their internal operations make assumptions
about ordering and exclusivity that mid-session calls violate. There
is no mutex or synchronization point that safely serializes a reload
with the game's `UpdateAndDraw` loop.

### Correct approach for future hot-reload (not yet implemented)
**Direct GPU buffer update** — bypass `LoadModelData`/`AddModelData`:
1. Parse the new GR2 data using Granny DLL calls
   (`GrannyReadEntireFileFromMemory`)
2. Extract vertex/index arrays from the parsed file
3. Write directly to the persistently-mapped GPU static buffer at the
   EXISTING offsets (overwrite in-place via `memcpy` to the mapped
   CPU address — the buffer was created with
   `RESOURCE_MEMORY_USAGE_CPU_TO_GPU` +
   `BUFFER_CREATION_FLAG_PERSISTENT_MAP_BIT`)
4. Update MeshBinding pointers only if the skeleton changed
5. Never touch the GrannyMeshData vector, never call LoadModelData,
   never call AddModelData

This avoids all the initialization-order assumptions. The renderer
sees smooth data updates at existing offsets. No vector reallocation,
no frame allocator interaction, no mModelFiles mutation.

### Preserved artifacts
- `docs/h2m_hot_reload_patch_experimental.diff` — full 635-line H2M
  C++ patch (data.cpp + main.cpp) including reload_character_model
  binding, pool expansion hook, diagnostic dump helpers, all offsets
- H2M branch `cg3h/v3.8-hot-reload` on `Enderclem0/Hell2Modding` —
  working tree with all changes
- This doc — complete RE findings for the cache architecture
- `docs/h2m_visibility_gate_experimental.diff` — 167-line visibility
  toggle patch (vertex count / index count / MeshBinding null
  approaches — all confirmed to have NO render effect because the
  draw pipeline caches draw state independently of mModelData fields)

## KEY FINDING: mModelData field writes have NO render effect

Tested three approaches to suppress mesh rendering by modifying
GrannyMeshData fields in mModelData:

1. Zero vertex count at `+0x3C` → no visual effect
2. Zero index count at `+0x38` → no visual effect
3. Null MeshBinding pointer at `+0x00` → no visual effect

**Conclusion:** the game's draw pipeline does NOT re-read these fields
from mModelData per frame.  It caches draw commands (vertex/index
buffer offsets, draw counts) at initialization time and replays them.
Modifying mModelData after initialization has zero visual effect.

**Correct approach:** hook the draw function itself
(`sgg::DrawManager::DoDraw3D` at `0x1401ea929`) and skip meshes whose
entry hash is in a "hidden" set.  This intercepts at the draw level
before commands are submitted, avoiding all data mutation.

## DoDraw3D draw-call hook — IMPLEMENTED (v3.8.0)

### Decompilation results

`sgg::DrawManager::DoDraw3D` lives at `0x1401e9a60` (function end
`0x1401eb1f2`, 6034 bytes). Address `0x1401ea929` is the `LEA rcx,
[mModelData]` instruction mid-function that references the global
hashtable.

**PDB signature (demangled):**
```cpp
static void sgg::DrawManager::DoDraw3D(
    const eastl::vector<sgg::RenderMesh*, eastl::allocator_forge>& meshes,
    unsigned int index,
    int param,
    sgg::HashGuid hash    // ← param4, in r9d
);
```

This is a **static** function. The 4th parameter `HashGuid` (a 4-byte
struct containing `unsigned int mId`) is the **mModelData entry hash**
— the same hash used for hashtable lookups throughout the model system.

**How we know param4 is the entry hash:**
1. Caller at `0x1401fe38c` sets `r9d = [draw_entry + 0x28]`
2. DoDraw3D prologue: `mov dword ptr [rax + 0x20], r9d` saves it to
   stack frame as `[rbp + 0x2e8]` (since `rbp = original_rsp - 0x2c8`)
3. At `0x1401ea91b`: `lea r8, [rbp + 0x2e8]` passes it as the lookup
   key to the mModelData hashtable search at `0x1401b0830`
4. The lookup returns the `ModelData` node whose GrannyMeshData vector
   is then iterated for drawing

**Three sibling functions share the EXACT same signature:**

| Symbol | Address | Size | Caller flag |
|---|---|---|---|
| `DoDraw3D` | `0x1401e9a60` | 6034 B | default (all flags 0) |
| `DoDrawShadow3D` | `0x1401e7950` | 3645 B | `[draw_entry + 0x2c]` |
| `DoDraw3DThumbnail` | `0x1401e8c00` | 3672 B | `[draw_entry + 0x2e]` |

All three save `r9d` at `[rax + 0x20]` in their prologue, confirming
the identical calling convention.

A 4th variant, `DoDrawShadowCast3D` at `0x1401e8790`, has a **different**
signature: `static void(RenderMesh*, uint, uint)` — no HashGuid param.
It handles shadow casting when `[draw_entry + 0x2d]` is set.

### DoDraw3D internal flow (summary)

1. Early profiling/TLS checks (series of `cmp [rip+offset], [rsi+rbx]`)
2. Compute draw-entry pointer: `rdi = base + edx * 320`
3. Read draw entry fields: `[rdi+0x5c]`, `[rdi+0x98]`, etc.
4. **mModelData lookup** at `0x1401ea929-0x1401ea930`:
   - `lea rcx, [rip+0x365f20]` → mModelData at `0x140550850`
   - `call 0x1401b0830` → hashtable search by entry hash
5. Load GrannyMeshData vector: `[result+0x10]` = mpBegin, `[result+0x18]` = mpEnd
6. Compute mesh count: `(end - begin) / 0x50`; skip if zero
7. **Per-mesh loop** at `0x1401eaa20`: stride 0x50
   - Switch on mesh type flag at `[mesh + 0x4c]` (byte):
     case 0 = normal 3D → full draw path
     case 1 = outline → set flag, skip to next
     case 2 = shadow mesh → separate path
     default = skip
   - Normal path writes vertex/index handles + mesh pointer into a
     draw command struct, then processes texture/UV/shader state

### Hook implementation

**Approach:** function-entry detour hook on all three DoDraw3D variants.
Check param4 (`HashGuid.mId`) against a global hidden-entries set.
If present, return without calling original → draw call suppressed.

```cpp
static void hook_DoDraw3D(void* vec_ref, unsigned int index, int param, sgg::HashGuid hash)
{
    {
        std::shared_lock l(cg3h_draw_gate::g_mutex);
        if (cg3h_draw_gate::g_hidden_entries.count(hash.mId))
            return;
    }
    big::g_hooking->get_original<hook_DoDraw3D>()(vec_ref, index, param, hash);
}
```

**Thread safety:** `shared_mutex` — render thread takes shared lock
(read), game thread takes exclusive lock (write on toggle). Shared
lock is ~1 interlocked CAS on the fast path, negligible at 60 FPS.

**Lua binding:** `rom.data.set_entry_visible("HecateBattle_Mesh", false)`
Uses `HashGuid::Lookup` (already in H2M symbol map) to resolve names.

**CG3H side:** toggle checkbox calls `runtime.toggle_mod_visibility`
which calls `rom.data.set_entry_visible` per mesh entry. Instant
visual toggle — no rebuild, no restart, no data mutation.

### Patch location

`Hell2Modding/src/lua_extensions/bindings/hades/data.cpp` on branch
`cg3h/v3.8-hot-reload`.
