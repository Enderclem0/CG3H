# CG3H v3.0.0 Release Test Plan

Test every user path before tagging. Each test should be run from scratch.
Mark with [PASS], [FAIL], or [SKIP] + notes.

---

## Prerequisites

- [ ] Hell2Modding installed via r2modman
- [ ] Game launches through r2modman without mods
- [ ] Python 3.14 + all deps installed (numpy, pygltflib, lz4, etcpak, Pillow, xxhash)
- [ ] Blender 4.x installed

---

## 1. GUI Launch & Settings

- [ ] `python tools/converter_gui.py` opens without errors
- [ ] Window title is "CG3H Mod Builder"
- [ ] 3 tabs visible: Create, Build, Mods
- [ ] Game path auto-detected from Steam
- [ ] Character dropdown populates with game characters

---

## 2. Create Tab — Export Character

### 2a. Basic export (mesh + textures)
- [ ] Select a character (e.g. Melinoe)
- [ ] Set mod name and author
- [ ] Textures checked, animations unchecked
- [ ] Click "Create Mod Workspace"
- [ ] Output folder created with: Character.glb, *.png, *.dds, manifest.json, mod.json
- [ ] GLB opens in Blender with textured mesh

### 2b. Export with animations
- [ ] Select a character, check animations
- [ ] Set animation filter (e.g. "Idle")
- [ ] Create workspace
- [ ] GLB has animation(s) visible in Blender
- [ ] manifest.json contains animation hashes

### 2c. Multi-entry character (e.g. Hecate)
- [ ] Select Hecate
- [ ] Create workspace
- [ ] GLB contains both HecateBattle and HecateHub meshes
- [ ] manifest.json has mesh_entries list with both entries
- [ ] Both outfits visible in Blender

### 2d. Export with mesh entry filter
- [ ] Set mesh entries field to "HecateHub_Mesh"
- [ ] Create workspace
- [ ] GLB only contains Hub meshes, not Battle

---

## 3. Blender Editing

### 3a. Edit texture in Blender
- [ ] Open exported GLB in Blender
- [ ] Switch to Texture Paint mode
- [ ] Paint something visible on the character
- [ ] Export GLB (overwrite the workspace GLB)

### 3b. Add new mesh in Blender
- [ ] Open exported GLB
- [ ] Add a primitive (cube/sphere)
- [ ] Parent to armature (Ctrl+P > Armature Deform with Automatic Weights)
- [ ] Create new material, assign a new texture image (name it meaningfully)
- [ ] Export GLB

### 3c. Edit existing mesh vertices
- [ ] Open exported GLB
- [ ] Select a body mesh, enter Edit mode
- [ ] Move some vertices visibly
- [ ] Export GLB

### 3d. Add outline + shadow meshes
- [ ] Add new mesh named "MyMeshOutline_MeshShape"
- [ ] Add new mesh named "MyMeshShadowMesh_MeshShape"
- [ ] Parent both to armature
- [ ] Export GLB

---

## 4. Build Tab — Build for H2M

### 4a. Texture-only mod
- [ ] In Blender, only edit the texture (no mesh changes)
- [ ] Export GLB back to workspace
- [ ] Open Build tab, browse to workspace
- [ ] Mod info shows correctly
- [ ] Check "Install to r2modman"
- [ ] Click Build
- [ ] Build succeeds, log shows .pkg created
- [ ] Files appear in r2modman plugins_data + plugins
- [ ] Launch game through r2modman — texture change visible

### 4b. Mesh add mod
- [ ] Use workspace from test 3b (new mesh added)
- [ ] Build with r2modman install
- [ ] Launch game — new mesh visible on character
- [ ] Custom texture renders on the new mesh

### 4c. Mesh edit mod (topology change)
- [ ] Use workspace from test 3c (vertices moved)
- [ ] Build — should detect topology change
- [ ] Launch game — vertex edits visible

### 4d. Animation mod
- [ ] Edit an animation in Blender (modify keyframes)
- [ ] Export GLB
- [ ] mod.json has animation assets configured
- [ ] Build succeeds
- [ ] Launch game — animation change visible

### 4e. Combined mod (mesh + texture + animation)
- [ ] Edit mesh, texture, AND animation in same workspace
- [ ] Build succeeds
- [ ] All three changes visible in game

### 4f. Thunderstore ZIP
- [ ] Check "Also create Thunderstore ZIP"
- [ ] Build succeeds
- [ ] ZIP created in workspace
- [ ] ZIP contains: mod.json, stripped GLB, .pkg, plugins/, manifest.json, README.md
- [ ] ZIP does NOT contain .gpk (built on user's machine)
- [ ] ZIP does NOT contain unchanged meshes/textures/animations

---

## 5. Mods Tab — Mod Management

### 5a. View installed mods
- [ ] After installing a mod via Build tab, switch to Mods tab
- [ ] Click Refresh
- [ ] Installed mod appears in list with correct info

### 5b. Disable a mod
- [ ] Select mod, click Disable
- [ ] Mod shows as disabled in list
- [ ] Launch game — mod effect is gone

### 5c. Re-enable (rebuild)
- [ ] Select disabled mod, click Rebuild
- [ ] Switches to Build tab with workspace loaded
- [ ] Build and install
- [ ] Mod active again in game

### 5d. Remove a mod
- [ ] Select mod, click Remove
- [ ] Mod removed from r2modman
- [ ] Workspace folder still exists (not deleted)

### 5e. Edit a mod
- [ ] Select mod, click Edit
- [ ] Opens workspace folder in file explorer
- [ ] Make changes in Blender, re-export
- [ ] Come back to Build tab, rebuild

---

## 6. Multi-Mod Scenarios

### 6a. Two texture mods for same character
- [ ] Install mod A (red Melinoe skin)
- [ ] Install mod B (blue Melinoe skin)
- [ ] Both appear in Mods tab
- [ ] Conflict shown: both replace Melinoe_Color512
- [ ] Game shows one of them (last in priority wins)

### 6b. Two mesh_add mods for same character
- [ ] Install mod A (adds crown to Melinoe)
- [ ] Install mod B (adds cape to Melinoe)
- [ ] Mods tab shows both, no hard conflict
- [ ] Click "Rebuild Merged"
- [ ] Merged GPK created in CG3H-Merged-Melinoe/
- [ ] Game shows BOTH crown and cape

### 6c. Mod priority reordering
- [ ] In Mods tab, change merge order (move mod up/down)
- [ ] Rebuild merged
- [ ] Verify priority change takes effect (for conflicting texture, other mod wins)

### 6d. Texture mod + mesh mod (independent)
- [ ] Install texture mod for Melinoe
- [ ] Install mesh_add mod for Moros
- [ ] No conflict (different characters)
- [ ] Both work in game simultaneously

### 6e. Mesh mod + animation mod (same character)
- [ ] Install mesh_add + animation_patch for same character
- [ ] Merger handles both
- [ ] Both mesh and animation changes visible in game

---

## 7. CLI Path

### 7a. cg3h_build.py from command line
- [ ] `python tools/cg3h_build.py <mod_dir>` builds successfully
- [ ] `python tools/cg3h_build.py <mod_dir> --package` creates ZIP
- [ ] `python tools/cg3h_build.py <mod_dir> --check-conflicts` reports conflicts

### 7b. mod_merger.py from command line
- [ ] `python tools/mod_merger.py <r2_dir>` scans and merges
- [ ] Creates cg3h_mod_priority.json
- [ ] Merged output in CG3H-Merged-{Character}/

### 7c. cg3h_builder.exe (standalone)
- [ ] `cg3h_builder.exe <mod_dir>` builds without Python installed
- [ ] Same output as the Python version

---

## 8. Blender Addon

### 8a. Import character
- [ ] File > Import > Hades II Model (.gpk)
- [ ] Select a .gpk from game directory
- [ ] Character imports with armature + meshes + textures

### 8b. Export character
- [ ] Edit mesh in Blender
- [ ] File > Export > Hades II Model (.gpk)
- [ ] Select character, allow topology change
- [ ] .gpk file created

### 8c. Build for H2M from Blender
- [ ] CG3H menu > Build for H2M
- [ ] Set mod directory, name, author
- [ ] Build succeeds from within Blender

---

## 9. Edge Cases

### 9a. Character with no texture in game (e.g. ClockworkGear)
- [ ] Export — no texture embedded, warning shown
- [ ] Build still succeeds (mesh-only mod)

### 9b. Very large character (many animations)
- [ ] Export Melinoe with all animations
- [ ] Parallel animation extraction works
- [ ] Build doesn't hang or crash

### 9c. Character with Lua texture overrides (e.g. EarthElemental)
- [ ] Export finds the Lua-overridden texture
- [ ] Texture visible in GLB

### 9d. Empty mod (no changes made)
- [ ] Export character, make NO edits
- [ ] Build — smart stripping removes everything
- [ ] No empty .pkg or .gpk created

### 9e. Custom texture larger than 512x512
- [ ] Add a 2048x2048 texture in Blender
- [ ] Build — auto-resized to 512x512
- [ ] No crash, texture looks correct

### 9f. Multiple mesh entries (Hecate Hub + Battle)
- [ ] Export both entries
- [ ] Edit only Hub meshes
- [ ] Build — only Hub changes included
- [ ] Both outfits work in game (Hub modded, Battle original)

---

## 10. Clean Install Simulation

### 10a. Fresh user (no Python)
- [ ] Remove all CG3H mods from r2modman
- [ ] Place a Thunderstore ZIP in r2modman
- [ ] cg3h_builder.exe runs on first launch
- [ ] GPK built from user's game files
- [ ] Mod works on second launch

### 10b. Game update
- [ ] Simulate: delete built GPK from plugins_data
- [ ] Next launch: Lua detects missing GPK, runs builder
- [ ] Mod works again

---

## Results Summary

| Category | Pass | Fail | Skip |
|----------|------|------|------|
| GUI Launch | | | |
| Create | | | |
| Blender Editing | | | |
| Build | | | |
| Mods Tab | | | |
| Multi-Mod | | | |
| CLI | | | |
| Blender Addon | | | |
| Edge Cases | | | |
| Clean Install | | | |
| **Total** | | | |
