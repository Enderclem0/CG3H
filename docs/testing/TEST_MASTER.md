# CG3H v3.9.0 Release Test Matrix

Manual pre-release checks.  Each row links to a dedicated sheet with
preconditions, steps, expected outcomes, and debug hints.  **Check the
box in the `Pass` column after running a sheet successfully.**

Intended workflow: work down the matrix top-to-bottom.  Upper rows
establish baseline state the lower rows depend on.

---

## How to run

1. Start from a clean r2modman profile with **only** `Hell2Modding` +
   `CG3HBuilder` (this branch's build) installed.  No CG3H mesh mods yet.
2. For each row that needs test mods, create them per the recipe in
   the linked sheet (or point the sheet at an existing test-mod ZIP).
3. Launch the game via r2modman.  Follow the sheet's steps.  Record
   result here.
4. After all rows pass, tag `v3.9.0` on both repos.

---

## Matrix

### A. Mod creation (one sheet per type)

| #   | Sheet                                                                        | Focus                                  | Pass |
|---- | ---------------------------------------------------------------------------- | -------------------------------------- | ---- |
| A.1 | [`TEST_mod_type_mesh_add.md`](TEST_mod_type_mesh_add.md)                     | `mesh_add` — always-visible accessory  | [ ]  |
| A.2 | [`TEST_mod_type_mesh_replace.md`](TEST_mod_type_mesh_replace.md)             | pure `mesh_replace` — picker variant   | [ ]  |
| A.3 | [`TEST_mod_type_mesh_patch.md`](TEST_mod_type_mesh_patch.md)                 | `mesh_patch` — vertex position edits   | [ ]  |
| A.4 | [`TEST_mod_type_texture_replace.md`](TEST_mod_type_texture_replace.md)       | `texture_replace` — standalone PKG     | [ ]  |
| A.5 | [`TEST_mod_type_animation_patch.md`](TEST_mod_type_animation_patch.md)       | `animation_patch` — curve tweak        | [ ]  |

### B. v3.9 picker behaviour

| #   | Sheet                                                      | Focus                                         | Pass |
|---- | ---------------------------------------------------------- | --------------------------------------------- | ---- |
| B.1 | [`TEST_picker_body.md`](TEST_picker_body.md)               | Body dropdown, 2×mesh_replace, apply-to-all   | [ ]  |
| B.2 | [`TEST_picker_accessory.md`](TEST_picker_accessory.md)     | Accessories checkboxes, toggle timing         | [ ]  |

### C. Build workflows

| #   | Sheet                                              | Focus                                     | Pass |
|---- | -------------------------------------------------- | ----------------------------------------- | ---- |
| C.1 | [`TEST_build_blender.md`](TEST_build_blender.md)   | Blender addon end-to-end                  | [ ]  |
| C.2 | [`TEST_build_gui.md`](TEST_build_gui.md)           | `tools/converter_gui.py` end-to-end       | [ ]  |

### D. Regression

| #   | Sheet                                                  | Focus                                       | Pass |
|---- | ------------------------------------------------------ | ------------------------------------------- | ---- |
| D.1 | [`TEST_regression_v38.md`](TEST_regression_v38.md)     | v3.8 draw-gate + enable/disable still work  | [ ]  |

---

## Acceptance criteria

A row **passes** when every checkbox in the sheet is ticked AND no
`[ERR]` lines appeared in `LogOutput.log` / `Hades II.log` that weren't
there on a clean-stock run.

Known-benign log entries (expected, don't fail the test):
- `Granny3D.cpp:1117 ERR String database remap failed for <entry>` —
  harmless for convert()-produced entries using inline strings.
- `Granny3D.cpp:77 ERR [Granny3D][FileReading] File has run-time type
  tag of 0x0 ... Automatic conversion will be attempted.` — same.
- `Granny3D.cpp:248 ERR Could not open Granny Packfile` for the
  initial first-attempt path before H2M's redirect fires — benign.

Anything else new is a genuine regression.

---

## Log locations

- `C:/Users/<you>/Saved Games/Hades II/Hades II.log` — game engine log
- `C:/Users/<you>/AppData/Roaming/r2modmanPlus-local/HadesII/profiles/Default/ReturnOfModding/LogOutput.log` — H2M + CG3H plugin log

Grep patterns that are useful across sheets:
```
grep -E "(CG3H|sanity_check|RequestBufferUpdate failed|Ran out of static|mesh-gate|\[variant\]|populate_entry_textures|swap_to_variant|restore_stock)" LogOutput.log
```

---

## After all rows pass

1. Update "Pass" column checkboxes in this file.
2. `git commit -am "v3.9.0 test matrix signed off"`.
3. Tag both repos (`CG3H: v3.9.0`, `Hell2Modding: cg3h/v3.9.0`).
4. Publish Thunderstore build from `.github/thunderstore/`.
