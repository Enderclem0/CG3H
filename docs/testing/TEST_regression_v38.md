# D.1 — v3.8 regression

Goal: confirm the v3.8 draw-gate + enable/disable flow still works in
v3.9.  No surprises for users upgrading from v3.8 with existing mod
installs.

---

## Precondition

- Fresh r2modman profile with CG3HBuilder v3.9.0 + Hell2Modding
  (cg3h/v3.9.0 branch head).
- At least one installed mod (either from the A.x sheets or a real
  user mod).

## Steps

### D.1.1 `rom.data.set_draw_visible` (entry-level hide)

1. Launch the game.
2. Open the Mods tab in the Mod Manager.
3. Every installed mod listed with a global enable/disable checkbox.
4. Uncheck a mod for a character whose meshes are currently visible.
5. Expected behaviour:
   - For `mesh_add` mods — runtime uses the new **mesh-gate**
     (per-mesh) path, not draw-gate.  Log: `[mesh-gate] ... (hide)`.
   - For `mesh_replace` (pure) mods — handled by the v3.9 picker; the
     Mods-tab checkbox acts as the variant-set-to-stock control.
   - For `animation_patch` — entry-level draw-gate (rare in practice;
     usually combined with mesh_add/replace anyway).
6. The v3.8 `rom.data.set_draw_visible` API is still present
   (exposed by H2M's `draw.cpp`) and callable from Lua if needed,
   but CG3H runtime no longer triggers it directly for mesh_add
   mods.  Verify via `print(rom.data.set_draw_visible)` from a Lua
   snippet — it's still defined.

### D.1.2 Shadow cave still active

1. Grep `LogOutput.log` for `draw: shadow cave at`.  Line should be
   present at plugin init, with a non-zero address.
2. If the log says `draw: shadow cave NOT installed — signature
   mismatch`, that's a post-game-update regression.  Not blocking
   for v3.9 (cave fails open, reverts to v3.7 behaviour of shadows
   not participating in hash remap), but note it.

### D.1.3 Pool patches

1. Grep `LogOutput.log` for `CG3H: raised static vertex pool` and
   `CG3H: raised static index pool`.  Both should be present.
2. `CG3H patch: [OK  ]` for both.
3. No `CG3H: could not find ... pool patch site` warnings.
4. `RequestBufferUpdate failed` count in `Hades II.log` should be
   zero after the patches take effect.

### D.1.4 Enable/disable persistence

1. Disable a mod via Mods tab → rebuild completes → quit game.
2. Relaunch.  Mod still disabled.  Rebuild skipped (cache key
   matches).  `cg3h_mod_state.json` persists the flag.
3. Re-enable → rebuild → effect returns after scene load.

### D.1.5 HashGuid::Lookup timing

1. Grep `LogOutput.log` for `populate_entry_textures: hash=0` or
   `set_mesh_visible: entry hash=0`.  Should be zero such lines on
   first ImGui frame (HashGuid::Lookup returns 0 only before any
   scene has loaded, and our first-frame callbacks run after that).
2. Any `hash=0` warning is a v3.8 timing regression.

### D.1.6 Legacy PKG filename

1. v3.7 named PKGs `CG3HBuilder-<mod_id>.pkg`.
2. v3.8+ renamed to `Enderclem-CG3HBuilder-<mod_id>.pkg` to satisfy
   H2M's filename-GUID check.
3. If you have an old mod install from v3.7 left over, runtime
   auto-migrates via `os.rename` in `runtime.lua::load_textures`.
4. Log: `[CG3H]   Migrated legacy PKG for <mod.name>` on first launch
   after upgrade.

## Expected

- v3.8 draw-gate still callable (for API consumers).
- Shadow cave installed.
- Pool patches applied.
- Enable/disable persistence.
- No HashGuid::Lookup=0 warnings.
- Legacy PKG migration still works.

## Checklist

- [ ] `rom.data.set_draw_visible` exists as a callable
- [ ] Shadow cave installed (log: `draw: shadow cave at ...`)
- [ ] Pool patches OK (both `[OK  ]`)
- [ ] Zero `RequestBufferUpdate failed` lines
- [ ] Enable/disable persists across sessions
- [ ] No `hash=0` warnings on first ImGui frame
- [ ] Legacy PKG auto-migration still fires (if applicable)

## If it fails

- **Shadow cave NOT installed** — game update shifted the byte
  sequence at `DoDraw3D + 0x148E4`.  Check `install_shadow_cast_patch`
  in `draw.cpp`.  Not a v3.9 regression; filing a separate task.
- **Pool patches SKIPPED** — scan pattern changed.  `CG3H patch:
  [SKIP]` in the summary.  Mods will regress to `Blank_Mesh`
  fallback.  Shipping the wrong build; don't tag v3.9.0 until fixed.
- **HashGuid=0 on first frame** — our first-ImGui-frame hook fired
  before the hash system was fully up.  This shouldn't happen (by
  the H2M init order documented in memory), but if it does, defer
  the variant-apply call by one frame.
