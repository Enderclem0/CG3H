# v2.1.0 — New Materials Support

## Overview

Allow modders to assign custom textures to new meshes they add in Blender.
Currently, new meshes inherit the template mesh's material (always the body texture).

## User Workflow

1. Export character (e.g. Melinoe) → GLB with existing textures
2. In Blender: add new mesh (e.g. cape), create new material, assign custom texture image
3. Export GLB
4. Install: tool detects new mesh has a custom texture →
   - Creates GR2 material chain in DLL memory
   - Compresses custom texture to DDS
   - Installs texture into .pkg (replace existing entry or add new)
   - Serializes GR2 with new material references

## Phases

### Phase 1: GLB Material/Texture Extraction

**Task 1.1**: Extend `parse_glb()` in `gltf_to_gr2.py`
- For each mesh, read its material index and the associated `baseColorTexture` image
- Extract image name (= texture base name)
- Add to mesh dict: `material_name`, `texture_name`, `texture_image_index`

**Task 1.2**: New function `_extract_glb_texture(gltf, image_index)` 
- Read buffer view, return raw PNG bytes
- Used during install to get custom texture data

### Phase 2: DLL Material/Texture Struct Creation

**Task 2.1**: `_create_granny_texture(dll, filename)` → pointer
- Allocate 92 bytes (`GrannyGetTotalObjectSize(GrannyTextureType)`)
- Zero-initialize, set `FromFileName*` to ctypes string buffer
- Add to `_keepalive`

**Task 2.2**: `_create_granny_material_chain(dll, mesh_name, texture_ptr)` → material_ptr
- Create:
  - `granny_texture` with desired `FromFileName`
  - Inner `granny_material`: Name = `"<Char>_Model_custom_<N>"`, Texture = texture_ptr
  - `granny_material_map`: Usage = `"color"`, Material = inner material
  - Outer `granny_material`: Name = `"Mat_<MeshName>"`, Maps = {1, ptr to map}
- Wire all pointers, add to `_keepalive`

**Task 2.3**: Extend `_create_new_mesh()` to accept `material_ptr`
- If provided, overwrite cloned mesh's MaterialBindings
- Allocate new 8-byte pointer array, update mesh +0x24 (count) and +0x28 (ptr)

### Phase 3: fi->Materials and fi->Textures Expansion

**Task 3.1**: After creating new material/texture structs, expand fi arrays
- `fi->Materials`: bump count, allocate new array, append new pointers
- `fi->Textures`: bump count, allocate new array, append new texture pointer
- Same pattern as existing `fi->Meshes` and `Model->MeshBindings` expansion

### Phase 4a: Texture Installation — Replace Existing (Ship First)

**Task 4.1**: `find_replacement_target(pkg_dir, width, height, format)` → (pkg, entry_name)
- Search texture index for entries with matching or larger pixel_data_size
- Prefer unused/debug textures, or let user choose
- The `FromFileName` in GR2 must match the replaced entry's base name

**Task 4.2**: Extend `replace_texture()` for dimension changes
- Update width, height, pixel_data_size, XNB file_size fields in the chunk
- Update entry total_size (big-endian)
- Recompress and rebuild

### Phase 4b: Texture Installation — Add New Entry (Stretch Goal)

**Task 4.3**: `add_texture_entry(pkg_path, entry_name, dds_path)` → bool
- Decompress target chunk
- Insert before 0xBE/0xFF terminator: 0xAD tag + CSString + BE size + XNB + texture data
- Recompress, rebuild PKG, update checksums
- Helpers: `_write_7bit_int`, `_write_csstring`, `_build_xnb_texture_entry`

### Phase 5: Manifest and GUI Integration

**Task 5.1**: Extend manifest with new-texture metadata
- Per-mesh: `custom_texture: {name, source: "glb"|"file", image_index}`
- Import reads this to decide material creation

**Task 5.2**: GUI workflow
- Install detects new mesh with unknown texture
- Dialog: (a) pick existing game texture, or (b) install custom texture
- For (a): dropdown from texture index
- For (b): auto-select replacement target or add new entry

## Key Technical Details

### How the Game Finds Textures
```
AddModelData reads: texture->FromFileName
  → fsGetPathFileName → base name (e.g. "Melinoe_Color512")
  → "GR2/" + base_name → HashGuid::StringIntern → runtime hash
  → Matched against loaded .pkg texture entries
```

### Replace-Existing Approach (Recommended)
- Pick an existing texture entry in any .pkg (e.g. a debug texture)
- Replace its pixel data with the custom texture
- Set `FromFileName` in the new GR2 material to match the replaced entry's name
- Hash lookup works automatically: the game loads the entry by name, finds the custom pixels
- Zero structural PKG changes, proven mechanism

### Serialization
- Golden path serializer walks the `file_info` type tree
- New structs in Python ctypes buffers work (proven by existing new-mesh code)
- New materials/textures must be in `fi->Materials`/`fi->Textures` arrays

## Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Serializer doesn't follow new material pointers | Low | Test early: clone existing material, add to fi->Materials, verify in output |
| AddModelData doesn't find new texture | Low | Ensure FromFileName basename matches .pkg entry name exactly |
| PKG chunk size change breaks reader (Phase 4b) | Medium | Ship Phase 4a first (replace existing = no size change) |
| New mesh renders with wrong shader | Low | Body shader (type 0) is default for non-Outline/non-Shadow meshes |

## Test Plan

1. Export Melinoe, add new mesh in Blender with existing body texture → verify material reuse
2. Add new mesh with custom texture → verify new material chain created in GR2
3. Verify golden path serialization includes new material/texture
4. Install custom texture (replace existing) → verify in-game rendering
5. Test uninstall → verify original texture restored
