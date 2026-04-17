# A.2 — `mesh_replace` mod type (pure)

Goal: a mod that **only** replaces existing character mesh geometry
(no new meshes added) becomes a **Body** picker entry in v3.9.

---

## Precondition

- Clean r2modman profile, no other CG3H mesh mods.
- Blender 4.2+ with the addon, game path set.

## Recipe

1. **File > Import > Hades II Model (.gpk)** — pick Hecate.
2. Import loads the full character.  Note the entry meshes
   (`Hecate_Rig:HecateBattle_MeshShape`, etc.).
3. In edit mode, modify vertex positions on ONE existing mesh
   (e.g. scale the body uniformly, or sculpt a visible change).
   **Do NOT add new meshes.**
4. In the CG3H sidebar, confirm only the modified mesh's entry
   checkbox matches the mesh's original entry (`HecateBattle_Mesh`
   etc.).
5. **File > Export > Hades II Mod (CG3H)**.  Mod name:
   `TestMeshReplace`.  Install to r2modman.
6. Verify `mod.json` type is `"mesh_replace"` (single string, NOT a
   list with `mesh_add`) and `new_mesh_routing` is absent.
7. Confirm `target.mesh_entries` lists the stock entry names you
   modified.

## Steps

1. Launch the game.
2. Log check: `sanity_check_gmd: 'HecateHub_Mesh' layout OK`.
3. Open Mod Manager → Characters → Hecate.
4. In the **Body** section, per scene entry there should be a
   dropdown showing: `Stock` (first), `TestMeshReplace`.
5. On game boot, the first ImGui frame auto-applies `"stock"` — so
   the entry shows as `Stock` initially.  Hecate visibly renders
   like un-modded in the Hub.
6. Pick `TestMeshReplace` from the Hub dropdown.  Hecate's body
   changes to the modded shape within one frame.  No rebuild log.
7. Pick `Stock`.  Back to vanilla body.
8. Verify the Accessories section is **empty** (this mod has no
   `mesh_add`).
9. Walk around; scene transitions preserve the pick.

## Expected

- Mod appears in Body dropdown, not Accessories.
- Swap Stock ↔ TestMeshReplace is instant, no rebuild.
- `restore_stock: 'HecateHub_Mesh' remap cleared` appears in
  `LogOutput.log` when Stock is picked.
- `swap_to_variant: 'HecateHub_Mesh' -> 'Hecate_TestAuthor_TestMeshReplace_V<N>_Mesh'`
  appears when the mod is picked.

## Checklist

- [ ] mod.json type is `"mesh_replace"` single string
- [ ] No `new_mesh_routing` (or if present, doesn't classify as additive)
- [ ] Body dropdown shows Stock + TestMeshReplace
- [ ] Accessories section is empty
- [ ] Pick swaps instantly, no rebuild
- [ ] Scene transition preserves pick

## If it fails

- **Mod in Accessories instead of Body** — Blender detected the
  edited mesh as new (not matching `cg3h_original_meshes`).  Check
  that `context.scene["cg3h_original_meshes"]` is populated after
  import.  If empty, the addon's import step failed to set it.
- **No variant entry in GPK** — check `plugins_data/Enderclem-CG3HBuilder/Hecate.gpk`
  via `python tools/gpk_pack.py list`.  Should contain
  `Hecate_TestAuthor_TestMeshReplace_V<N>_Mesh`.  Missing →
  `_build_variant_entries` in `cg3h_builder_entry.py` skipped it;
  check `cg3h_status.json` `variants` field.
- **Instant swap but textures are wrong / white** — `populate_entry_textures`
  didn't fill GMD+0x44.  Grep log for `populate_entry_textures`
  errors.  Ensure `rom.data.populate_entry_textures` exists (log
  `set_mesh_visible: required symbols missing`-style ERRORs mean
  H2M binding didn't register).
