# CG3H Mod Specification

This is the definitive reference for CG3H mod authors. It covers mod types, the `mod.json` format, conflict resolution, and the build pipeline.

---

## Mod Types

CG3H supports 5 mod types. The build system infers operations from assets, so a single mod can combine multiple types.

### texture_replace

Replace a character's texture without touching the mesh.

```json
{
  "type": "texture_replace",
  "character": "Melinoe",
  "assets": {
    "textures": ["MelinoeRetexture.png"]
  }
}
```

**Build**: Compresses PNG to BC7 DDS with mipmaps, builds a standalone `.pkg` with the entry named `GR2/<original_texture_name>`. H2M loads this .pkg at runtime; the game's texture resolver picks up the replacement by name (last-loaded wins).

**CC-free**: Only the custom PNG is shipped.

### mesh_add

Add new geometry attached to a character's skeleton. Original meshes are not distributed.

```json
{
  "type": "mesh_add",
  "character": "Melinoe",
  "assets": {
    "glb": "CrownAccessory.glb",
    "textures": ["Crown_Color.png"]
  }
}
```

**Build**:
1. Reads original `Melinoe.gpk` from the user's game install (NOT shipped)
2. Appends new meshes + materials from the GLB
3. Builds standalone `.pkg` for custom textures
4. Outputs GPK to the mod's H2M directory

**CC-free**: Only the GLB (custom meshes) and PNGs are shipped. `cg3h_builder.exe` (29MB) is included so end users can build the GPK on their machine without Python.

### mesh_replace

Replace a character's meshes entirely with new ones.

```json
{
  "type": "mesh_replace",
  "character": "Melinoe",
  "assets": {
    "glb": "CustomMelinoe.glb",
    "textures": ["CustomMel_Color.png"]
  }
}
```

**Build**: Loads the original GPK, replaces specified meshes, updates MeshBindings, builds standalone `.pkg` for textures.

**CC note**: Currently requires distributing modified geometry. A v3.1 diff format will enable CC-free distribution.

### mesh_patch

Modify existing mesh vertices (reshape, sculpt) without adding or removing meshes.

```json
{
  "type": "mesh_patch",
  "character": "Melinoe",
  "assets": {
    "glb": "MelinoeEdited.glb"
  }
}
```

**Build**: Loads the original GPK, matches GLB meshes to GR2 meshes by name, patches vertex positions/normals/UVs in-place.

**CC note**: Currently requires distributing the edited GLB. A v3.1 diff format will enable CC-free distribution.

### animation_patch

Replace or modify specific animation curves on a character.

```json
{
  "type": "animation_patch",
  "character": "Melinoe",
  "assets": {
    "glb": "Melinoe_edited.glb",
    "animations": {
      "patch": true,
      "filter": "NoWeapon_Base_Idle"
    }
  }
}
```

**`assets.animations` fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `patch` | bool | yes | Must be `true` to enable animation patching |
| `filter` | string | no | Substring filter selecting which animations to patch. If omitted, all animations in the GLB are patched. |

**Build**: Loads original GPK, matches GLB animations to GR2 animations (filtered if set), patches curve data, outputs modified GPK.

**CC-free**: Only the GLB (containing modified curves) is shipped.

### Combining types

A mod can perform multiple operations:

```json
{
  "type": ["mesh_patch", "animation_patch"],
  "character": "Melinoe",
  "assets": {
    "glb": "Melinoe_edited.glb",
    "animations": {
      "patch": true,
      "filter": "NoWeapon_Base_Idle"
    }
  }
}
```

The build system also infers operations from assets automatically via `_infer_operations`.

---

## mod.json Format

```json
{
  "name": "MyMod",
  "version": "1.0.0",
  "author": "YourName",
  "description": "Description of the mod",
  "type": "texture_replace",
  "character": "Melinoe",
  "assets": {
    "glb": "CustomMesh.glb",
    "textures": ["CustomTexture.png"],
    "animations": {
      "patch": true,
      "filter": "Idle"
    }
  }
}
```

**Required fields:**

| Field | Description |
|-------|-------------|
| `name` | Mod name (used for Thunderstore manifest and H2M folder naming) |
| `version` | Semver version string |
| `author` | Author name |
| `type` | One of the 5 types, or an array for combined types |
| `character` | Target character name (must match a `.gpk` in `_Optimized/`) |

**Optional fields:**

| Field | Description |
|-------|-------------|
| `description` | Human-readable description |
| `assets.glb` | Path to GLB file (relative to mod.json) |
| `assets.textures` | Array of PNG filenames |
| `assets.animations` | Animation patch config (`patch` + optional `filter`) |

---

## Thunderstore Package Structure

```
AuthorName-ModName/
  mod.json                  CG3H mod descriptor
  manifest.json             Thunderstore manifest (auto-generated)
  main.lua                  H2M Lua companion (auto-generated)
  *.glb                     Custom meshes (mesh_add only, CC-free)
  *.png                     Custom textures
  *.pkg                     Standalone texture package (pre-built)
  cg3h_builder.exe          Standalone builder (mesh mods only)
```

The Lua companion handles:
- Loading standalone `.pkg` via `rom.game.LoadPackages`
- Auto-building GPK on first launch (runs `cg3h_builder.exe` if needed)
- Deferred loading via `rom.on_import.post`

---

## Conflict Resolution

### Detection

When multiple mods target the same character, CG3H detects conflicts per-operation:

| Mod A | Mod B | Conflict? | Resolution |
|-------|-------|-----------|------------|
| texture_replace (same texture) | texture_replace (same texture) | Yes | Priority order; higher wins |
| mesh_add | mesh_add | No | Both appended |
| mesh_replace | mesh_replace (same meshes) | Yes | Mutually exclusive |
| mesh_replace | mesh_add | Maybe | May need manual adjustment |
| mesh_patch | texture_replace | No | Independent operations |
| mesh_patch | mesh_replace | Yes | Replace overrides patch |
| animation_patch (same filter) | animation_patch (same filter) | Yes | Mutually exclusive |
| animation_patch (different filter) | animation_patch (different filter) | No | Non-overlapping |
| animation_patch | mesh_replace | No | Independent (mesh vs anim) |
| animation_patch | texture_replace | No | Independent |

### Mod priority

`cg3h_mod_priority.json` controls merge order when multiple mods target the same character:
- Auto-generated, editable via the GUI Mods tab or by hand
- Higher index = applied later = wins conflicts
- The multi-mod merger (`mod_merger.py`) applies mods sequentially in priority order

### Multi-mod merging

When mods are compatible, `mod_merger.py`:
1. Scans all installed mods, groups by target character
2. Applies each mod sequentially to the previous output
3. Merges custom textures into a combined standalone `.pkg`
4. Outputs a single merged GPK per character

---

## Build Pipeline

### For mod creators

1. **Create**: Use the GUI Create tab (or CLI `gr2_to_gltf.py`) to export a character
2. **Edit**: Modify in Blender — meshes, textures, animations
3. **Describe**: Write `mod.json` (the GUI generates this automatically)
4. **Build**: `python tools/cg3h_build.py` (or GUI Build tab)
5. **Package**: `python tools/cg3h_build.py --package` for Thunderstore ZIP
6. **Upload**: Upload ZIP to Thunderstore

### For mod consumers

1. Install Hell2Modding via r2modman
2. Install CG3H mods from Thunderstore
3. Launch the game — H2M loads the mods automatically
4. For mesh mods: `cg3h_builder.exe` runs on first launch to build the GPK

### Build internals

```
mod.json + assets
    |
    +-- Read original GPK from game install (not shipped)
    |
    +-- Apply operations:
    |     texture_replace -> build standalone .pkg
    |     mesh_add        -> append to GPK + build .pkg
    |     mesh_replace    -> patch GPK + build .pkg
    |     mesh_patch      -> patch vertices in GPK
    |     animation_patch -> patch curves in GPK
    |
    +-- Generate main.lua (H2M Lua companion)
    +-- Generate manifest.json (Thunderstore)
    +-- Smart strip: remove unchanged assets from distribution
    |
    +-- Output H2M-compatible folder structure
```
