# Address Recovery After Game Updates

When Hades II gets patched, hardcoded addresses in our hooks will likely
break. This doc lists every address we depend on and how to re-find it.

## Addresses currently used

### PDB-resolved (auto-updates, just rebuild)
These are resolved via `big::hades2_symbol_to_address[...]` so they survive
patches automatically — no RE needed.

- `sgg::DrawManager::DoDraw3D`
- `sgg::DrawManager::DoDrawShadow3D`
- `sgg::DrawManager::DoDraw3DThumbnail`
- `sgg::DrawManager::DoDrawShadowCast3D`
- `sgg::Granny3D::LoadAllModelAndAnimationData`
- `sgg::Granny3D::LoadModelData`
- `sgg::Granny3D::AddModelData`
- `sgg::Granny3D::mModelData`
- `sgg::Granny3D::mModelFiles`
- `sgg::HashGuid::Lookup`
- `sgg::HashGuid::StringIntern`
- `sgg::HashGuid::gStringBuffer`

### Internal offsets from DoDraw3D (WILL BREAK on update)

These are offsets INTO DoDraw3D's body. Used by the shadow cast code cave.

| Purpose | Offset from DoDraw3D | Current absolute (base 0x140000000) | Byte signature |
|---|---|---|---|
| Patch site | `+0x148E4` | `0x1401FE344` | `41 80 7A 2D 00 74 14` (cmp [r10+0x2d],0; je) |
| Shadow continue | `+0x148EB` | `0x1401FE34B` | `49 8B 0A 45 8B 42 20` (mov rcx,[r10]...) |
| Main continue | `+0x148FF` | `0x1401FE35F` | `45 8B 4A 28 45 8B 42 24` (mov r9d,[r10+0x28]...) |
| Loop next | `+0x14AC1` | `0x1401FE521` | `48 FF C6 4C 3B F6 7C` (inc rsi; cmp; jl) |

### Draw dispatch function
- Entry: `0x1401FD0D0` — size 6336 bytes
- Located via: callee of function at `0x1401EB550` (26-byte wrapper)
- Loop body: `0x1401FE080` - `0x1401FE527`

### Draw entry array global
- VA: `0x14173E348` (RVA `0x173E348`)
- Loaded via RIP-relative at `0x1401FD210` with displacement `0x1541131`
- Stride: 0x30 (48 bytes)

## Recovery strategy after update

### Step 1: Rebuild H2M's PDB symbol map
Just rebuild H2M — the PDB parser handles new addresses automatically.

### Step 2: Verify byte signatures at expected offsets
Run this check against the new Hades2.exe:

```python
import struct
exe = r'C:\Program Files (x86)\Steam\steamapps\common\Hades II\Ship\Hades2.exe'
# Get DoDraw3D address via PDB or pattern match
# Then check offsets from DoDraw3D
do_draw_3d = <new_addr>  # From PDB after update
checks = [
    (0x148E4, [0x41, 0x80, 0x7A, 0x2D, 0x00, 0x74, 0x14], "patch site"),
    (0x148EB, [0x49, 0x8B, 0x0A, 0x45, 0x8B, 0x42, 0x20], "shadow continue"),
    (0x148FF, [0x45, 0x8B, 0x4A, 0x28], "main continue"),
    (0x14AC1, [0x48, 0xFF, 0xC6, 0x4C, 0x3B, 0xF6, 0x7C], "loop next"),
]
with open(exe, 'rb') as f:
    # Read .text section...
    # For each check, seek to do_draw_3d + offset, verify bytes
```

### Step 3: If signatures don't match
The internal layout of DoDraw3D changed. We need to re-find the patch site
by PATTERN, not offset.

**Key pattern**: the shadow-flag check is `cmp byte [r10+0x2d], 0; je`
(instruction bytes `41 80 7A 2D 00 74 XX`). Search the .text section for
this pattern near DoDraw3D's start. The `XX` byte is the je rel8 offset to
the main path (where hash is loaded).

**Alternative**: find the shadow branch by looking for the `call DoDrawShadowCast3D`
instruction inside the dispatch. That call target resolves via PDB
(DoDrawShadowCast3D), so we can scan for `E8 xx xx xx xx` calls matching it.
The 7 bytes BEFORE that call in the dispatch are our patch site.

### Step 4: Find the RenderMesh object layout (v3.9 work)
NOT YET RE'd. When we resume v3.9:
- RenderMesh class exists in PDB (`sgg::RenderMesh`)
- Methods: `Set`, `Set2D`, `SetSprite`, `SetPolygon`, `SetFlatOrVertical`, etc.
- Global arrays: `sgg::RenderCommands::RENDER_MESHES`, `STATIC_MESHES`
- Struct layout UNKNOWN — needs per-field disassembly on the new binary

## Code that needs review after update

### Hell2Modding/src/lua_extensions/bindings/hades/draw.cpp
- `install_shadow_cast_patch()` — lines with hardcoded `+0x148E4`, `+0x148EB`, `+0x148FF`, `+0x14AC1`
- The `expected[]` byte array in the patch verification

### CG3H/tools/cg3h_builder_entry.py
- No game-address dependencies. Build tools read PDB-agnostic data.

### CG3H Lua files
- No game-address dependencies. All access via H2M Lua APIs.

## Risk assessment per component

### Foundational PRs (already merged into cg3h/v3.8-hot-reload)
| Component | Update risk | Notes |
|---|---|---|
| `add_granny_file` / `add_package_file` API (PR #1) | LOW | Pure string map, no game addresses |
| GPK filename exact-match fix (PR #2) | LOW | `strcmp` change in existing hook, no new addresses |

Both PRs are pending upstream submission to `Enderclem0/Hell2Modding`. They
survive game updates without changes. Local branches: `feature/add-granny-file-api`
and `fix/gpk-exact-match`.

### v3.8 draw gate
| Component | Update risk | Notes |
|---|---|---|
| DoDraw3D detour hook | LOW | PDB symbol, auto-updates |
| DoDrawShadow3D detour hook | LOW | PDB symbol, auto-updates |
| DoDraw3DThumbnail detour hook | LOW | PDB symbol, auto-updates |
| Shadow cast code cave | HIGH | Hardcoded offsets from DoDraw3D |
| `set_draw_visible` binding | LOW | Uses Lookup/StringIntern (PDB) |

### v3.9 WIP (exploratory)
| Component | Update risk | Notes |
|---|---|---|
| `set_draw_remap` / `clear_draw_remap` | LOW | PDB symbols only |
| `set_draw_remap_hash` | NONE | Pure numeric, no game interaction |
| `load_model_entry` | LOW | Uses LoadModelData/StringIntern (PDB) |
| RenderMesh RE findings | N/A | Not yet implemented |

## Mitigation

The code cave's `expected[]` byte verification will safely REFUSE to patch
if the bytes don't match. So a broken update won't corrupt the game — it'll
just disable shadow cast filtering. That's a safe failure mode.

The rest of the draw gate (detour hooks on the 3 main draw functions) will
continue to work via PDB symbols. Only shadow cast rendering gets the
pre-update behavior (shadows visible even when character is hidden).
