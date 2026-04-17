# A.4 — `texture_replace` mod type

Goal: replace a stock character texture via a standalone PKG without
touching geometry.  Loaded via biome overrides at scene-transition time.

---

## Precondition

- Clean r2modman profile.
- `python` + `etcpak` / `Pillow` available (for PKG build).

## Recipe

1. Pick a stock texture to replace — e.g. Hecate's body texture
   `HecateBodyTexture` (exact name varies; find via
   `tools/gr2_to_gltf.py` which dumps a texture index, or browse
   Melinoe/Hecate in Blender via the CG3H addon and note the
   `_texture_index.json` entries).
2. Prepare a 2048x2048 PNG with the replacement image.
3. Create a mod workspace dir with:
   ```
   mod.json
     {
       "format": "cg3h-mod/1.0",
       "metadata": { "name": "TestTextureReplace", "author": "TestAuthor", "version": "1.0.0", "description": "..." },
       "type": "texture_replace",
       "target": { "character": "Hecate" },
       "assets": {
         "textures": [
           { "name": "HecateBodyTexture", "file": "HecateBodyTexture.png",
             "custom": false, "width": 2048, "height": 2048 }
         ]
       }
     }
   ```
4. Put the PNG next to `mod.json` with matching `file:` name.
5. Build the standalone PKG via `tools/cg3h_build.py <mod_dir> --package`
   (subprocess invoked by the Blender addon, or run by hand).
6. Verify a `.pkg` of the expected stem was produced next to the
   workspace.  Install via r2modman (drag the built Thunderstore ZIP
   into the profile).

## Steps

1. Launch the game.
2. Log check: `[CG3H]   Textures: Enderclem-CG3HBuilder-TestAuthor-TestTextureReplace.pkg`
   (the PKG loaded via biome overrides + LoadPackages).
3. Go to a scene where the targeted character renders (for
   HecateBodyTexture — go to Hecate's lounge in Hub, or any scene with
   her).
4. Transition between scenes (leave and return) — this is required
   because `load_package_overrides_set` only triggers
   `ReadTexture2D` on scene transition.
5. The replaced texture should render on the character.

## Expected

- Custom texture visible after first scene transition.
- `LogOutput.log` shows `[CG3H]   Textures: ... (TestTextureReplace)`
  at plugin init.
- No errors about missing textures.

## Checklist

- [ ] PKG built and installed via r2modman
- [ ] `[CG3H]   Textures:` log line at init
- [ ] Texture renders after a scene transition
- [ ] No `ReadTexture2D` errors

## If it fails

- **PKG didn't build** — `tools/pkg_texture.py` couldn't encode the
  texture.  Check that `etcpak` is installed in system Python
  (not Blender's bundled Python — Blender's env lacks it, which is
  why the Blender addon uses `shutil.which("python")` to shell out).
- **Texture doesn't replace** — two issues possible:
  1. Texture name mismatch.  Use `tools/pkg_texture.py list` on the
     stock PKG to confirm the exact name.
  2. Scene didn't transition.  Per `MEMORY.md` H2M Texture Loading
     notes, `ReadTexture2D` only runs on transition, not game load.
- **Wrong character's textures replaced** — name collisions between
  characters.  `HecateBodyTexture` is unique; `BodyTexture` is not.
  Always use the full stock-named texture.
