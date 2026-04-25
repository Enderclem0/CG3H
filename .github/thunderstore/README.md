# CG3H Builder

Runtime builder for [CG3H](https://github.com/Enderclem0/CG3H) mesh mods.

Builds modded GPK files on your machine from your own copy of the game — no copyrighted content is shipped through Thunderstore.

## For mod users

Install via r2modman. CG3H mesh mods declare this package as a dependency, so it'll come along automatically.

What you get in-game (`INSERT` → CG3HBuilder → Mod Manager):

- **Per-character outfit picker** — multiple body-replacement mods coexist; pick which one you wear per scene.
- **"Apply to all scenes" cascade** — set one body across every scene the mod targets, in one click.
- **Accessory toggles** — turn individual `mesh_add` mods on/off live, no rebuild needed.
- **Mod manager view** — per-character build state, error reporting, mod list, summary totals.

## For mod creators

CG3H mesh mods are data-only packages (GLB + textures + manifest). The Blender addon and authoring tools live at the [main repo](https://github.com/Enderclem0/CG3H).

To depend on this package, add it to your mod's `manifest.json` dependencies (use the latest released version):

```json
"dependencies": ["Enderclem-CG3HBuilder-<version>"]
```

The full mod-author guide, schema, and Blender addon are in the [CG3H repo's docs](https://github.com/Enderclem0/CG3H#for-mod-authors).
