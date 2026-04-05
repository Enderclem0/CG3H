# Changelog

All notable changes to CG3H are documented here.

---

## v3.0.0

Hell2Modding integration, non-destructive mod distribution, and complete GUI rewrite.

**This is a major workflow shift.** CG3H is now a mod builder that produces H2M-compatible packages. Zero game files are modified — H2M loads standalone assets at runtime.

### Added

- **CG3H Mod Builder GUI** — complete rewrite with 3 workflow tabs:
  - **Create**: pick character, export to mod workspace, auto-generate mod.json
  - **Build**: build GPK + PKG for H2M, optional Thunderstore ZIP, one-click r2modman install
  - **Mods**: see installed mods, conflicts, merge order, disable/remove/rebuild
- **`cg3h build` command** — reads `mod.json` + assets, builds H2M folder structure
  - `--package` flag creates Thunderstore-ready ZIP
  - `--check-conflicts` for dry-run conflict detection
- **Standalone .pkg builder** — creates `.pkg` from scratch with custom textures
  - H2M's `LoadPackages` API loads them at runtime
  - No `.pkg_manifest` needed for 3D textures
- **mod.json specification** — 5 mod types:
  - `texture_replace`: custom PNG → standalone .pkg (CC-free)
  - `mesh_add`: append new meshes (CC-free, originals stripped)
  - `mesh_replace`: swap character meshes
  - `mesh_patch`: edit vertices in-place
  - `animation_patch`: edit animations with filter support
- **Operation-based system** — mods infer operations from assets, support multiple types
- **Multi-mod merger** — scans mods, groups by character, builds merged GPK + PKG
  - Sequential merge: each mod applied to previous output
  - Merged PKG combines all custom textures
- **Mod priority system** — `cg3h_mod_priority.json` controls merge order
  - Auto-generated, editable via GUI or by hand
  - Higher index = applied later = wins conflicts
- **Conflict detection** — per-operation analysis:
  - Same texture replaced by multiple mods = conflict
  - Multiple mesh_replace for same character = conflict
  - mesh_add + mesh_add = compatible (merged)
  - Different animation filters = compatible
- **Smart data stripping** — Thunderstore ZIPs only contain changed data:
  - Meshes: compared by vertex/index count against manifest
  - Textures: compared by PNG hash against manifest
  - Animations: compared by content hash against manifest
  - Unchanged assets stripped from distribution
- **PyInstaller exe** (`cg3h_builder.exe`, 29MB) — no Python needed for end users
  - Included in Thunderstore ZIP for mesh mods
  - Lua companion auto-runs on first launch to build GPK
- **H2M Lua companion** — auto-generated `main.lua`:
  - `rom.game.LoadPackages` for custom .pkg loading
  - `rom.on_import.post` for deferred initialization
  - Auto-build GPK on first launch if missing
- **Thunderstore packaging** — ZIP ready for upload:
  - mod.json + stripped GLB + PNG (CC-free for mesh_add + texture_replace)
  - Standalone .pkg + H2M manifest + Lua companion
  - `cg3h_builder.exe` for mesh mods
  - `conflicts.json` describing what the mod touches
- **GitHub Actions** — tag `v*` triggers automated release build
- **Blender addon v3.0** — textures, animations, topology change, Build for H2M menu

### Changed

- GUI rewritten from 4 technical tabs to 3 workflow tabs (Create/Build/Mods)
- Window title: "CG3H Mod Builder"
- Default output directory: `Documents/CG3H_Mods/`
- Blender addon version bumped to 3.0.0

### Removed

- Legacy Import tab (merged into Build)
- Legacy Install tab (replaced by Mods)
- Backup/restore system (H2M mods are non-destructive)
- Checksum management (standalone packages bypass validation)
- Mod registry (_mods.json) — replaced by r2modman scanning
- Direct game file modification is no longer the primary workflow

### Known Issues

- `mesh_replace` and `mesh_patch` types still require distributing copyrighted geometry; v3.1 will add a diff format for CC-free distribution
- `cg3h_builder.exe` is 29MB due to PyInstaller bundling; may be reduced in future versions

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
