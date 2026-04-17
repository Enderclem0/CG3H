# A.1 — `mesh_add` mod type

Goal: verify an additive accessory mod (new mesh merged into stock
entries) installs, renders, and appears as a togglable **Accessory**
checkbox — not a **Body** picker entry.

---

## Precondition

- Clean r2modman profile.
- No other CG3H mesh mods installed yet.
- Blender 4.2+ with the CG3H addon installed and game path set.

## Recipe (author a test mod)

1. In Blender: **File > Import > Hades II Model (.gpk)** — pick
   Melinoe's GPK.
2. In viewport: add a simple primitive mesh (e.g. `Add > Mesh > Cylinder`).
   Position it near the character's head.
3. Parent the new mesh to the armature (Ctrl+P → Armature Deform With
   Automatic Weights).  Rename the mesh to something obvious like
   `TestHat`.
4. In the **CG3H** sidebar (N key → CG3H tab), confirm `TestHat` is
   checked under the entry checkboxes (all entries by default).
5. **File > Export > Hades II Mod (CG3H)**.  Mod name: `TestMeshAdd`.
   Author: `TestAuthor`.  Check "Install to r2modman after build".
6. Verify the exported `mod.json` has:
   ```json
   "type": "mesh_add",
   "target": { "character": "Melinoe", "mesh_entries": [...],
               "new_mesh_routing": { "TestHat": [...] } }
   ```
   (not `"mesh_replace"` — if it is, the Blender auto-classify step
   from v3.9 is broken; see [`TEST_build_blender.md`](TEST_build_blender.md).)

## Steps

1. Launch the game.
2. Log check — tail `LogOutput.log`:
   - `CG3H patch: [OK  ] vertex pool 64->128 MB`
   - `CG3H patch: [OK  ] index pool 32->64 MB`
   - `sanity_check_gmd: 'HecateHub_Mesh' layout OK (N meshes)`
3. Open **CG3H Mod Manager** from the menu bar.  Expand **Characters
   > Melinoe**.
4. Verify the **Body** section is empty (no pure mesh_replace mods
   installed).
5. Verify the **Accessories** section shows `TestMeshAdd` with a
   checked box.
6. Go to Melinoe in-game (hub).  Confirm the `TestHat` mesh is
   visible on her.
7. Uncheck `TestMeshAdd`.  `TestHat` should disappear within one
   frame.  `Hades II.log` must NOT gain `RequestBufferUpdate failed`
   lines.
8. Re-check the box.  `TestHat` reappears within one frame.
9. Walk to a different scene (e.g. Olympus, Chaos).  Toggle again.
   Effect immediate, no rebuild messages.

## Expected

- Mod in **Accessories** section, not Body.
- Toggle OFF → mesh invisible; toggle ON → mesh visible.
- No rebuild triggered (no "Rebuilding X..." lines in log during
  toggle).
- Other meshes (Melinoe body, other characters, weapons) unaffected.

## Checklist

- [ ] mod.json type is `"mesh_add"`
- [ ] Accessory appears in Accessories section, not Body
- [ ] Toggle works instantly (no rebuild)
- [ ] No `RequestBufferUpdate failed` lines added
- [ ] Scene transition preserves toggle state
- [ ] Melinoe body unaffected by toggle

## If it fails

- **Ends up in Body picker instead of Accessories** — Blender
  auto-classify is wrong; check `mod.json` type field.  A
  `["mesh_add", "mesh_replace"]` list is additive-only (correct); a
  plain `"mesh_replace"` is misclassified.  Fix in
  `blender_addon/cg3h/__init__.py` classifier around the `has_replace
  / has_add` logic.
- **Toggle triggers rebuild** — check `runtime.lua::toggle_mod_visibility`
  falls into the mesh_add branch.  Requires `mod.has_mesh_add` set and
  `mod.new_mesh_routing` non-empty — grep the `LogOutput.log` for
  `[mesh-gate]` vs `[draw-gate]` to see which path fired.
- **Mesh invisible even when ON** — the mesh name in the GLB may have
  picked up a `.001` suffix.  Open the merged GPK with
  `python tools/gpk_pack.py list` and verify the mesh name is in
  `new_mesh_routing`.
