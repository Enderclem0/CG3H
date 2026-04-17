# A.3 — `mesh_patch` mod type

Goal: a mod that patches vertex positions on an existing mesh (without
replacing the whole mesh geometry or adding new meshes) installs and
renders the patched positions.

**Note**: `mesh_patch` is semantically similar to `mesh_replace` in the
builder (both route the edited vertex data through `convert()`).  The
distinction is that `mesh_patch` preserves topology (same vertex count,
same indices) — just position deltas.  v3.9 classifies it as neither
picker-variant nor accessory; it always merges into stock.

---

## Precondition

- Clean r2modman profile.

## Recipe

1. Import Hecate in Blender.
2. In edit mode, select a vertex on the body and move it ~5 units on
   the Z axis (visible bump/indent).
3. Export as `TestMeshPatch`.
4. Manually edit the exported `mod.json` to set `"type": "mesh_patch"`
   (the Blender addon auto-detects add/replace based on name-matching;
   it does not emit `mesh_patch` automatically — modders opt-in by
   hand-editing).  Verify `target.mesh_entries` is set and
   `new_mesh_routing` is absent.
5. Re-install via `python tools/install_plugin_local.py --no-build`
   OR just copy the mod to `plugins_data/`.

## Steps

1. Launch the game.
2. Sanity-check canary OK in log.
3. Go to Hecate scene.  Visible bump/indent should show on the modded
   mesh.
4. Open Mod Manager → Characters → Hecate.
5. **Body** section: no picker entry for `TestMeshPatch` (classified
   as neither variant nor accessory).
6. **Accessories**: no entry either.
7. **Mods** section: `TestMeshPatch` listed with a checkbox (global
   enable/disable via the v3.8 path).
8. Uncheck the Mods-tab checkbox.  Expected: triggers REBUILD (not a
   live mesh-gate — mesh_patch has no registered accessory meshes to
   hide).  After rebuild completes, the patch is removed from the
   merged GPK.  Requires restart (new GPK).

## Expected

- Vertex patch visible in-game.
- No picker visibility (v3.9 scoped to mesh_add / mesh_replace).
- Global enable/disable rebuilds the merged GPK.

## Checklist

- [ ] Vertex bump renders correctly on the character
- [ ] Not in Body dropdown
- [ ] Not in Accessories section
- [ ] Listed in Mods tab with global enable/disable
- [ ] Disabling triggers rebuild; post-restart the patch is gone

## If it fails

- **Vertex patch invisible** — the merged GPK didn't pick up the GLB
  changes.  Verify `plugins_data/Enderclem-CG3HBuilder/Hecate.gpk`
  has the expected decompressed size (modded > stock).
- **Classified as mesh_add or mesh_replace** — Blender export
  auto-detected a mesh name mismatch.  If vertex count differs from
  stock, the addon treats it as a new mesh.  For `mesh_patch` the
  count MUST match; the modder is responsible.  Document in the
  mod's README.
