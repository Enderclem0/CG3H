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
- Install multiple mods on the same character (auto-merged at runtime)

## Getting Started

### Blender Addon (recommended)

1. Download `cg3h_blender_addon.zip` from the [releases page](https://github.com/Enderclem0/CG3H/releases)
2. In Blender: Edit > Preferences > Add-ons > Install from Disk
3. Enable "CG3H — Hades II Model Tools" and set the game path in preferences
4. **Import**: File > Import > Hades II Model (.gpk)
5. **Export**: File > Export > Hades II Mod (CG3H) — produces a ready-to-install Thunderstore package

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
- **Requires H2M patches** — `rom.data.add_granny_file` API and GPK exact-match fix (PRs pending).

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
