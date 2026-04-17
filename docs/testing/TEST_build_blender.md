# C.1 — Blender addon build workflow

Goal: full mod creation path through the Blender addon — import a
character, edit, export, auto-build PKG + Thunderstore ZIP, install
to r2modman, launch and verify.

---

## Precondition

- Blender 4.2+ installed.
- CG3H addon installed (File > Import/Export menu entries present) and
  game path set in addon preferences.
- r2modman installed with the Hades II profile.
- System Python with `etcpak` / `Pillow` / `lz4` on PATH (addon uses
  `shutil.which("python")` to shell out for PKG builds — Blender's
  bundled Python lacks these).

## Steps

### C.1.1 Addon installation sanity

1. In Blender: Edit > Preferences > Add-ons.  Filter "CG3H".
2. Version shows **3.9.0** (was 3.4.0 earlier — bumped in release
   housekeeping).
3. Minimum version: 4.2.  (If you're on Blender 4.1, the addon
   refuses to enable with a clear error.)
4. Disable + re-enable the addon.  No `ImportError: cannot import
   name 'cg3h_core' from partially initialized module 'cg3h'`.  The
   earlier reload-at-top-level bug is fixed (v3.9).
5. Set the game path in addon prefs.

### C.1.2 Import

1. **File > Import > Hades II Model (.gpk)**.  Pick Hecate.gpk.
2. Wait for import (~30s for a character with animations).
3. After import: armature + N mesh objects in the scene, all named
   like `Hecate_Rig:HecateBattle_MeshShape` etc.
4. Check the scene property `cg3h_original_meshes` (via Scene
   properties → Custom Properties) — it should be a comma-separated
   list of the imported mesh names.  This drives Blender's
   auto-classify (mesh_replace vs mesh_add).

### C.1.3 Edit (choose one)

- **A: Pure mesh_replace** — select a body mesh, enter Edit Mode,
  move some vertices visibly.  Do NOT add new meshes.
- **B: mesh_add** — stay in Object Mode, `Add > Mesh > Cylinder`,
  parent to armature, rename to `TestHat`.  In the CG3H sidebar
  (N key), verify per-entry checkboxes for the new mesh default to
  all-checked.
- **C: Both** — do A and B together.

### C.1.4 Export

1. Select: armature + every mesh (original + new).
2. **File > Export > Hades II Mod (CG3H)**.
3. Mod name, author, version in dialog.  Check **Install to r2modman**.
4. Click Export.  Progress in Blender's info area.
5. Verify the mod workspace was created at
   `~/Documents/CG3H_Mods/<mod-id>/` and contains: `mod.json`,
   `Hecate.glb`, `manifest.json`, optional `icon.png`, any texture
   PNGs.
6. Verify `mod.json` type field — depending on your edit path:
   - Path A → `"mesh_replace"` (single string)
   - Path B → `"mesh_add"` (single string)
   - Path C → `["mesh_add", "mesh_replace"]` (array)

### C.1.5 Build + install

1. Blender runs `tools/cg3h_build.py <mod_dir> --package` via
   system Python.  Info area shows `Built: ...` and `Installed to
   r2modman: <mod-id>`.
2. Check `AppData/Roaming/r2modmanPlus-local/HadesII/profiles/Default/
   BepInEx/plugins_data/<AuthorName-ModName>/` exists with the built
   `.pkg` + `.glb` + `mod.json`.

### C.1.6 Launch + verify

1. Launch Hades II via r2modman.
2. Mod Manager shows the mod under the right section:
   - Path A → **Body** dropdown for Hecate
   - Path B → **Accessories** checkbox
   - Path C → **Accessories** checkbox (mesh_add is dominant under
     v3.9's classifier)
3. Visually verify the mod's effect in-game (per the corresponding
   A.x sheet's steps).

## Expected

- Addon 3.9.0 enables without ImportError.
- Import populates scene with armature + meshes + `cg3h_original_meshes` prop.
- Export produces correct `mod.json` type for each edit path.
- Auto-build + install work end-to-end.
- Mod behaviour in-game matches its classification.

## Checklist

- [ ] Addon version 3.9.0
- [ ] Addon enables cleanly (no ImportError on 4.2+)
- [ ] Disable+enable: no ImportError
- [ ] Import: mesh + armature + `cg3h_original_meshes` scene prop
- [ ] Export path A → type `"mesh_replace"`
- [ ] Export path B → type `"mesh_add"`
- [ ] Export path C → type list with both
- [ ] Workspace has mod.json + GLB + manifest
- [ ] System-Python subprocess builds PKG + Thunderstore ZIP
- [ ] r2modman install directory populated
- [ ] In-game behaviour matches classification

## If it fails

- **Addon refuses to enable** — Blender version too old (< 4.2) or the
  addon zip is malformed.  Check `bl_info["blender"]` in
  `blender_addon/cg3h/__init__.py`.
- **ImportError "partially initialized"** — the fix from v3.9 didn't
  deploy.  Check that `importlib.reload(cg3h_core)` is inside an
  `if "cg3h_core" in locals():` guard, not at module top level.
- **Wrong `type` classification** — the auto-detect logic in
  `CG3H_OT_Export.execute` didn't see `cg3h_original_meshes`.
  Re-import the character to re-populate the scene prop, then
  re-export.
- **System Python not found** — Blender shell-outs via `shutil.which`.
  On Windows, ensure `python.exe` is on PATH.  Alternatively, run
  `python tools/cg3h_build.py <workspace> --package` by hand.
