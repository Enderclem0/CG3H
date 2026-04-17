# B.2 — Accessory picker (v3.9 mesh-add toggle)

Goal: installed `mesh_add` mods appear as checkboxes in the Accessories
section; toggle flips visibility within one frame via the per-mesh
gate; effect persists across scenes; body and other characters are
unaffected.

---

## Precondition

- At least one `mesh_add` mod installed (e.g. `TestMeshAdd` from A.1,
  plus ideally a real one like `Enderclem-MelinoeGlasses` and
  `Enderclem-HecateBiMod`).
- Clean r2modman profile or known state.

## Steps

### B.2.1 Listing

1. Launch the game.  Log check: `sanity_check_gmd` OK.
2. Open Mod Manager → Characters → [Character with mesh_add mod].
3. Expand **Accessories** section.  Each installed `mesh_add` mod
   appears as a checkbox.
4. Default state: all checked (always-on is the v3.9 default).
5. Each accessory's mesh(es) should be visible on the character
   in-game.

### B.2.2 Toggle off

1. Uncheck one accessory's box.
2. The accessory's meshes should vanish within one frame (no
   rebuild, no scene reload).
3. Log line expected: `[mesh-gate] <mod-id> (hide) N mesh(es)`.
4. The character's body and other meshes must remain visible and
   unchanged.

### B.2.3 Toggle on

1. Re-check the box.
2. The accessory returns within one frame.  Log: `[mesh-gate]
   <mod-id> (show) N mesh(es)`.

### B.2.4 Multiple accessories

1. With 2+ mesh_add mods on one character (e.g. HecateBiMod + a
   second test mod), toggle them in different combinations:
   - Both on → both visible
   - A off, B on → only B visible
   - Both off → character body alone, no accessories
2. Verify each combination renders correctly.

### B.2.5 Cross-character isolation

1. Toggle a Melinoe accessory off.
2. Switch to Hecate scene.  Hecate's body + her accessories unchanged.
3. Confirm toggle only affected the intended character's entries.

### B.2.6 Scene transitions

1. Toggle an accessory off.
2. Walk through several scene transitions (Hub → Battle → etc.).
3. After each transition, accessory remains hidden.
4. Re-toggle on.  Scene transitions preserve new state.

### B.2.7 Persistence

1. With one accessory off, exit the game.
2. Relaunch.
3. Expected behaviour: **the mod was disabled at the last build, so
   next-launch rebuild excludes it entirely from the GPK**.  The
   accessory remains hidden.  No warning.
4. Mod Manager: the accessory checkbox reflects the disabled state.
5. Re-check the box.  **In-session** this triggers a REBUILD (not a
   live toggle) because the mesh isn't in the current GPK.  After
   rebuild completes, the accessory returns on next scene load.

## Expected

- Accessory checkboxes per character.
- Toggle off: instant, no rebuild, body unaffected.
- Toggle on (session-started-enabled): instant.
- Toggle on (session-started-disabled): rebuild required.
- Persistence across session: enabled=current reflects last build.

## Checklist

- [ ] All mesh_add mods listed as Accessories per character
- [ ] Toggle off: instant (log `[mesh-gate] ... (hide)`)
- [ ] Toggle on after in-session off: instant (log `(show)`)
- [ ] Body / other accessories unaffected by single toggle
- [ ] Scene transitions preserve state
- [ ] Cross-character isolation holds
- [ ] Re-enable across sessions triggers rebuild (documented limitation)

## If it fails

- **Toggle off hides the body too** — `set_mesh_visible` matched
  meshes by name-hash but hit something unexpected.  Verify
  `mod.new_mesh_routing` lists only the added mesh names; if it's
  empty or points to stock names, Blender auto-classify failed.
- **Toggle off triggers rebuild instead of instant** — fell through
  to rebuild path.  Check that `rom.data.set_mesh_visible` exists and
  that `target_mod.has_mesh_add` is true in `mod_state.lua`.
  Log-check: `[mesh-gate] ... no meshes toggled` means the lookup
  found zero matches — likely a name mismatch between the
  `new_mesh_routing` entry and what's actually in the GPK.  Use
  `tools/gpk_pack.py list` + `rom.data.dump_mesh_info` to compare.
- **Every character's meshes break when toggling an accessory** —
  regression of the earlier "icount=0 poisons command list" bug.
  Confirm the current `set_mesh_visible` writes to GMD+0x4C (mesh_type),
  NOT +0x38 (icount) or +0x40 (tex_hash).  Look for the `HIDE_TYPE = 2`
  sentinel in `draw.cpp`.
