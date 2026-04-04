# CG3H Manual Test Checklist

Run from the game's `Ship/` directory. Check each box when verified.

---

## 1. Single Character Export (CLI)

```bash
python C:/Users/ender/GhidraHades2/tools/gr2_to_gltf.py Melinoe --textures --animations --anim-filter Idle -o C:/Users/ender/Documents/Hades2Mods/Melinoe/Melinoe.glb
```

- [ ] Creates `Melinoe/` directory with `Melinoe.glb`, `*.dds`, `*.png`, `manifest.json`
- [ ] `manifest.json` has `textures` section with `pkg`, `format`, `width`, `height`, `mip_count`
- [ ] GLB opens in Blender with textured mesh
- [ ] GLB has at least 1 animation that plays correctly (no stretching)
- [ ] Online glTF viewer shows correct mesh + texture

---

## 2. Multi-Texture Character (CLI)

```bash
python C:/Users/ender/GhidraHades2/tools/gr2_to_gltf.py Athena --textures -o C:/Users/ender/Documents/Hades2Mods/Athena/Athena.glb
```

- [ ] Two textures found: `Athena_Color` + `AthenaWeapons_Color`
- [ ] Both DDS + PNG files saved in `Athena/`
- [ ] `manifest.json` has both textures with correct `pkg` (e.g. ScriptsBase.pkg)
- [ ] GLB shows body texture AND weapon texture on correct meshes
- [ ] Shield/spear use weapons texture, body uses body texture

---

## 3. Lua Override Texture (CLI)

```bash
python C:/Users/ender/GhidraHades2/tools/gr2_to_gltf.py EarthElemental --textures -o C:/Users/ender/Documents/Hades2Mods/EarthElemental/EarthElemental.glb
```

- [ ] Log shows "Lua GrannyTexture overrides: ['EarthElementalTyphon_Color']"
- [ ] Texture found in BiomeQ.pkg
- [ ] GLB is textured

---

## 4. Previously Missing Texture (CLI)

```bash
python C:/Users/ender/GhidraHades2/tools/gr2_to_gltf.py Artemis --textures -o C:/Users/ender/Documents/Hades2Mods/Artemis/Artemis.glb
```

- [ ] Texture `Artemis_Color` found (was missing before 0xAA fix)
- [ ] GLB is textured

---

## 5. Animation Sanitization (CLI)

```bash
python C:/Users/ender/GhidraHades2/tools/gr2_to_gltf.py Arachne --textures --animations -o C:/Users/ender/Documents/Hades2Mods/Arachne/Arachne.glb
```

- [ ] Export completes without errors
- [ ] GLB rest pose is correct (no stretching)
- [ ] Animations play without mesh deformation artifacts

---

## 6. _static Suffix Animation (CLI)

```bash
python C:/Users/ender/GhidraHades2/tools/gr2_to_gltf.py ErymanthianBoar --textures --animations --anim-filter Idle -o C:/Users/ender/Documents/Hades2Mods/ErymanthianBoar/ErymanthianBoar.glb
```

- [ ] Animation channels are built (not 0 — was broken before _static fix)
- [ ] GLB has animation visible in viewer

---

## 7. GUI Batch Export

Open GUI: `python C:/Users/ender/GhidraHades2/tools/converter_gui.py`

1. Export tab: select 5+ characters, check "Textures", click Export
- [ ] Parallel export runs (log shows "Parallel export: N workers")
- [ ] No encoding errors (cp1252 fix)
- [ ] Each character gets its own subdirectory
- [ ] Progress bar updates as characters complete
- [ ] All selected characters export successfully

2. Export with animations: select 2 characters, check "Textures" + "Animations"
- [ ] Outer workers limited (log shows fewer parallel workers)
- [ ] Animation processing uses multiple workers per character
- [ ] Completes without stalling

---

## 8. Texture Install — DDS Path (GUI)

1. Export Melinoe with textures (if not already done)
2. Edit `Melinoe/MelinoeTransform_Color.dds` in a DDS editor (change a color)
3. Install tab > "Install modified textures" > Browse to `Melinoe/`
- [ ] GUI shows manifest info ("Melinoe: 1 texture(s) - MelinoeTransform_Color")
- [ ] Click "Install textures"
- [ ] Backup created in `_backups/`
- [ ] "1 texture(s) installed!" message
- [ ] Launch game — Melinoe has modified texture

4. Restore: Install tab > select backup > Restore
- [ ] Original texture restored

---

## 9. Texture Install — PNG Path (GUI)

1. Export Melinoe with textures (if not already done)
2. Edit `Melinoe/MelinoeTransform_Color.png` in any image editor (paint something visible)
3. Install tab > Browse to `Melinoe/`
- [ ] Click "Install textures"
- [ ] Log shows "Compressing MelinoeTransform_Color.png -> DDS..."
- [ ] DDS file is regenerated from the PNG (check file timestamp)
- [ ] "1 texture(s) installed!" message
- [ ] Launch game — Melinoe has the PNG edit visible

---

## 10. Mesh Import Roundtrip

1. Export Melinoe: `gr2_to_gltf.py Melinoe --textures -o .../Melinoe/Melinoe.glb`
2. Open in Blender, make a visible edit (move vertices, delete a part)
3. Export from Blender as `Melinoe_edited.glb`
4. Import:
```bash
python C:/Users/ender/GhidraHades2/tools/gltf_to_gr2.py Melinoe Melinoe_edited.glb --allow-topology-change
```
- [ ] Import completes, produces `Melinoe_mod.gpk`
- [ ] Mesh names matched correctly (no LOD warnings, uses _1/_2 suffixes)
- [ ] Install mod GPK and launch game — edit is visible

---

## 11. Mesh Naming

Export any character with duplicate mesh names (e.g. Melinoe):
- [ ] Log shows `_1`, `_2` suffixes (NOT `_LOD1`, `_LOD2`)
- [ ] Blender import shows clean names without LOD references

---

## Results

| # | Test | Status |
|---|------|--------|
| 1 | Single export | |
| 2 | Multi-texture | |
| 3 | Lua override | |
| 4 | Missing texture fix | |
| 5 | Animation sanitization | |
| 6 | _static suffix | |
| 7 | GUI batch export | |
| 8 | DDS install | |
| 9 | PNG install | |
| 10 | Mesh roundtrip | |
| 11 | Mesh naming | |
