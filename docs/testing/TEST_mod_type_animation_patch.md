# A.5 — `animation_patch` mod type

Goal: edit an animation curve (e.g. change the amplitude of a bone's
rotation during an idle animation) without touching mesh geometry.

---

## Precondition

- Clean r2modman profile.
- Blender 4.2+ with CG3H addon.

## Recipe

1. Import a character via **File > Import > Hades II Model (.gpk)** —
   the import loads meshes + skeleton + action-compatible animation
   tracks.
2. Switch to the animation editor workspace.  Select one of the
   imported actions (e.g. Hecate's `HecateHub_Base_IdleHub_C_00`).
3. Edit a bone's rotation f-curve — e.g. scale the amplitude of the
   spine rotation to 0.5× (subtle but visible in game).
4. Export with **File > Export > Hades II Mod (CG3H)**.  Name
   `TestAnimPatch`.
5. Manually edit `mod.json` after export to set
   `"type": "animation_patch"` (auto-classify only handles mesh
   types).  Add a `target.animation_filter` array listing the animation
   names you edited (optional — if omitted, the builder applies the
   patch to every action).

## Steps

1. Launch the game.
2. Log check: builder logs `Build complete` for the affected character.
   Sanity canary OK.
3. Go to the character.  The modified animation should play with the
   new curve amplitude.
4. Open Mod Manager → Mods tab.  `TestAnimPatch` listed with checkbox.
5. Uncheck the mod → rebuild triggered, animation reverts on next
   scene.

## Expected

- Animation plays with modified curve after install.
- Disable via Mods-tab checkbox rebuilds without the patch.
- No mesh-visibility or picker interaction (animation is not a
  picker-type).

## Checklist

- [ ] Modified animation visibly plays
- [ ] Not in Body picker, not in Accessories
- [ ] Listed in Mods tab
- [ ] Disable → rebuild → next-scene revert

## If it fails

- **Animation unchanged in-game** — most likely the curve export
  didn't replace the stock GR2 animation track.  Check that the mod
  GPK's animation entry (e.g. `Hecate_Torch_Base_Idle_C_00`) has a
  different decompressed size than stock (`tools/gpk_pack.py list`).
- **Character T-poses / glitches** — curve values out of range.  Check
  `_animation_sanitize` logic in `tools/gltf_to_gr2.py` — quaternions
  must be unit-length, translations within ±1000, scales skip if
  off-diagonal > 0.01.
- **Wrong action edited** — GLB exported with a different action
  selected than intended.  Re-export with the correct action active.
