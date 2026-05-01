# CG3H — Custom Geometry 3D for Hades II

[![Release](https://img.shields.io/github/v/release/Enderclem0/CG3H)](https://github.com/Enderclem0/CG3H/releases)

Extract, edit, and repack 3D models and textures for Hades II.
Build non-destructive mod packages for [Hell2Modding (H2M)](https://github.com/SGG-Modding/Hell2Modding) — zero game files modified.

## What You Can Do

- Edit character meshes (move vertices, sculpt, change topology)
- Add new meshes (accessories, armor, custom geometry)
- Paint bone weights in Blender
- Replace or edit textures (PNG/DDS)
- Edit animation curves
- Target specific mesh entries (battle-only, hub-only for multi-entry characters)
- **Install multiple body mods on the same character and pick between them in-game** (v3.9)
- **Toggle accessories (glasses, capes, etc.) individually** — checkbox in the in-game mod manager, instant effect (v3.9)

## Getting Started

### Blender Addon (recommended)

1. Download `cg3h_blender_addon.zip` from the [releases page](https://github.com/Enderclem0/CG3H/releases)
2. In Blender: Edit > Preferences > Add-ons > Install from Disk
3. Enable "CG3H — Hades II Model Tools" and set the game path in preferences
4. **Import**: File > Import > Hades II Model (.gpk)
5. **Export**: File > Export > Hades II Mod (CG3H) — produces a `build/` folder with the assembled mod files; zip its contents when you're ready to publish on Thunderstore

The sidebar panel N key (CG3H tab) lets you assign new meshes to specific entries for multi-entry characters.

### GUI

```bash
python tools/converter_gui.py
```

- **Create** — Pick a character, export to a mod workspace
- **Build** — Set mod name + author, build Thunderstore package, one-click r2modman install
- **Mods** — View installed CG3H mods and build status

### For Players

Install the [CG3HBuilder](https://thunderstore.io/c/hades-ii/p/Enderclem/CG3HBuilder/) plugin via r2modman. It handles everything at runtime — no Python needed.

## In-Game Mod Manager (v3.9)

Open the **CG3H Mod Manager** from the menu bar.  Under the **Characters** tab, each character expands to two picker sections:

- **Body** — dropdown per scene (Hub, Battle, …) listing **Stock** + every pure `mesh_replace` mod.  Pick one to swap bodies instantly — no rebuild, no restart.
  - **Apply to all scenes** cascades one pick across every scene entry the mod covers.
  - Default is **Stock** (auto-applied on the first frame); your picks persist to the next session.
- **Accessories** — checkbox per installed `mesh_add` mod.  Toggle on/off to show/hide the added meshes within the same frame.

Two mods of the same type on the same character are **not a conflict in v3.9** — both get picker entries.  The only true conflict is two pure `mesh_replace` mods targeting the *same mesh*, which resolve via the picker (user chooses).

## Mod Types

| Type | Description |
|---|---|
| `mesh_add` | Add new meshes to a character |
| `mesh_replace` | Replace existing character meshes |
| `texture_replace` | Replace textures via standalone .pkg |
| `animation_patch` | Edit animation curves |

See [`docs/mod_spec.md`](docs/mod_spec.md) for the full mod specification.

## Known Limitations

- **New mesh bone bindings** — new meshes inherit bone bindings from the best-matching existing mesh. Bone binding expansion planned for v4.0.
- **65,535 vertices max per mesh** — engine limitation (uint16 index buffers).
- **Adding/removing bones** is not supported — the skeleton is read-only.
- **Re-enabling an accessory mid-session** requires a rebuild if the mod was disabled at the last build (runtime mesh-gate can hide what's in the merged GPK but can't add meshes that aren't there).  Toggling within a session where the mod was enabled at build time is instant.
- **Animation patches are not live-toggleable.** Animation curves are baked into the merged GPK at build time — disabling an `animation_patch` mod requires a rebuild + restart (the v3.8+ instant-toggle covers `_Mesh` entries only).
- **Requires Hell2Modding 1.0.92 or newer.** r2modman pulls in upstream `Hell2Modding-Hell2Modding` automatically as a dependency of any CG3H mod.

## Requirements

For development / CLI usage:

```
pip install -r requirements.txt
```

The game's `granny2_x64.dll` is required (auto-detected from Steam).

## Documentation

- [`docs/skinning_guide.md`](docs/skinning_guide.md) — **Modder guide**: entries, templates, and BoneBindings explained
- [`docs/mod_spec.md`](docs/mod_spec.md) — Mod types, mod.json format, conflict resolution
- [`docs/file_formats.md`](docs/file_formats.md) — GPK / SDB / GR2 / PKG binary layouts
- [`docs/texture_pipeline.md`](docs/texture_pipeline.md) — Texture export/import pipeline
- [`docs/architecture.md`](docs/architecture.md) — Implementation decisions and internals
- [`CHANGELOG.md`](CHANGELOG.md) — Release history
