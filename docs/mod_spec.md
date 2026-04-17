# CG3H Mod Specification

This is the definitive reference for CG3H mod authors. It covers mod types, the `mod.json` format, conflict resolution, and the build pipeline.

---

## Mod Types

CG3H supports 5 mod types. The build system infers operations from assets, so a single mod can combine multiple types.

### texture_replace

Replace a character's texture without touching the mesh.

```json
{
  "format": "cg3h-mod/1.0",
  "metadata": {"name": "MelRetexture", "author": "YourName", "version": "1.0.0"},
  "type": "texture_replace",
  "target": {"character": "Melinoe"},
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
  "format": "cg3h-mod/1.0",
  "metadata": {"name": "MelCrown", "author": "YourName", "version": "1.0.0"},
  "type": "mesh_add",
  "target": {"character": "Melinoe"},
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

**CC-free**: Only the GLB (custom meshes) and PNGs are shipped. The shared CG3HBuilder Thunderstore plugin builds the GPK on end users' machines at game launch.

### mesh_replace

Replace a character's meshes entirely with new ones.

```json
{
  "format": "cg3h-mod/1.0",
  "metadata": {"name": "CustomMel", "author": "YourName", "version": "1.0.0"},
  "type": "mesh_replace",
  "target": {"character": "Melinoe"},
  "assets": {
    "glb": "CustomMelinoe.glb",
    "textures": ["CustomMel_Color.png"]
  }
}
```

**Build**: Loads the original GPK, replaces specified meshes, updates MeshBindings, builds standalone `.pkg` for textures.

**Distribution note**: ships the modified geometry as a GLB inside the Thunderstore package. CG3HBuilder rebuilds the GPK on the user's machine using their game files, so no copyrighted base geometry is redistributed.

### mesh_patch

Modify existing mesh vertices (reshape, sculpt) without adding or removing meshes.

```json
{
  "format": "cg3h-mod/1.0",
  "metadata": {"name": "MelReshape", "author": "YourName", "version": "1.0.0"},
  "type": "mesh_patch",
  "target": {"character": "Melinoe"},
  "assets": {
    "glb": "MelinoeEdited.glb"
  }
}
```

**Build**: Loads the original GPK, matches GLB meshes to GR2 meshes by name, patches vertex positions/normals/UVs in-place.

**Distribution note**: ships the edited GLB; CG3HBuilder repatches the user's local GPK at runtime.

### animation_patch

Replace or modify specific animation curves on a character.

```json
{
  "format": "cg3h-mod/1.0",
  "metadata": {"name": "MelIdleEdit", "author": "YourName", "version": "1.0.0"},
  "type": "animation_patch",
  "target": {"character": "Melinoe"},
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
| `filter` | string | no | Substring filter for **export** — reduces GLB size by only exporting matching animations. At build time, the GLB contents are the sole authority on which animations are patched (no import-side filtering). |

**Build**: Loads original GPK, matches GLB animations to GR2 animations by name, patches curve data, outputs modified GPK. Only animations present in the GLB are patched — no filter needed at build time.

**CC-free**: Only the GLB (containing modified curves) is shipped.

### Combining types

A mod can perform multiple operations:

```json
{
  "format": "cg3h-mod/1.0",
  "metadata": {"name": "MelCombo", "author": "YourName", "version": "1.0.0"},
  "type": ["mesh_patch", "animation_patch"],
  "target": {"character": "Melinoe"},
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
  "format": "cg3h-mod/1.0",
  "metadata": {
    "name": "MyMod",
    "author": "YourName",
    "version": "1.0.0",
    "description": "Description of the mod"
  },
  "type": "texture_replace",
  "target": {
    "character": "Melinoe"
  },
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
| `format` | Must be `"cg3h-mod/1.0"` |
| `metadata.name` | Mod name (used for Thunderstore manifest and H2M folder naming) |
| `metadata.version` | Semver version string |
| `metadata.author` | Author name |
| `type` | One of the 5 types, or an array for combined types |
| `target.character` | Target character name (must match a `.gpk` in `_Optimized/`) |

**Optional fields:**

| Field | Description |
|-------|-------------|
| `metadata.description` | Human-readable description |
| `assets.glb` | Path to GLB file (relative to mod.json) |
| `assets.textures` | Array of PNG filenames |
| `assets.animations` | Animation patch config (`patch` + optional `filter`) |

---

## Thunderstore Package Structure

CG3H mods are **data-only** packages. All runtime logic (GPK building, PKG loading, texture registration) is handled by the shared **CG3HBuilder** Thunderstore plugin, which is declared as a dependency.

```
AuthorName-ModName/
  manifest.json             Thunderstore manifest (auto-generated, depends on CG3HBuilder)
  icon.png                  Thunderstore icon
  README.md                 Thunderstore description
  plugins/
    AuthorName-ModName/
      manifest.json         H2M plugin manifest (name + version, no main.lua)
  plugins_data/
    AuthorName-ModName/
      mod.json              CG3H mod descriptor
      *.glb                 Custom meshes (mesh_add/mesh_replace, CC-free)
      manifest.json         Export manifest (mesh routing metadata)
      *.pkg                 Standalone texture package (pre-built)
```

The CG3HBuilder plugin (installed once as a shared dependency) handles:
- Scanning all installed CG3H mods via `mod.json` discovery
- Building GPKs at game launch from GLBs + the player's local game files
- Caching built GPKs (only rebuilds when mods change)
- Loading standalone `.pkg` files via `rom.game.LoadPackages`
- Merging multiple mods targeting the same character into one GPK

---

## Conflict Resolution

### Detection

When multiple mods target the same character, CG3H detects conflicts per-operation:

| Mod A | Mod B | Conflict? | Resolution |
|-------|-------|-----------|------------|
| texture_replace (same texture) | texture_replace (same texture) | Yes | Priority order; higher wins |
| mesh_add (custom texture, same name) | mesh_add (custom texture, same name) | No | Both auto-prefixed with mod id at build time (v3.6) |
| mesh_add | mesh_add | No | Both appended (same names auto-prefixed with mod id) |
| mesh_replace (pure) | mesh_replace (pure, same meshes) | No (v3.9+) | Both become switchable variants in the outfit picker; merged stock entry holds the union |
| mesh_replace | mesh_add | No | mesh_add is additive (always visible); mesh_replace becomes a picker variant |
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

### Multi-mod merging (runtime)

CG3H is not just a build tool — the **CG3HBuilder plugin** runs on end-user
machines to fuse multiple mods into one GPK per character at game launch.
Two mods cannot each ship their own `Melinoe.gpk`; the runtime merger
resolves this automatically.

The merger:
1. Scans all installed CG3H mods in `plugins_data/`
2. Groups mods by target character
3. Single-pass GLB merge via `_merge_glbs()` (meshes + animations + materials)
4. Single `convert()` call produces the final `{character}.gpk`
5. Registers the GPK via `rom.data.add_granny_file`

Build-time conflict pre-flight is exposed via `tools/mod_info.py:check_conflicts()`
and used by the GUI Mods tab.

See [`architecture.md`](architecture.md) for the full merge flow and conflict check details.

### Outfit Switching (v3.9+)

When two or more `mesh_replace` mods target the same character, CG3H
emits **both** as switchable variants — the player picks which outfit
they want per scene from the in-game Mod Manager. No more "which mod
wins"; every installed mod stays accessible.

The picker dropdown per scene entry shows:

- **Stock** — the unmodified game content for that entry (auto-applied
  on first frame so the game opens on vanilla by default, not a merged
  view).
- **<Mod name>** — that one body-replacer's contribution.
- **Apply to all scenes** — cascade one choice across every scene the
  character appears in.

**What shows up in the picker (v3.9 rule):**

- A mod that is **purely `mesh_replace`** (no `mesh_add` in its type
  list) gets an entry in the picker — it offers an alternative body.
- A mod that includes **`mesh_add`** (even alongside `mesh_replace`)
  is treated as **additive**: its meshes are always merged into the
  default stock entry and always render, regardless of the picker
  choice. It does NOT appear as a picker option.

This means `["mesh_add", "mesh_replace"]` mods like "Hecate with an
added chest piece" are always-on accessories — the picker is reserved
for choosing between *whole alternative bodies*. Splitting a mixed
mod's GLB into add-only / replace-only halves (so the replace half
becomes a picker variant while the add half stays additive) is a
v4.x goal — not built.

**Builder-side:** for each `mesh_replace` mod declaring
`target.mesh_entries`, the builder emits a slim variant entry named
`{Character}_{SanitizedModId}_V{N}_Mesh`, plus one
`{Character}_Stock_V{N}_Mesh` per targeted entry (raw bytes from the
game's stock GPK). The default-name stock entry (`HecateHub_Mesh` etc.)
stays populated with the **union of all mods** so the drawable is
pre-sized for the maximum footprint at scene-load. Runtime remaps swap
to a strictly-smaller variant on demand.

**H2M dependency:** v3.9 ships alongside paired pool-size patches in
Hell2Modding (vertex pool 64 → 128 MB, index pool 32 → 64 MB). Both
pools live in the DX12 upload heap (system RAM, not VRAM), so the
extra capacity is cheap. Without the patches, variant entries
overflow the default budgets and weapons/enemies render as
placeholder. See `docs/rendering_pipeline.md` Part 3b.

**Persistence:** active picks are saved to `cg3h_mod_state.json`
under `active_variants` and restored on the first ImGui frame of the
next session.

---

## Pipelines

CG3H operates at two stages of the mod lifecycle:

### Build-time (mod creator)

1. **Create**: Use the GUI Create tab (or CLI `gr2_to_gltf.py`) to export a character
2. **Edit**: Modify in Blender — meshes, textures, animations
3. **Describe**: Write `mod.json` (the GUI generates this automatically)
4. **Build**: `python tools/cg3h_build.py` (or GUI Build tab)
5. **Package**: `python tools/cg3h_build.py --package` for Thunderstore ZIP
6. **Upload**: Upload ZIP to Thunderstore

### Runtime (end user)

1. Install Hell2Modding via r2modman
2. Install CG3H mods from Thunderstore (CG3HBuilder is pulled automatically as a dependency)
3. Launch the game — CG3HBuilder scans all installed CG3H mods
4. For mesh mods: CG3HBuilder builds the GPK from the player's local game files + the shipped GLB (no copyrighted geometry distributed)
5. For multiple mods on the same character: CG3HBuilder merges them into one GPK
6. Built GPKs are cached and only rebuilt when mods change

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
    +-- Generate manifest.json (Thunderstore, depends on CG3HBuilder)
    +-- Smart strip: remove unchanged assets from distribution
    |
    +-- Output H2M-compatible folder structure
```
