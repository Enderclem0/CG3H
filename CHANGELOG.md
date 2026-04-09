# Changelog

All notable changes to CG3H are documented here.

---

## v3.2.0

Multi-mod robustness: name deduplication, animation merge, and conflict detection.

### Added

- **Name deduplication** — when two `mesh_add` mods use the same mesh name, both are kept with `{mod_id}_` prefix (e.g. "ModA_Crown" and "ModB_Crown"). Transparent to mod creators.
- **Animation merge** — `_merge_glbs()` copies animations from all mods with name-based node remapping. Previously only the first mod's animations survived.
- **Animation dedup** — same animation name in multiple mods: last mod wins with warning printed.
- **Mesh name conflict warnings** — `check_conflicts()` warns about shared mesh names across `mesh_add` mods (informational, not blocking).
- **`_copy_accessor()` helper** — shared function for copying binary accessor data between GLBs, used by both mesh and animation merge.
- **Ruff linter** — `pyproject.toml` with ruff config for code quality. Zero warnings on all core files.

### Changed

- `_merge_glbs()` returns `(merged_path, collisions)` instead of just `merged_path`. Collisions set propagated to manifest merge and routing.
- `_merge_manifests()` accepts `collisions` set instead of flat rename map, derives prefixed names per-mod.
- `patch_animation_entries()` filter is now optional (was required). Builder no longer passes filter — GLB contents are sole authority on what gets patched. CLI `--anim-patch-filter` still works as optional pre-filter.
- Routing keys updated per-mod when collisions exist (not a flat rename after the fact).

### Removed

- Import-side animation filter in builder — was redundant with export-side filter.
- Unused variables caught by ruff: `needs_reserialize`, `dim`, `first_id`, `idx_path`.
- Unused import: `STEAM_PATHS` from `cg3h_constants`.

### Known Limitations

- Name deduplication covers meshes only (not textures/images in PKGs — planned for future).
- New mesh bone bindings limited to template mesh's bindings (v4.0).

---

## v3.1.0

Multi-entry character support and improved game path detection.

### Added

- **Multi-entry GPK export** — all mesh entries exported by default (e.g. Hecate: Battle + Hub)
- **Skeleton merge** — bones from all entries merged into unified skeleton for the GLB
- **Multi-entry import** — manifest routes GLB meshes to correct GPK entries, each serialized independently
- **Per-entry mesh routing** — `new_mesh_routing` in mod.json targets new meshes to specific entries (e.g. battle-only accessories)
- **Smart template selection** — new meshes pick the existing mesh with best bone overlap as template
- **Manifest merging** — multi-mod builds merge manifests from all mods
- **Steam auto-detection** — game path found via Windows registry + `libraryfolders.vdf` (supports custom Steam libraries)
- **Stale texture cache fallback** — if texture index misses entries, full .pkg scan with auto-cleanup
- **Blender addon CG3H panel** — sidebar panel for assigning meshes to entries with checkboxes
- **Blender addon auto-build** — export runs `cg3h_build.py` via system Python to produce PKG + Thunderstore ZIP
- **Clear error on missing deps** — `texture2ddecoder` / `Pillow` missing now shows explicit error instead of silent failure
- **requirements.txt** for easy dependency install
- **Cross-platform guards** — `ctypes.windll` wrapped in try/except for non-Windows import compatibility

### Changed

- Export defaults to all `_Mesh` entries instead of body entry only
- GUI Create tab shows entry checkboxes for multi-entry characters
- GUI mod name/author moved to Build tab with persistent author
- `_build_entry_routing()` replaces single-entry body selection
- `load_gr2()` refactored into `load_sdb()` + `load_gr2_entry()` for SDB reuse
- `os.path.abspath` for gpk_dir normalization (fixes mixed slash paths)

### Known Limitations

- New mesh bone bindings limited to template mesh's bindings (v4.0)

---

## v3.0.0

Shared runtime builder, Blender addon, and r2modman integration.

**This is a major workflow shift.** Mods are data-only packages (GLB + textures). The shared CG3HBuilder plugin handles all runtime logic — building GPKs, merging mods, loading textures. Zero game files modified.

### Added

- **CG3HBuilder Thunderstore plugin** — shared runtime dependency for all CG3H mods:
  - Scans `plugins_data/` for installed CG3H mods at game launch
  - Merges multiple mods targeting the same character into one GLB
  - Builds GPKs from GLBs + player's local game files (no Python needed)
  - Registers GPKs via `rom.data.add_granny_file` (no restart needed)
  - Caches built GPKs, rebuilds when mods change, cleans up on mod removal
  - Loads custom textures via biome overrides + LoadPackages
- **Data-only mod packages** — mods contain only assets (GLB, PKG, mod.json)
  - Minimal `plugins/` stub (H2M manifest with CG3HBuilder dependency)
  - No executable or Lua code per mod
- **Auto texture detection** — textures embedded in GLBs are automatically found at build time, extracted, and packaged (only new mesh textures, not original character textures)
- **Blender addon** — self-contained (bundled exporter/importer/builder exes):
  - Import: File > Import > Hades II Model (.gpk) with textures and animations
  - Export: File > Export > Hades II Mod (CG3H) — creates workspace + Thunderstore ZIP
  - Author saved in addon preferences
  - Character auto-detected from selected objects on export
- **GUI overhaul**:
  - Create tab: searchable character list, export to workspace
  - Build tab: mod name + author fields, Thunderstore ZIP, one-click r2modman install
  - Mods tab: CG3HBuilder status, GPK build state, installed mod list
  - Author name persisted across sessions
- **r2modman integration** — install registers mods in `mods.yml`, handles nested directory layout, cache icons
- **Conflict detection** — per-operation analysis:
  - Same texture replaced by multiple mods = conflict
  - Multiple mesh_replace for same character = conflict
  - mesh_add + mesh_add = compatible (merged)
- **Smart data stripping** — Thunderstore ZIPs strip unchanged meshes/textures vs manifest baseline
- **GitHub Actions** — tag `v*` builds CG3HBuilder ZIP, Blender addon ZIP, and tools ZIP
- **Shared constants** — `cg3h_constants.py` for Steam paths and dependency versions
- **PyInstaller specs** — for exporter, importer, and builder executables
- **Name collision warnings** — merger logs when a mesh from a second mod is skipped due to duplicate name
- **All biome texture loading** — custom textures register for all 16 biomes

### Changed

- GUI rewritten from technical tabs to workflow tabs (Create/Build/Mods)
- Mods no longer bundle `cg3h_builder.exe` or `main.lua` — all runtime logic centralized
- Build pipeline auto-detects textures from GLB instead of requiring manual listing

### Removed

- Per-mod `main.lua` generation
- Per-mod `cg3h_builder.exe` bundling
- Backup/restore system (H2M mods are non-destructive)
- Checksum management (standalone packages bypass validation)
- Per-mod `main.lua` generation
- Per-mod `cg3h_builder.exe` bundling
- `mesh_patch` type — folded into `mesh_replace` (true diff patching planned for v3.3)
- `mod_merger.py` merge functions — replaced by CG3HBuilder runtime
- Direct game file modification workflow

### Known Limitations

- **Requires H2M patches** — `rom.data.add_granny_file` API and GPK exact-match fix (PRs pending)

---

## v2.1.0

Custom materials and new texture support for added meshes.

### Added

- **New material support** -- new meshes added in Blender can have their own custom textures
- Material chain creation in DLL memory (granny_material + granny_material_map + granny_texture)
- `FromFileName` hash lookup verified in-game: game finds custom textures via `"GR2/" + basename`
- **Add texture entries to .pkg** -- custom textures inserted as new 0xAD entries (no existing textures replaced)
- Custom texture auto-resize to 512x512 (prevents oversized chunks)
- GLB material/texture extraction: `parse_glb` reads material_name, texture_name per mesh
- `extract_glb_textures` returns embedded PNG bytes by image name
- Unmatched GLB meshes (added in Blender) auto-created as new meshes in the first entry
- Outline/shadow template selection: new meshes named with "Outline" or "ShadowMesh" inherit the correct template material (Mat_Outline / shadow shader)
- `fi->Materials` and `fi->Textures` arrays expanded for new material structs

### Changed

- Custom texture target .pkg chosen from manifest (same pkg as character's existing textures) instead of hardcoded Fx.pkg
- `_create_new_mesh` accepts `material_ptr` parameter to override template MaterialBindings
- Outline/shadow meshes skip custom material creation (use template's shader material)

### Known Issues

- Custom outline meshes need manual vertex offset (~0.6 units via Alt+S in Blender) to produce correct silhouette effect. Game's outline meshes are pre-authored with different topology, not simple duplicates.
- Custom texture image in Blender should be named meaningfully (not "Image_0") as the name becomes the .pkg entry key

---

## v2.0.0

Full end-to-end texture pipeline, multi-entry GPK support, unified install workflow, and animation fixes.

**Breaking changes:** Export directory structure changed (per-character folders with manifest). Old exports without `manifest.json` are not compatible with the new Install tab. Re-export your characters to use the new pipeline.

### Added

- **Texture import pipeline** -- edit textures in Blender, Photoshop/GIMP (PNG), or paint.net (DDS) and install them back into the game
- **PNG -> DDS compression** with mipmaps via `etcpak` (BC7/BC3/BC1 output matching original format)
- **DDS auto-truncation** for extra mip levels (paint.net compatibility)
- **Multi-PKG texture replacement** -- textures duplicated across biome packages (BiomeF, BiomeHub, BiomeIHouse, etc.) are ALL replaced
- **XXH64 checksum validation** -- game checks `checksums.txt`; tool updates it after every replacement and restores on uninstall
- **Lua GrannyTexture override parsing** -- 48 entities override their texture at runtime via game `Scripts/`; exporter respects these
- **Variant textures** (e.g. HecateEM_Color for Extreme Measures) exported as standalone files, marked in manifest
- **Texture index** (`_texture_index.json`) for fast batch lookups, auto-built on first use
- **Export manifest** (`manifest.json`) per character -- tracks mesh entries, per-mesh GR2 index mapping, texture metadata (pkg source, format, dimensions, mip count, png_hash), variant flags, and full pkgs list
- **Multi-entry GPK support** -- characters with multiple mesh entries (e.g. Hecate: HecateBattle_Mesh + HecateHub_Mesh) export ALL entries by default
- `--mesh-entry` flag to filter specific entries, `--list-entries` to inspect
- Importer routes GLB meshes to correct GR2 entry via manifest
- Each entry patched and serialized separately, packed into single output GPK
- **Unified Install tab** in GUI -- browse to export folder, reads manifest, imports mesh + installs textures in one click
- **Mod registry** (`_mods.json`) -- tracks all changes per character (mesh + texture PKGs + checksums)
- **One-click uninstall** -- restores ALL files (GPK + PKGs + checksums) at once; multi-mod safe
- **GLB texture extraction** with hash-based change detection (png_hash comparison)
- **Animation gap-fill** -- if a bone has any animated channel, emit all three (pos + rot + scale) with rest pose constants; prevents Blender bogus scale on partially-animated bones
- **_static suffix stripping** for animation track-to-bone matching
- **bone_index field** preserved in animation track data
- **Parallel animation extraction** with `multiprocessing.Pool` and `--anim-workers` flag
- **25 unit tests** (`tests/test_core.py`)
- **Manual test checklist** (`tests/TEST_CHECKLIST.md`)
- New dependencies: `etcpak` (BC7 compression), `xxhash` (checksum validation)
- Per-character export directories: `Character/Character.glb` + DDS + PNG + manifest

### Changed

- GUI restructured from 3 tabs (Export/Import/Install) to 2 tabs (Export/Install); Install tab now handles both mesh import and texture installation
- Install runs in background thread (no UI freeze)
- GUI auto-scales outer/inner parallelism based on batch size for animation export
- Non-blocking subprocess stdout via reader threads (fixes UI stalling during batch export)
- Duplicate mesh names now get `_2`, `_3` suffixes (not `_LOD1`, `_LOD2`) -- same-name meshes are split parts, not LODs
- Importer strips both new `_2`/`_3` and legacy `_LOD` suffixes
- "Export all LODs" checkbox removed (all parts exported by default)
- Standalone PNG files saved alongside DDS for Photoshop/GIMP editing path
- Multi-texture per character: each mesh gets its own texture via material chain walking

### Fixed

- **PKG 0xAA (Texture3D) parsing** -- size field is big-endian (was misread as little-endian, causing scanner to skip rest of chunk); ALL character textures now found
- **Animation scale channels** -- skip if 3x3 matrix has significant off-diagonal (shear/rotation, not pure scale); skip NaN/Inf/extreme values (>100)
- **Animation rotation channels** -- normalize quaternions to unit length (non-unit causes mesh stretching)
- **Animation translation channels** -- skip NaN/Inf and extreme values (>1000)
- **UTF-8 encoding** on all subprocess stdout (fixes cp1252 crash on Windows)

### Known Issues

- `_static` suffix on animation tracks: mechanism not fully understood
- ErymanthianBoar spine bones have no animation tracks in Idle (may be correct for that clip)
- Some characters have textures referenced in GR2 but not shipped in any `.pkg` (e.g. ClockworkGear's gear_diffuse)

---

## v1.2.0

### Added

- Add new meshes to characters (accessories, armor, custom geometry) via `--allow-topology-change`
- New meshes added to Model MeshBindings so the game renders them
- Body mesh used as template for new meshes (not outline mesh)

### Fixed

- Bone index remapping for new meshes

---

## v1.1.0

### Added

- Animation export (`--animations` flag) with curve decoding and post-processing
- Animation import (`--patch-animations` flag) with in-place DaK8uC8u/DaK16uC16u curve patching
- `--anim-patch-filter` to prevent mass animation re-encoding
- Animation patch filter field in GUI import tab
- Progress output during animation export

### Fixed

- Flush animation progress output for GUI visibility

---

## v1.0.0

Initial release.

### Added

- GPK format parsing (Format B, no `uncompressed_size` field)
- `granny_types.py` -- dynamic Granny struct offset resolver
- `gr2_to_gltf.py` -- export any character to `.glb` (skinned + rigid meshes)
- `gltf_to_gr2.py` -- import edited `.glb` back to `.gpk` (golden path serialization)
- `gpk_pack.py` -- GPK archive pack/unpack
- `converter_gui.py` -- GUI with Export, Import, and Install tabs
- Blender addon (`blender_addon/cg3h/`) for File > Import/Export
- 40-byte physical GPU stride override (engine invariant)
- Index extraction via `GrannyCopyMeshIndices` (handles 16-bit and 32-bit)
- Bone weight remapping from Blender joint order to GR2 BoneBinding order
- Index buffer patching (triangle/face edits)
- Variable vertex count support (`--allow-topology-change`)
- Blender `.001` duplicate suffix stripping
- uint16 vertex count limit validation (65,535 max)
- Backup and restore of original files before installing
- Bounding box (OBB) recomputation for frustum culling
