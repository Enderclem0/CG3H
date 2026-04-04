# v3.0 — CG3H + Hell2Modding Integration

## Revised Vision

Instead of building our own framework, **integrate with Hell2Modding (H2M)**. CG3H becomes the **build tool** that produces H2M-compatible mod packages. H2M handles DLL injection, file redirection, and Lua scripting.

---

## How Hell2Modding Works (Key Findings)

- **DLL proxy** (`d3d12.dll`): injected automatically when game starts
- **GPK redirection**: `plugins_data/AuthorName-ModName/Melinoe.gpk` → game loads this instead of original
- **PKG redirection**: same mechanism for `.pkg` + `.pkg_manifest` files
- **Lua mods**: `plugins/AuthorName-ModName/main.lua` with `manifest.json`
- **Lua API**: `rom.data`, `rom.gpk`, `rom.gui` (ImGui), `rom.game` (game globals)
- **Distribution**: Thunderstore mod manager
- **Buffer size hooks**: already extends game limits (8x) for larger models

### H2M Folder Structure
```
Ship/
  ReturnOfModding/
    plugins/                    ← Lua mods
      AuthorName-ModName/
        manifest.json
        main.lua
    plugins_data/               ← Asset mods (auto-redirected)
      AuthorName-ModName/
        Melinoe.gpk             ← replaces original
        AuthorName-ModName.pkg  ← custom textures
        AuthorName-ModName.pkg_manifest
```

---

## Goal 1: CG3H as H2M Mod Builder

### What Changes
CG3H's output format changes from "install by copying to game dir" to "build an H2M mod package."

### New Export Format: `mod.json` + H2M Structure

```
MyMod/
  mod.json                     ← CG3H mod descriptor
  manifest.json                ← H2M/Thunderstore manifest
  plugins_data/
    AuthorName-MyMod/
      Melinoe.gpk              ← built by CG3H
      AuthorName-MyMod.pkg     ← custom textures (if any)
      AuthorName-MyMod.pkg_manifest
  plugins/
    AuthorName-MyMod/
      manifest.json
      main.lua                 ← optional: outfit switcher, Lua hooks
```

### `mod.json` (CG3H-specific metadata)
```json
{
  "format": "cg3h-mod/1.0",
  "metadata": {
    "name": "Dark Melinoe Armor",
    "author": "ModderName",
    "version": "1.0.0",
    "description": "A dark armor set for Melinoe"
  },
  "target": {
    "character": "Melinoe",
    "mesh_entries": ["Melinoe_Mesh"]
  },
  "assets": {
    "glb": "DarkArmor.glb",
    "textures": ["DarkArmor_Color"]
  },
  "build": {
    "gpk": "Melinoe.gpk",
    "pkg": "ModderName-DarkArmor.pkg",
    "pkg_manifest": "ModderName-DarkArmor.pkg_manifest"
  }
}
```

### Implementation Tasks

**Task 1.1**: New CLI command `cg3h build` or `--build-h2m`
- Reads `mod.json` + manifest + GLB
- Runs the import pipeline (gltf_to_gr2)
- Produces H2M-compatible folder structure

**Task 1.2**: Custom texture .pkg builder
- Instead of modifying the game's .pkg, build a STANDALONE .pkg containing only the custom textures
- H2M's file redirection will load it alongside the game's packages
- Need to generate matching `.pkg_manifest`

**Task 1.3**: GUI "Build for H2M" button
- One-click: GLB + textures → H2M mod package
- Option to build Thunderstore ZIP for distribution

---

## Goal 2: Append Mode (Multiple Outfits)

### How It Works with H2M

H2M redirects by filename. If `Melinoe.gpk` is in `plugins_data/`, the game loads that instead. But only ONE mod can provide `Melinoe.gpk`.

### Solution: Merged GPK Builder

When multiple mods target the same character:
1. CG3H scans all installed mod.json files targeting "Melinoe"
2. Builds a MERGED `Melinoe.gpk` containing:
   - Original meshes
   - Mod A's meshes (appended)
   - Mod B's meshes (appended)
3. A companion Lua script controls which meshes are active via MeshBindings

### Implementation Tasks

**Task 2.1**: Mod scanner
- Scans `plugins_data/` and `plugins/` for CG3H `mod.json` files
- Groups mods by target character

**Task 2.2**: GPK merger
- Loads original GPK + all mod GPKs for a character
- Appends all mod meshes to a single merged GPK
- Assigns unique mesh names per mod

**Task 2.3**: MeshBindings Lua controller
- Generates `main.lua` that hooks into model loading
- Uses `rom.game` to access MeshBindings
- Provides in-game UI (via `rom.ImGui`) to switch outfits

---

## Goal 3: Standalone .pkg Builder

### Current Problem
Custom textures are added to the game's Fx.pkg by modifying it. This is fragile and conflicts with other mods.

### Solution
Build a standalone .pkg file containing ONLY the mod's custom textures. H2M's package redirection loads it alongside the game's packages.

### Implementation Tasks

**Task 3.1**: `build_standalone_pkg(textures, output_path)`
- Creates a new .pkg from scratch: header + chunks + texture entries
- Follows the game's format: big-endian header, LZ4 chunks, 0xAD entries
- Each entry: CSString name + BE size + XNB + tex header + pixels

**Task 3.2**: `.pkg_manifest` generator
- H2M expects a `.pkg_manifest` alongside each `.pkg`
- Generate the manifest with correct checksums (XXH64)

**Task 3.3**: Update custom texture pipeline
- Instead of `add_texture_entry` to game's pkg → `build_standalone_pkg`
- The `FromFileName` hash still works: game loads the standalone .pkg via H2M, registers the texture, hash lookup finds it

---

## Goal 4: Lua Integration

### Auto-generated Lua Mod

CG3H generates a companion `main.lua` for each mod:

```lua
-- Auto-generated by CG3H v3.0
-- Mod: Dark Melinoe Armor by ModderName

local mod = {
    name = "Dark Melinoe Armor",
    character = "Melinoe",
    meshes = {
        body = "DarkArmor_MeshShape",
        outline = "DarkArmorOutline_MeshShape",
        shadow = "DarkArmorShadowMesh_MeshShape",
    },
    textures = {
        "DarkArmor_Color",
    },
}

-- Register with CG3H framework (if installed)
if CG3H then
    CG3H.RegisterMod(mod)
end

-- Hook: log when mod is loaded
rom.log.info("[CG3H] Loaded: " .. mod.name)
```

### CG3H Framework Lua Plugin

A separate H2M plugin (`CG3H-Framework`) that:
- Scans for registered CG3H mods
- Provides `rom.ImGui` UI for outfit selection
- Manages MeshBindings switching at runtime
- Exposes API: `CG3H.RegisterMod()`, `CG3H.SetActiveOutfit()`, `CG3H.ListMods()`

---

## Goal 5: Thunderstore Distribution

### Package Format
Thunderstore expects a ZIP with:
```
manifest.json          ← Thunderstore manifest
icon.png               ← 256x256 preview
README.md
plugins/
  AuthorName-ModName/
    manifest.json      ← H2M manifest
    main.lua
plugins_data/
  AuthorName-ModName/
    Character.gpk
    AuthorName-ModName.pkg
    AuthorName-ModName.pkg_manifest
```

### CG3H "Package" Command
- `cg3h package --author ModderName --name DarkArmor`
- Reads mod.json, builds GPK/PKG, generates manifests, creates ZIP
- Ready to upload to Thunderstore

---

## Implementation Priority

| Phase | Feature | Complexity | Dependency |
|-------|---------|-----------|------------|
| 3.0-alpha | mod.json spec + H2M folder builder | Low | None |
| 3.0-alpha | Standalone .pkg builder | Medium | None |
| 3.0-alpha | GUI "Build for H2M" | Low | Builder |
| 3.0-beta | Mod scanner + GPK merger | Medium | mod.json |
| 3.0-beta | Thunderstore ZIP packager | Low | Builder |
| 3.0-rc | Outfit switcher Lua plugin | Medium | Merger |
| 3.0-rc | CG3H Framework Lua API | Medium | Plugin |
| 3.1 | In-game ImGui mod manager | High | Framework |
| 3.1 | Runtime MeshBindings switching | High | Framework |

## What We DON'T Need Anymore

Thanks to H2M:
- ❌ Our own DLL injection
- ❌ Our own file redirection hooks
- ❌ Modifying game .pkg files directly (standalone .pkg instead)
- ❌ Checksum management (H2M bypasses checksums.txt via PlatformOpenFile hooks)
- ❌ Our own backup/restore system (H2M mods are non-destructive)

## What We Keep

- ✅ GR2 ↔ glTF conversion (gr2_to_gltf.py, gltf_to_gr2.py)
- ✅ Granny DLL type system (granny_types.py)
- ✅ Material chain creation
- ✅ Animation export/import
- ✅ Texture extraction/compression (etcpak)
- ✅ GUI for exporting and building mods
