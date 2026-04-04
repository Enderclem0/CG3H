# Changelog

All notable changes to CG3H are documented here.

---

## v1.3.0

Full end-to-end texture pipeline, multi-entry GPK support, unified install workflow, and animation fixes.

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
