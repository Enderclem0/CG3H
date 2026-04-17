# C.2 — `tools/converter_gui.py` workflow

Goal: tkinter GUI end-to-end — Create a mod workspace, Build it into a
Thunderstore package, install to r2modman, verify in-game.

---

## Precondition

- System Python with `lz4`, `pygltflib`, `etcpak`/`Pillow`, `numpy`
  installed (`pip install -r requirements.txt`).
- Hades II on Steam; game path auto-discovered via registry or
  fallback paths.
- r2modman installed (optional — only needed for the Install tab).

## Steps

### C.2.1 Launch

1. `python tools/converter_gui.py`.
2. Window opens with three tabs: **Create**, **Build**, **Mods**.
3. No stack traces in terminal.
4. If the game path didn't auto-discover, the GUI reports it at the
   top — set it manually via the dropdown's **…** button.

### C.2.2 Create

1. **Create** tab.
2. Select a character from the dropdown (Hecate / Melinoe / etc.).
3. Enter a workspace name (defaults to character name).
4. Click **Export**.  Progress shown; after ~10–30s the workspace
   directory appears under `~/Documents/CG3H_Mods/<name>/` with a
   `.glb` + stock `mod.json`.
5. The GLB contains armature + all character meshes.

### C.2.3 Edit (outside GUI)

- Open the GLB in Blender, edit, save back over the same filename.
  (The GUI is a thin orchestrator; actual editing happens in Blender.)
- Optionally hand-edit `mod.json` for metadata (name, author,
  description) and `type` (defaults to `"mesh_replace"` — change to
  `"mesh_add"` / list as needed).

### C.2.4 Build

1. **Build** tab.
2. Workspace field: point at the directory from step C.2.2.
3. Mod name / author / version fields (read from mod.json initially).
4. **Build** button — runs `cg3h_build.py` as a subprocess.
5. Progress: `Built: <mod-id>` and a path to the Thunderstore ZIP
   under the workspace.
6. If **Install to r2modman** was checked, the tool copies into the
   r2modman profile AND drops the Thunderstore ZIP into the r2modman
   download cache so the mod appears in the profile's mods list.

### C.2.5 Mods tab

1. Switch to **Mods** tab.
2. List populates with every CG3H mod installed in the r2modman
   profile.
3. Click a mod: details panel shows mod.json metadata.
4. **Remove from r2modman** deletes the installed mod (keeps the
   workspace).

### C.2.6 In-game verification

1. Launch Hades II via r2modman.
2. Verify mod behaviour per the corresponding A.x sheet.

## Expected

- GUI opens without Python errors.
- Create produces a working workspace GLB.
- Build produces a Thunderstore ZIP + installs to r2modman.
- Mods tab reflects installed mods and supports removal.

## Checklist

- [ ] `python tools/converter_gui.py` opens without traceback
- [ ] Game path discovered (or manually set)
- [ ] Create → workspace + GLB produced
- [ ] Build → Thunderstore ZIP produced
- [ ] Install to r2modman copies files into profile
- [ ] Mods tab shows installed mods
- [ ] Remove button uninstalls cleanly
- [ ] In-game: mod renders correctly

## If it fails

- **Game not found** — registry lookup failed AND every fallback path
  (`cg3h_constants.py::_FALLBACK_PATHS`) doesn't match.  User must set
  it manually.  If this is common, add env-var override.
- **`etcpak`/`Pillow` missing** — check `pip install -r
  requirements.txt` actually ran in the same interpreter.  `which
  python` on Windows can be deceptive; try
  `python -m pip install ...` directly.
- **Install to r2modman puts files in wrong place** — `converter_gui.py`
  hardcodes `%APPDATA%/r2modmanPlus-local/HadesII/profiles/Default/
  ReturnOfModding`.  Users with non-Default profile or portable
  r2modman install won't match.  Document as known limitation;
  fallback path for users to install manually via drag-and-drop of
  the Thunderstore ZIP into r2modman's profile view.
