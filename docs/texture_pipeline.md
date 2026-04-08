# Hades II Texture Pipeline

## Overview

Textures are NOT stored in GR2 files. They live in `.pkg` package files under
`Content/Packages/1080p/`. The GR2 material chain provides a texture name, and the
engine resolves it at runtime via a hash lookup.

```
GR2 material chain -> FromFileName -> extract basename
  -> prepend "GR2/" -> StringIntern hash -> resolves to .pkg texture at runtime
```

---

## GR2 Material Chain

Each mesh's texture is found by walking the material chain:

```
granny_mesh
  -> MaterialBindings[0] -> granny_material (44 bytes)
      Name*: "Mat_Melinoe"
      Maps: {count=1, ptr}
    -> Maps[0] -> granny_material_map (16 bytes)
        Usage*: "color"
        Map*: ptr to nested material
      -> granny_material "Melinoe_Model_file1"
          Texture*: ptr to granny_texture
        -> granny_texture (92 bytes)
            FromFileName*: "D:/.../Melinoe_Color512.png"
```

The engine calls `fsGetPathFileName(texture->FromFileName)` to extract just the
filename, prepends `"GR2/"`, and hashes it with `HashGuid::StringIntern` to look
up the texture from loaded packages.

### Fallback levels

Three levels for texture name resolution:
1. **Material chain** (most reliable) — walks the struct pointers above
2. **fi->Textures** — reads the file_info texture array
3. **Name-based guessing** — derives texture name from character name

### Multi-texture per character

Each mesh can have its own texture. The exporter walks the material chain per mesh,
embeds the correct PNG in each glTF material, and exports separate DDS/PNG files
per texture. Example: Athena's body and weapons get different textures automatically.

### Lua GrannyTexture overrides

48 entities override their texture at runtime via Lua scripts in the game's
`Scripts/` directory. The exporter parses these and uses the Lua-specified texture
when it differs from the material chain value.

### Variant textures

Some characters have alternate textures for different game modes (e.g.
`HecateEM_Color` for Extreme Measures). These are exported as standalone files
and marked with `"variant": true` in the manifest. They are NOT embedded in the GLB.

---

## Mesh Type Detection

Determined by mesh name string matching in the engine's `AddModelData`:
```c
if (strstr(mesh->Name, "Outline"))    -> type = 1 (outline shader)
if (strstr(mesh->Name, "ShadowMesh")) -> type = 2 (shadow shader)
else                                  -> type = 0 (Granny3D body shader)
```

New meshes added in Blender inherit the correct template material based on their name
(Outline -> Mat_Outline, ShadowMesh -> shadow shader, otherwise body shader with
custom texture).

---

## New Material Creation (mesh_add)

When adding meshes with custom textures, the importer creates new structs in DLL memory:
- `granny_material` (44 bytes)
- `granny_material_map` (16 bytes)
- `granny_texture` (92 bytes) with `FromFileName` set to the custom texture name

These are wired into the material chain and `fi->Materials` / `fi->Textures` are expanded.
The game resolves custom textures via the same `FromFileName` hash lookup (`"GR2/" + basename`).

---

## Export Output

Per-character directory with textures in multiple formats:

```
Athena/
  Athena.glb                  Meshes + skeleton + per-mesh embedded PNG
  Athena_Color.dds            Body texture (BC7 + all mipmaps, lossless)
  Athena_Color.png            Body texture (base mip, for image editors)
  AthenaWeapons_Color.dds     Weapon texture (separate)
  AthenaWeapons_Color.png     Weapon texture
  manifest.json               Metadata (see below)
```

### Export manifest

`manifest.json` tracks per-texture metadata:
- `pkg` — source `.pkg` file
- `pkgs` — ALL `.pkg` files containing this texture (for multi-pkg operations)
- `format` — DDS format code (BC7, BC3, BC1, etc.)
- `width`, `height` — pixel dimensions
- `mip_count` — number of mip levels
- `png_hash` — hash of the exported PNG (for change detection)
- `variant` — true if this is an alternate texture

---

## Texture Import

### H2M Workflow (standalone .pkg)

The primary workflow builds a new `.pkg` from scratch:

1. Compress custom PNG to BC7/BC3/BC1 DDS with mipmaps via `etcpak`
2. Wrap each texture in a 0xAD (Texture2D) chunk with correct XNB headers
3. Write `.pkg` file with proper chunk table and offsets
4. H2M loads the standalone `.pkg` at runtime using a dual-method approach:
   - `load_package_overrides_set` — for NEW texture names not already in the game's
     cache; triggers real `ReadTexture2D` on scene transition
   - `rom.game.LoadPackages` (via `rom.on_import.post`) — for REPLACING existing
     texture names; pre-seeds the `mLoadedTexture2DHash` cache so last-loaded wins
   Both methods must be used together. Using only one fails for certain cases.
5. The game's texture resolver picks up the replacement by name

No game files are modified. No `.pkg_manifest` is needed for H2M loading.
No checksum updates needed (H2M bypasses `checksums.txt` validation).

### DDS Compression

PNG-to-DDS compression uses `etcpak` (BC7 encoder). Mipmaps are generated from the
base level via Pillow `Image.resize` and each level is compressed independently.
The output format matches the original texture's format code (BC7, BC3, or BC1).

Custom textures are auto-resized to 512x512 to prevent oversized chunks.

### Three editing paths

**Blender**: The texture is embedded as PNG in the GLB. Edit in Blender's texture paint
mode, export GLB. Change detection compares the embedded PNG against `png_hash`.

**PNG editors** (Photoshop, GIMP): Edit the standalone `.png` from the export directory.
Change detection via manifest hash comparison.

**DDS editors** (paint.net, advanced): Edit the `.dds` file directly. Auto-truncates
extra mip levels if the editor added more than the original. Zero quality loss.

---

## PKG File Format Reference

See [`file_formats.md`](file_formats.md) for the complete PKG binary layout, asset
type tags, and texture entry format.

Key points:
- Both 0xAA (Texture3D) and 0xAD (Texture2D) use big-endian size + XNB wrapper
- Textures are often duplicated across biome packages (BiomeF, BiomeHub, etc.)
- The texture index (`_texture_index.json`) maps texture names to their `.pkg` locations

### Texture format codes

| Code | Format | Description |
|------|--------|-------------|
| 0x00 | B8G8R8A8_UNORM | 32-bit BGRA uncompressed |
| 0x04 | DXBC1_RGB_UNORM | BC1/DXT1, 4bpp, no alpha |
| 0x05 | DXBC2_UNORM | BC2/DXT3, 8bpp, sharp alpha |
| 0x06 | DXBC3_UNORM | BC3/DXT5, 8bpp, smooth alpha |
| 0x0C | R8_UNORM | 8-bit grayscale |
| 0x0E | R8G8B8A8_UNORM | 32-bit RGBA uncompressed |
| 0x1A | R8_UNORM | 8-bit grayscale (variant) |
| 0x1C | **DXBC7_UNORM** | BC7, high quality block compression |
| 0x1E | R8G8_UNORM | 16-bit RG |
| 0x20 | Native | DDS/KTX via loadTextureNative |

---

## File Locations

```
<game>/Content/Packages/
  1080p/
    Fx.pkg                   Some 3D model textures (534MB)
    <Character>.pkg          Character-specific textures + 2D sprites
    BiomeF.pkg, BiomeHub.pkg, ...   Biome packages (textures duplicated across these)
    checksums.txt            XXH64 hashes (validated at startup, bypassed by H2M)
  720p/
    ...                      Lower resolution variants
```

3D model textures use a `GR2\` or `Models\` path prefix in `.pkg` entries.
A texture index (`_texture_index.json`) can be built by scanning all `.pkg` files
once with `pkg_texture.py build-index` for fast batch lookups.
