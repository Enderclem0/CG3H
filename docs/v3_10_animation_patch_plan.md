# v3.10 — first-class animation_patch mods

`animation_patch` is already a documented mod type (since v1.1 — see
`docs/mod_spec.md`).  Today it can only ride along with a mesh mod —
the GLB has to contain at least one mesh edit because `convert()`
errors out when no meshes are patched.  v3.10's job is to make
animation_patch a standalone-capable type: a mod can ship just an
animation GLB, no mesh content, and the builder will handle it.

This doc is the audit output of v3.10's first task; it informs the
schema, builder, and addon changes that follow.

## Audit findings

**Existing pipeline:** `tools/gltf_to_gr2.py::convert(...)`

- `patch_animations: bool = False` parameter exists since v1.1.
- When set, the function calls `parse_glb_animations(glb_path)` early
  to extract every animation track from the GLB, then runs
  `patch_animation_entries(...)` AFTER the per-entry mesh loop.
- `patch_animation_entries`:
  - Walks every GPK entry that does NOT end in `_Mesh`
    (i.e. animation entries).
  - For each entry, looks up a matching GLB animation by name (exact
    or normalized — strips `_Skin` / `_Armature` / `.001` suffixes).
  - Patches the GR2 curves in place via the Granny write API.
  - Returns the number of entries patched.
  - Self-contained — does not depend on any mesh-side state.

**Two ship-blockers for animation-only mods:**

1. **`convert()` raises if no meshes were patched** (line 1927):
   ```python
   if total_patched == 0:
       ...
       raise RuntimeError("No meshes were patched — nothing to write")
   ```
   This fires before the animation patch step. An animation-only mod
   has no meshes to patch, so this guard always trips.

   *Fix:* relax the guard — only raise when **both** `total_patched`
   is zero **and** `glb_animations` is empty (or `not patch_animations`).
   Allow the function to fall through to `pack_gpk(...)` when there
   are animations to write.

2. **Per-entry mesh loop runs unconditionally.** Even when the GLB
   has zero meshes, lines 1894-1925 still iterate every `_Mesh` entry,
   call `load_gr2_entry`, run `patch_vertex_data` (which returns 0),
   and free the GR2. Wasteful but harmless.

   *Optional improvement:* short-circuit the mesh loop when
   `len(glb_meshes) == 0`. Skip directly to the animation patch.
   Saves a few seconds on every animation-only build.

**Builder side:** `tools/cg3h_builder_entry.py`

- Already sets `patch_animations` on every `convert()` call from the
  flag `patch_anims = 'animation_patch' in types` (line 711).
- The string `'animation_patch'` is the **legacy** name; we'll rename
  to `'animation_patch'` for consistency with the other
  `mesh_replace` / `mesh_add` types. Keep `'animation_patch'` as an
  alias for backward compatibility (no deployed mod uses it today,
  but the schema string is in our docs).
- Animation-only mods today can already piggy-back on a `mesh_replace`
  build, but cannot be standalone — the merger requires at least one
  GLB mesh to drive the per-entry routing.

**Mod scanner:** `tools/cg3h_builder_entry.py::_classify_mod`

- Currently classifies into `(is_variant, is_accessory)`.
- `is_variant`: pure mesh_replace + has `target.mesh_entries`.
- `is_accessory`: any `mesh_add`.
- Need to extend with `is_animation_only` for the new path.
- Animation-only mods participate in NEITHER variant picker NOR
  accessory toggles — they're a separate axis (always-applied
  patches).

## Schema

`mod.json` for an animation-only mod:

```json
{
  "format": "cg3h-mod/1.0",
  "type": "animation_patch",
  "metadata": { ... },
  "target": {
    "character": "Melinoe",
    "animations": ["MelinoeIdle", "MelinoeRun"]
  },
  "assets": { "glb": "Melinoe.glb" }
}
```

- `type` may be `"animation_patch"` (string) or
  `["animation_patch"]` (list, if mixed with other types in the
  future, though we don't currently mix).
- `target.animations` — **optional**. If omitted, the builder patches
  every animation in the GLB that has a matching GPK entry. If
  provided, only those entries are patched (acts as a whitelist).
  Useful when a modder ships a GLB with many animations but only
  wants to replace a few.
- `target.character` — required, same as other types.
- `assets.glb` — required, must contain the animation tracks.

## Conflict policy

Two animation_patch mods touching the same animation entry:

- Detect at scan time in `mod_info.check_conflicts`.
- Resolution: existing `priority.json` wins; alphabetical mod_id is
  the deterministic fallback.
- Warn (don't error) so users see what's happening.
- Same animation in mesh_replace + animation_patch: the
  animation_patch mod wins (it's more specific in intent).

## Builder routing

In `scan_and_build_all` (per-character merge):

- If ALL mods for a character are animation_patch:
  - Skip `_merge_glbs` entirely.
  - For each mod (in priority order), run
    `convert(glb=mod.glb, gpk=stock.gpk, patch_animations=True,
             allow_topology_change=False, ...)`
    against the *current* working GPK, accumulating animation patches.
  - Same approach as multi-mod stacking on the mesh side, but using
    animation entries as the patch target.
- If MIXED (mesh + animation mods):
  - Run mesh merge first (existing path).
  - Then run animation_patch mods AGAINST the merged GPK,
    layering animations on top.

## Blender addon

- Auto-detection: when no mesh has changed positions vs the imported
  baseline (`.baseline_positions.npz`) AND there's at least one
  animation track in the export, default the mod-type dropdown to
  `animation_patch`.
- Sidebar UI: add an "animation_patch" radio next to the existing
  `mesh_add` / `mesh_replace` toggles.
- Validation popup: warn if the user has both mesh edits AND selected
  `animation_patch` (suggest mixed-type or split-into-two-mods).

## Tests

- `test_core::test_animation_patch_classification` — schema accepted,
  classifier returns the right tuple.
- `test_core::test_animation_patch_conflict` — two mods touching
  the same entry produce a warning + winner.
- End-to-end smoke: build a fixture mod that replaces one animation
  on YoungMel (small cast for fast iteration), verify the GPK
  contains the patched curve.

## Sequencing

Tasks 78–85 (in the task list) execute in dependency order. Schema
and classifier are independent and unblock the builder, conflict
detection, and addon paths in parallel. End-to-end test gates the
release.

## Out of scope for v3.10

- Adding new animation entries (creating animations the game doesn't
  have an entry for). Today we only patch existing entries.
- Per-bone-track granularity (animation_patch replaces a whole
  animation entry; can't replace just one bone's tracks within an
  animation entry).
- Skeleton extension — that's v4.0.
