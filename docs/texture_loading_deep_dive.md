# Texture Loading Deep Dive — Decompilation & Testing Findings

All facts confirmed through Ghidra decompilation of Hades2.exe + granny2_x64.dll
and in-game testing with instrumented Lua hooks.

---

## Game Loading Timeline (confirmed from logs)

```
T+0.0s   H2M DLL init
         - Discovers PKGs/GPKs in plugins_data/ ("Adding to package files")
         - Registers fsAppendPathComponent hook (file path redirect)
         - Hooks LoadAllModelAndAnimationData (call-once guard)
         - Hooks LoadPackage (override system)

T+1.0s   IsContentFolderModified
         - Reads checksums.txt, computes XXH64 of each PKG
         - On mismatch: sets arePackagesModified=true, logs error
         - Does NOT block loading (unless strictPackageChecks enabled)
         - Both 1080p/ and 720p/ directories checked independently

T+2.0s   Loading package: Launch.pkg
         - LoadPackage → LoadManifest → ReadPackageFile → ReadTexture2D
         - BEFORE Lua plugins load

T+3.0s   Lua plugins load (first pass)
         - rom.data.get_hash_guid_from_string returns 0 (hash system not ready)
         - rom.on_import.post callbacks registered
         - rom.game.LoadPackages calls game's Lua LoadPackages
           → calls sgg::GameAssetManager::LoadPackage (C++)
           → calls ReadPackageFile → BUT ReadTexture2D is NOT called (0Mb logged)
           → CONFIRMED: LoadPackages from Lua does NOT process texture data

T+3.01s  Main.lua imported → on_import.post callbacks fire

T+5.0s   Loading package: GUI.pkg
         - Full C++ pipeline: LoadPackage → ReadPackageFile → ReadTexture2D
T+5.1s   Loading package: GUI_Mouse.pkg
T+5.2s   Loading package: Fx.pkg
         - ReadPackageFile processes all chunks (198 textures in original Fx.pkg)
         - DURING Fx loading: GPK entries resolve via AddModelData
         - AddModelData walks: mesh→MaterialBindings[0]→Material→Maps[0]→Material→Texture→FromFileName
         - fsGetPathFileName(FromFileName) → strips path AND extension → e.g. "ConeTexture"
         - Builds "GR2/" + filename → HashGuid::StringIntern → stores hash as texture reference
         - String database remap fails for modded entries (harmless for inline strings)
T+5.5s   Loading package: ScriptsBase.pkg
T+5.6s   Loading package: Melinoe.pkg (loads ONCE on first pass)
T+6.0s   Loading package: MainMenu.pkg

T+8.0s   Scene transition → second Lua load
         - get_hash_guid_from_string NOW works (returns valid hashes)
         - load_package_overrides_set registers correctly
         - LoadAllModelAndAnimationData blocked (H2M call-once guard)
         - For already-loaded models: "Failed to insert existing ModelFile" → REJECTED
         - AddModelData does NOT re-run for existing models

T+8.5s   Biome packages load (BiomeHub, BiomeF, etc.)
         - load_package_overrides_set fires → standalone PKGs loaded via REAL LoadPackage
         - ReadPackageFile → ReadTexture2D processes the standalone PKG textures
         - Characters that load HERE (Moros, Hecate) get fresh AddModelData calls
```

---

## Decompiled Functions (Hades2.exe)

### sgg::IsContentFolderModified (Program.cpp:2649)
- **Address:** `0x1402ffa50`
- Reads `checksums.txt`, computes XXH64 of each listed file
- Compares against stored hash (big-endian in checksums.txt)
- On mismatch: logs `"mismatched file %s"`, sets `bVar3 = true`, breaks
- Caller (`InitWindow`): sets `arePackagesModified = true`
- If `strictPackageChecks`: calls `ExitProcess` (terminate)
- Otherwise: continues normally, does NOT block file loading

### sgg::GameAssetManager::LoadPackage (GameAssetManager.cpp)
- **Address:** `0x1401a2510`
- Dedup check: if package hash already in `mLoadedPackages` with status `'\x02'` → SKIP entirely
- Calls `LoadManifest(this, hash, group)` for manifest-based textures (2D sprites)
- Calls `ReadPackageFile(this, path, ...)` for actual PKG data reading
- Logs `"Loading package: %s"` and `"Package Loaded: %s %dMb in %0.2fs"`
- The `%dMb` comes from `ReadPackageFile` return value (byte counter)

### sgg::GameAssetManager::LoadManifest (GameAssetManager.cpp:581)
- Reads `.pkg_manifest` file (same chunk format as PKG, big-endian int32s)
- Only handles 0xDE (atlas), 0xEE (bink), 0xCC (include), 0xBE/0xFF (terminators)
- **GR2 model textures (GR2\...) are NOT in manifest files**
- Manifest only contains 2D atlas/sprite entries
- Confirmed: `"GR2"`, `"Melinoe"`, `"Moros"` NOT FOUND in Fx.pkg_manifest

### sgg::GameAssetManager::ReadPackageFile (GameAssetManager.cpp)
- **Address:** `0x1401a1820` (approximate)
- Reads PKG header (version, compression flag)
- For each LZ4-compressed chunk: decompresses, calls `ReadNextAsset` in a loop
- `ReadNextAsset` dispatches by tag: 0xAD→ReadTexture2D, 0xAA→ReadTexture3D, 0xDE→ReadAtlas, etc.

### sgg::GameAssetManager::ReadNextAsset (GameAssetManager.cpp)
- **Address:** `0x1401a78a0` (approximate)
- Reads 1-byte tag, dispatches:
  - `0xAD` → `ReadTexture2D(this, stream, vec, byte_counter)`
  - `0xAA` → `ReadTexture3D(this, stream, vec, byte_counter)`
  - `0xDE` → `ReadAtlas(this, stream, vec, deferred)`
  - `0xBB` → Bink video
  - `0xCC` → `ReadIncludePackage`
  - `0xBE` → EndOfChunk
  - `0xFF` → EndOfFile

### sgg::GameAssetManager::ReadTexture2D (GameAssetManager.cpp)
- **Address:** `0x1401a3f50`
- Calls `Read2DTextureWithName(this, &name, stream, &texture_ptr)`
- Computes `StringIntern(name)` hash
- Three paths:
  - **Path A — NOT in mDeferredTextures, NOT in mTextureHandles:** Creates new TextureHandle via TextureHandleManager, registers in mTextureHandles. (First-time load)
  - **Path B — NOT in mDeferredTextures, IS in mTextureHandles:** `goto skip` — does NOTHING. (Duplicate)
  - **Path C — IS in mDeferredTextures:** Fills deferred entry with texture data pointer.
- Byte counter `*param_3` only incremented when name is non-empty AND texture_ptr is non-null

### sgg::GameAssetManager::Read2DTextureWithName (GameAssetManager.cpp)
- **Address:** `0x1401a3530` (approximate)
- Reads CSString name from stream → normalizes `\` to `/`
- Hashes with `StringIntern`
- Checks `mLoadedTexture2DHash` cache:
  - If found: seeks past texture data, returns cached pointer + name
  - If not found: reads XNB header, calls `ForgeRenderer::AddTexture2DResourceFromStream`
  - Validates XNB: must start with `"XNB"`, version byte `'w'` (0x77) accepted
  - After reading: verifies seek position matches expected end
  - Registers in `mLoadedTexture2DHash` cache
  - Returns name + texture pointer

### sgg::Granny3D::AddModelData (Granny3D.cpp)
- **Address:** `0x1401a9b60`
- Called from `LoadAllModelAndAnimationData` (and `LoadModelData`)
- Parameters: `(HashGuid entry_name, GrannyFile* file, bool param3)`
- Iterates `file_info->Meshes` (NOT Model->MeshBindings)
- For each mesh:
  - Creates `GrannyNewMeshBinding`
  - Reads `mesh->MaterialBindings->Material->Maps`
  - If Maps is non-null AND Maps->Material->Texture is non-null:
    - `fsGetPathFileName(texture->FromFileName, buffer)`
    - Builds `"GR2/" + buffer`
    - `HashGuid::StringIntern(result)` → stores hash as mesh texture reference
  - Computes vertex bounding box from vertex positions (stride 40, offset +0)
  - `HashGuid::StringIntern(mesh->Name)` → stores mesh name hash

### sgg::Granny3D::OptimizedBufferToGrannyFile (Granny3D.cpp)
- Decompresses LZ4 GPK entry
- Validates CRC: `GrannyFileCRCIsValidFromMemory`
- Loads via `GrannyReadEntireFileFromMemory`
- Looks up SDB by hash in `mStringDatabase` hashtable
- If SDB not found: opens `.sdb` file from disk, loads, registers in `mStringDatabase`
- Calls `GrannyRemapFileStrings(granny_file, sdb_ptr)`
- On remap failure: logs `"String database remap failed for %s"` → **continues normally**
- Calls `GrannyGetFileInfo` → returns file_info pointer
- Returns success if both file and file_info are non-null

### fsGetPathFileName (The Forge, FileSystem.cpp)
- **Confirmed behavior:** Strips BOTH path AND extension
- Input: `"D:/mod/ConeTexture.png"` → Output: `"ConeTexture"`
- Uses `strrchr` for last `/` or `\`, scans backwards from end for `.`
- Extension stored in `local_438`, stripped from output

---

## Decompiled Functions (granny2_x64.dll)

### FUN_180049ac0 (granny_file_info.cpp)
- Called from `GrannyGetFileInfo`
- Checks runtime type tag at file offset `+0x24` against `0x80000039`
- If mismatch (tag is `0x0`): calls `FUN_18003cf40` (type converter)
- Type converter walks source type definition → copies/converts to runtime type definition
- **Result cached** at `param_1 + 0x30` — only converts once per file
- With correct tag (`0x80000039`): returns raw data directly, no conversion

---

## Confirmed Test Results

| Test | Result | Notes |
|------|--------|-------|
| Replace `Melinoe_Color512` with red via `LoadPackages` at plugin init | **WORKS** | Existing texture name, override via mLoadedTexture2DHash |
| Replace `Hecate_Color` via `load_package_overrides_set` (BiomeF/BiomeHub/BiomeIHouse) | **WORKS** | Existing texture, biome override triggers real LoadPackage |
| New texture `Image_0` on Moros Table via biome override | **WORKS** | Moros loads on second pass, fresh AddModelData |
| New texture `ConeTexture` on Melinoe Cone via any approach | **FAILS** | Material chain broken — Cone always shows Melinoe_Color512 |
| New texture `Chronos_Color` (existing name) on Melinoe Cone | **FAILS** | Even with existing texture, Cone shows body texture |
| Inject ConeTexture into Fx.pkg first chunk | **FAILS** | Texture available but material chain broken |
| Hook LoadPackage + dynamic_call to inject PKG before Fx.pkg | **FAILS** | Injection fires 0.05ms before Fx, texture registered, material still broken |
| `rom.game.LoadPackages` at plugin init | **0Mb logged** | ReadTexture2D NOT called by Lua LoadPackages |
| `rom.data.reload_game_data()` | No effect | Does not reload Granny model data |
| Type tag patch (0x80000039) | Prevents auto-conversion | Material chain BREAKS — Cone shows body texture |
| No type tag patch (0x0) | Auto-conversion runs | Moros works, Melinoe doesn't |

## Key Confirmed Facts

1. **LoadPackages (Lua) does NOT call ReadTexture2D** — confirmed by hook (0 ReadTexture2D calls during LoadPackages, "0Mb" logged)
2. **load_package_overrides_set triggers REAL LoadPackage** which calls ReadPackageFile → ReadTexture2D — confirmed by Moros working
3. **get_hash_guid_from_string returns 0 on first Lua load** — hash system not ready
4. **Melinoe.pkg loads ONCE** and is never reloaded. Second AddModelData call is REJECTED ("Failed to insert existing ModelFile")
5. **GR2 model textures are NOT in .pkg_manifest** — confirmed by binary search of Fx.pkg_manifest
6. **AddModelData iterates file_info->Meshes** (not Model->MeshBindings)
7. **fsGetPathFileName strips both path and extension** — confirmed by decompilation
8. **DLL verification shows correct material chain** for both Moros Table and Melinoe Cone after serialization and reload
9. **In-game, Melinoe Cone always resolves to body texture** regardless of what ConeTexture points to

## Remaining Unknown

**Why does AddModelData read the template material for Melinoe's Cone mesh but the custom material for Moros's Table mesh?**

Both meshes are created by `_create_new_mesh` with identical logic. Both override MaterialBindings at `+0x28` with a custom material pointer. Both serialize correctly (strings verified in binary). Both reload correctly through the DLL.

The ONLY known difference:
- **Moros:** Single GPK entry (`Moros_Mesh`). AddModelData runs on second pass only (first pass rejected by H2M hook). Auto-conversion runs (type tag 0x0).
- **Melinoe:** Three GPK entries (`Hat_Mesh`, `Melinoe_Mesh`, `MelinoeOverlook_Mesh`). AddModelData runs on FIRST pass (Melinoe never reloads). Auto-conversion runs (type tag 0x0).

Possible investigation paths:
1. Does multi-entry GPK handling affect how new meshes are serialized?
2. Does the auto-conversion (FUN_18003cf40) behave differently for the first-pass load vs second-pass?
3. Is there a race condition during Fx.pkg loading where AddModelData runs before the conversion completes?
4. Hook AddModelData and inspect the actual MaterialBindings pointer for Cone in the game's memory (not our DLL)
