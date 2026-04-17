# B.1 — Body picker (v3.9 outfit switching)

Goal: with ≥ 2 pure `mesh_replace` mods on the same character, the
Body dropdown lets the user pick between them; apply-to-all cascades
across scenes; default is always Stock on first frame.

---

## Precondition

- Two pure `mesh_replace` mods installed for the SAME character (e.g.
  Hecate) — reuse `TestMeshReplace` from A.2 and create a second one
  (e.g. `TestMeshReplace2`) with a visibly different body shape.
- No `mesh_add` mods for that character (this test isolates picker
  behaviour).

## Steps

### B.1.1 First-frame default

1. Launch the game from a cold state (not a re-enable).
2. Before opening the Mod Manager, note the character's appearance in
   the hub.  Expected: looks **like un-modded Hecate** (auto-applied
   `"stock"` variant).
3. Log must contain: `[variant] applied N default/persisted
   selection(s)`.

### B.1.2 Dropdown content

1. Open Mod Manager → Characters → Hecate.
2. Expand the **Body** section.
3. For each scene entry (Hub, Battle, …) the dropdown shows
   **Stock**, **TestMeshReplace**, **TestMeshReplace2** — in that
   order (Stock always first).
4. Preview text shows the active pick (should read `Stock` initially).

### B.1.3 Manual pick

1. In the Hub row, pick `TestMeshReplace`.  Hecate's Hub body changes
   instantly.
2. Change to `TestMeshReplace2`.  Body changes again, no rebuild.
3. Change back to `Stock`.  Body reverts to vanilla.
4. Scene-transition (leave Hub, come back).  Pick state persists
   (still `Stock` in the preview).

### B.1.4 Apply to all scenes

1. Below the per-entry dropdowns, find **Apply to all scenes:**.
2. Pick `TestMeshReplace`.  All scene entries (Hub, Battle, ...) swap
   to that mod's variant for that scene.  Preview text updates for
   each.
3. Pick `Stock`.  All scenes revert.

### B.1.5 Apply-to-all filter

1. If `TestMeshReplace` only covers Hub (not Battle), the
   apply-to-all list should NOT include it (only full-coverers
   appear).  Confirm this by checking another mod that only targets
   one scene.

### B.1.6 Persistence

1. With `TestMeshReplace` applied to all scenes, close the game.
2. Relaunch.
3. First-frame log: `[variant] applied N default/persisted
   selection(s)`.
4. Hecate renders as `TestMeshReplace` in Hub without the user
   opening the UI.

## Expected

- First frame = Stock (auto-apply).
- Dropdown per scene with Stock + mod list, alphabetical-except-Stock-first.
- Picking is instant, no rebuild.
- Scene transitions preserve picks.
- Persisted picks reload on next session.
- Apply-to-all only lists mods covering every scene.

## Checklist

- [ ] First frame = Stock
- [ ] Dropdown populated with Stock + both mods
- [ ] Pick mod A → instant swap
- [ ] Pick mod B → instant swap
- [ ] Pick Stock → revert
- [ ] Scene transition preserves pick
- [ ] Relaunch: persisted pick auto-applies
- [ ] Apply-to-all cascade works
- [ ] Apply-to-all filter excludes partial-coverage mods

## If it fails

- **Default is merged-all instead of Stock** — `apply_active_variants`
  in `runtime.lua` didn't fall back to `"stock"` when the user has
  no persisted pick, OR the builder didn't emit `Hecate_Stock_V<N>_Mesh`.
  Check `plugins_data/Enderclem-CG3HBuilder/Hecate.gpk` via
  `tools/gpk_pack.py list` for the Stock entries.
- **Swap shows white mesh** — `populate_entry_textures` didn't run
  before `swap_to_variant`, or `GMD+0x44` didn't get filled.  Check
  log for `populate_entry_textures: ... populated N handle(s)`.
- **Swap hangs the game** — pool exhaustion.  Run `rom.data.dump_pool_stats()`
  via the "Pool stats" button and check index-buf is below 64 MB.
  If over, the mod set exceeds v3.9's pool budget; defer to v4 with
  larger patch.
- **Apply-to-all missing** — check `mods_covering_all_entries` in
  `mod_state.lua`.  A mod that doesn't target all of the
  character's scene entries is intentionally excluded.
