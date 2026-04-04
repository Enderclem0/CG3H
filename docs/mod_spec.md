# CG3H Mod Specification v1.0

## Design Principles

1. **No copyrighted content**: Mods carry ONLY custom assets + patch instructions. Original game files are never redistributed.
2. **Non-destructive**: Original game files are never modified. Mods are applied at load time via Hell2Modding.
3. **Modular**: Each mod is a self-contained unit. Multiple mods can coexist.
4. **Conflict-aware**: Mods declare what they touch. Conflicts are detected and resolvable.
5. **Composable**: Mods can depend on, extend, or patch other mods.

---

## Mod Types

### Type 1: Texture Replacement
Replace a character's texture without touching the mesh.

```json
{
  "type": "texture_replace",
  "target": { "character": "Melinoe" },
  "textures": [
    { "replaces": "Melinoe_Color512", "file": "MelinoeRetexture.png" }
  ]
}
```

**At build time**: CG3H compresses the PNG to BC7 DDS, builds a standalone .pkg with the entry named `GR2\Melinoe_Color512`. H2M loads this .pkg, and the game finds the modded texture instead of the original (last-loaded wins).

**No CC content**: Only the custom PNG is shipped.

---

### Type 2: Additive Mesh (Attachment)
Add new geometry attached to a character's skeleton. Original meshes untouched.

```json
{
  "type": "mesh_add",
  "target": { "character": "Melinoe", "mesh_entry": "Melinoe_Mesh" },
  "meshes": [
    { "name": "CrownAccessory_MeshShape", "type": "body", "texture": "Crown_Color" },
    { "name": "CrownAccessoryOutline_MeshShape", "type": "outline" },
    { "name": "CrownAccessoryShadowMesh_MeshShape", "type": "shadow" }
  ],
  "textures": [
    { "name": "Crown_Color", "file": "Crown_Color.png", "custom": true }
  ]
}
```

**At build time**:
1. Load original `Melinoe.gpk` from game (NOT shipped with mod)
2. Append new meshes + materials to the GR2
3. Output `Melinoe.gpk` to the mod's `plugins_data/`
4. Build standalone .pkg for custom textures

**No CC content**: Only the GLB (custom meshes) and PNGs are shipped. The original GPK is read from the user's game install at build time.

---

### Type 3: Full Mesh Replacement
Replace a character's meshes entirely with new ones.

```json
{
  "type": "mesh_replace",
  "target": { "character": "Melinoe", "mesh_entry": "Melinoe_Mesh" },
  "replaces": ["Melinoe_MeshShape", "MelinoeOutline_MeshShape"],
  "meshes": [
    { "name": "CustomMelinoe_MeshShape", "type": "body", "texture": "CustomMel_Color" },
    { "name": "CustomMelinoeOutline_MeshShape", "type": "outline" },
    { "name": "CustomMelinoeShadowMesh_MeshShape", "type": "shadow" }
  ],
  "textures": [
    { "name": "CustomMel_Color", "file": "CustomMel_Color.png", "custom": true }
  ]
}
```

**At build time**:
1. Load original GPK from game
2. Patch: zero out or replace the specified original meshes
3. Append new meshes
4. Update MeshBindings to point at new meshes instead of originals

**No CC content**: Mod ships the GLB + PNG. Build step reads the original GPK locally.

---

### Type 4: Semi-Destructive (Vertex Edit)
Modify existing mesh vertices (reshape, sculpt) without changing topology or adding meshes.

```json
{
  "type": "mesh_patch",
  "target": { "character": "Melinoe", "mesh_entry": "Melinoe_Mesh" },
  "patches": [
    {
      "mesh": "Melinoe_MeshShape",
      "glb": "MelinoeEdited.glb",
      "allow_topology_change": false
    }
  ]
}
```

**At build time**:
1. Load original GPK from game
2. Match GLB meshes to GR2 meshes
3. Patch vertex positions/normals/UVs in-place
4. Output modified GPK

**No CC content**: Only the edited GLB (which contains modified vertex data, not original) is shipped. The original mesh structure comes from the user's install.

---

## Folder Structure (Thunderstore Package)

```
ModAuthor-ModName/
  manifest.json              ← Thunderstore manifest
  icon.png                   ← 256x256 preview
  README.md
  mod.json                   ← CG3H mod descriptor (source of truth)
  assets/
    CustomMesh.glb           ← Blender export (custom geometry only)
    CustomTexture.png        ← Custom texture
  build/                     ← Generated at install time (NOT shipped)
    plugins_data/
      ModAuthor-ModName/
        Melinoe.gpk          ← Built from original + mod assets
        ModAuthor-ModName.pkg
        ModAuthor-ModName.pkg_manifest
    plugins/
      ModAuthor-ModName/
        manifest.json        ← H2M manifest
        main.lua             ← Generated companion script
```

**Key**: The `build/` directory is created on the user's machine by CG3H. The Thunderstore package only ships `mod.json`, `assets/`, `manifest.json`, `icon.png`, and `README.md`.

---

## mod.json Full Specification

```json
{
  "format": "cg3h-mod/1.0",

  "metadata": {
    "name": "Dark Melinoe Armor",
    "author": "ModderName",
    "version": "1.0.0",
    "description": "A dark armor set replacing Melinoe's default outfit",
    "preview": "icon.png",
    "tags": ["armor", "melinoe", "cosmetic"]
  },

  "type": "mesh_replace",

  "target": {
    "character": "Melinoe",
    "mesh_entries": ["Melinoe_Mesh"],
    "game_version": ">=1.0.0"
  },

  "assets": {
    "glb": "assets/DarkArmor.glb",
    "textures": [
      {
        "name": "DarkArmor_Color",
        "file": "assets/DarkArmor_Color.png",
        "width": 512,
        "height": 512,
        "custom": true
      }
    ]
  },

  "meshes": [
    {
      "name": "DarkArmor_MeshShape",
      "type": "body",
      "texture": "DarkArmor_Color",
      "replaces": "Melinoe_MeshShape"
    },
    {
      "name": "DarkArmorOutline_MeshShape",
      "type": "outline",
      "replaces": "MelinoeOutline_MeshShape"
    },
    {
      "name": "DarkArmorShadowMesh_MeshShape",
      "type": "shadow"
    }
  ],

  "options": {
    "allow_topology_change": true,
    "keep_original_outline": false,
    "keep_original_shadow": false
  },

  "conflicts": {
    "incompatible": [],
    "overrides": ["OtherMod-MelinoeRetexture"]
  },

  "compatibility": {
    "patches": [
      {
        "mod": "SomeOtherMod-WeaponPack",
        "action": "preserve_meshes",
        "meshes": ["Weapon_MeshShape"]
      }
    ]
  },

  "lua": {
    "generate_companion": true,
    "hooks": {
      "on_load": "-- custom Lua to run when mod loads",
      "const_name": "Outfit_DarkArmor"
    }
  }
}
```

---

## Conflict Resolution

### Detection
When multiple mods target the same character:

| Mod A | Mod B | Conflict? | Resolution |
|-------|-------|-----------|------------|
| texture_replace (Melinoe_Color) | texture_replace (Melinoe_Color) | YES | Last installed wins, or user picks |
| mesh_add (Crown) | mesh_add (Cape) | NO | Both appended to same GPK |
| mesh_replace (body) | mesh_add (Crown) | MAYBE | Crown attaches to replacement body — may need compat patch |
| mesh_replace (body) | mesh_replace (body) | YES | Mutually exclusive, user picks one |
| mesh_patch (vertices) | texture_replace | NO | Independent operations |
| mesh_patch (vertices) | mesh_replace | YES | Replace overrides patch |

### Conflict Rules in mod.json

```json
"conflicts": {
  "incompatible": ["OtherAuthor-SameMelinoeMod"],
  "overrides": ["AuthorX-MelinoeTexture"],
  "compatible_with": ["AuthorY-MelinoeWeapons"]
}
```

### Compatibility Patches

A third mod can act as a compatibility bridge:

```json
{
  "type": "compatibility_patch",
  "patches": [
    {
      "when": ["ModA-DarkArmor", "ModB-CrownAccessory"],
      "action": "merge",
      "description": "Adjusts Crown bone weights for Dark Armor's modified skeleton"
    }
  ]
}
```

---

## Build Pipeline

### User Flow (Mod Creator)
1. Export character with CG3H → GLB + textures
2. Edit in Blender → modified GLB
3. Write `mod.json` (or GUI generates it)
4. `cg3h build` → reads mod.json, builds GPK/PKG locally
5. `cg3h package` → creates Thunderstore ZIP

### User Flow (Mod Consumer)
1. Install Hell2Modding (Thunderstore/r2modman)
2. Install CG3H Runtime plugin (one-time)
3. Install mods via Thunderstore
4. CG3H Runtime scans installed mods, builds GPK/PKGs from assets
5. Game launches with mods active

### Build Steps (Internal)

```
mod.json + assets/
    │
    ├── Read original GPK from game install
    │   (Content/GR2/_Optimized/Character.gpk)
    │
    ├── Apply mod type:
    │   ├── texture_replace → build standalone .pkg
    │   ├── mesh_add → append to original GPK
    │   ├── mesh_replace → patch original GPK + build .pkg
    │   └── mesh_patch → patch vertices in original GPK
    │
    ├── Generate companion main.lua
    │
    └── Output to build/plugins_data/ + build/plugins/
```

---

## CG3H Runtime Plugin

A lightweight H2M Lua plugin that:
1. Scans for installed CG3H mods (looks for `mod.json` in Thunderstore packages)
2. Triggers build if `build/` is missing or outdated
3. Copies built files to `plugins_data/` and `plugins/`
4. Provides ImGui UI: list installed CG3H mods, enable/disable, check conflicts

```
plugins/
  CG3H-Runtime/
    manifest.json
    main.lua              ← scanner + builder + UI
```

This would need to call CG3H's Python tools — either via `os.execute` or by porting critical path to Lua/C++.

---

## Migration from v2.x

| v2.x Feature | v3.0 Equivalent |
|---|---|
| Export to GLB + manifest | Same (mod.json replaces manifest.json) |
| Install by copying GPK | Build to H2M plugins_data/ |
| Modify game .pkg | Build standalone .pkg |
| Backup/restore | Not needed (non-destructive) |
| Checksum management | Not needed (H2M bypasses) |
| Mod registry | H2M + Thunderstore handle this |
