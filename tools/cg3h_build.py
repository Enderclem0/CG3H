"""
CG3H Build — Produces Hell2Modding-compatible mod packages.

Usage:
    python cg3h_build.py <mod_dir>
    python cg3h_build.py <mod_dir> --package  (also creates Thunderstore ZIP)
    python cg3h_build.py <mod_dir> --check-conflicts --r2-plugins-dir <path>
    python cg3h_build.py <mod_dir> --r2-plugins-dir <path>  (build with conflict check)

Reads mod.json from the mod directory, builds GPK + standalone PKG,
and outputs to build/ in H2M folder structure.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import traceback
import zipfile

import numpy as np
import pygltflib

_tools_dir = os.path.dirname(os.path.abspath(__file__))
if _tools_dir not in sys.path:
    sys.path.insert(0, _tools_dir)

from cg3h_constants import (
    CG3H_BUILDER_DEPENDENCY,
    CG3H_BUILDER_FOLDER,
    H2M_DEPENDENCY,
    find_game_path,
)
from pkg_texture import build_standalone_pkg
from texture_variant import walk_texture_overrides


def _is_mesh_changed(gltf, mesh, blob, orig_mesh, baseline_positions=None):
    """Check whether a GLB mesh differs from the original export.

    Uses baseline position data (from .baseline_positions.npz) for direct
    comparison with tolerance, absorbing Blender's ~1e-5 float noise while
    detecting any genuine edit >= 0.001.  Handles Blender's normal-split
    vertex inflation via unique (UV + coarse position) counting.
    """
    orig_vc = orig_mesh.get('vertex_count')

    for prim in mesh.primitives:
        pos_idx = prim.attributes.POSITION
        if pos_idx is None:
            continue
        acc = gltf.accessors[pos_idx]
        bv = gltf.bufferViews[acc.bufferView]
        pos_bytes = blob[bv.byteOffset:bv.byteOffset + bv.byteLength]
        cur_vc = acc.count

        if orig_vc is None:
            continue  # no manifest data, can't compare

        # Same vertex count — compare positions against baseline
        if cur_vc == orig_vc:
            if (baseline_positions is not None
                    and baseline_positions.shape == (cur_vc, 3)):
                pos_data = np.frombuffer(pos_bytes, dtype=np.float32).reshape(cur_vc, 3)
                max_diff = np.max(np.abs(pos_data - baseline_positions))
                if max_diff > 1e-4:  # well above ~1e-5 noise, catches any visible edit
                    return True
            # Check index count as extra signal
            orig_ic = orig_mesh.get('index_count')
            if orig_ic is not None and prim.indices is not None:
                if gltf.accessors[prim.indices].count != orig_ic:
                    return True
            continue

        # Fewer vertices — definitely changed
        if cur_vc < orig_vc:
            return True

        # More vertices — check if it's just Blender normal splits
        uv_idx = prim.attributes.TEXCOORD_0
        if uv_idx is None:
            return True  # can't verify without UVs

        uv_acc = gltf.accessors[uv_idx]
        uv_bv = gltf.bufferViews[uv_acc.bufferView]
        uv_bytes = blob[uv_bv.byteOffset:uv_bv.byteOffset + uv_bv.byteLength]

        # Count unique (exact UV + quantized position) tuples.
        # Quantized to 0.01 — coarser than Blender noise (~1e-5) so
        # noise doesn't create false unique entries.
        pos_data = np.frombuffer(pos_bytes, dtype=np.float32).reshape(cur_vc, 3)
        uv_data = np.frombuffer(uv_bytes, dtype=np.float32).reshape(cur_vc, 2)
        pos_q = np.round(pos_data * 100).astype(np.int32)

        unique = set()
        for vi in range(cur_vc):
            # Fixed-width fields: 8 bytes UV + 12 bytes pos — no ambiguity
            unique.add(uv_data[vi].tobytes() + pos_q[vi].tobytes())

        if len(unique) > orig_vc:
            return True  # genuinely more vertices than original

        # Unique count <= original — just normal splits, mesh unchanged

    return False


def _strip_unchanged_data(glb_path, mod_dir):
    """
    Strip unchanged meshes and textures from a GLB for distribution.
    Compares against the original export to detect what actually changed.
    Keeps: new meshes, edited meshes, new textures, edited textures.
    Removes: unmodified meshes, unmodified textures, skeleton (rebuilt from game).
    Returns stripped GLB bytes, or None if nothing changed or stripping failed.
    """
    try:
        manifest_path = os.path.join(mod_dir, 'manifest.json')
        if not os.path.isfile(manifest_path):
            return None

        with open(manifest_path) as f:
            manifest = json.load(f)

        # Load the modified GLB
        gltf = pygltflib.GLTF2().load(glb_path)
        blob = gltf.binary_blob()

        # Load baseline positions for direct comparison
        baseline_path = os.path.join(mod_dir, '.baseline_positions.npz')
        try:
            baseline = dict(np.load(baseline_path)) if os.path.isfile(baseline_path) else {}
        except Exception:
            baseline = {}

        original_png_hashes = {}
        for tex_name, tex_info in manifest.get('textures', {}).items():
            if tex_info.get('png_hash'):
                original_png_hashes[tex_name] = tex_info['png_hash']

        original_names = {m['name'] for m in manifest.get('meshes', [])}

        # Determine which meshes to keep
        keep_indices = []
        for i, mesh in enumerate(gltf.meshes):
            if mesh.name not in original_names:
                # New mesh — always keep
                keep_indices.append(i)
                continue

            orig_mesh = next((m for m in manifest.get('meshes', [])
                              if m['name'] == mesh.name), None)
            if not orig_mesh:
                keep_indices.append(i)
                continue

            changed = _is_mesh_changed(gltf, mesh, blob, orig_mesh,
                                       baseline.get(mesh.name))
            if changed:
                keep_indices.append(i)

        # Also check textures — strip unchanged ones
        images_to_keep = set()
        for img_idx, img in enumerate(gltf.images or []):
            if img.bufferView is not None and img.name:
                bv = gltf.bufferViews[img.bufferView]
                png_data = blob[bv.byteOffset:bv.byteOffset + bv.byteLength]
                cur_hash = hashlib.md5(png_data).hexdigest()
                orig_hash = original_png_hashes.get(img.name, '')
                if cur_hash != orig_hash:
                    images_to_keep.add(img_idx)
                    # Also keep meshes that use this texture
                    for mi, mesh in enumerate(gltf.meshes):
                        for prim in mesh.primitives:
                            if prim.material is not None:
                                mat = gltf.materials[prim.material]
                                if (mat.pbrMetallicRoughness and
                                        mat.pbrMetallicRoughness.baseColorTexture):
                                    tex = gltf.textures[mat.pbrMetallicRoughness.baseColorTexture.index]
                                    if tex.source == img_idx and mi not in keep_indices:
                                        keep_indices.append(mi)

        # Strip unchanged animations
        original_anim_hashes = manifest.get('animations', {}).get('hashes', {})
        keep_anims = []
        stripped_anims = 0
        if gltf.animations and original_anim_hashes:
            for ai, anim in enumerate(gltf.animations):
                orig_hash = original_anim_hashes.get(anim.name, '')
                if not orig_hash:
                    # New animation — keep it
                    keep_anims.append(ai)
                    continue
                # Compute current hash from the GLB animation data
                anim_hash = hashlib.md5()
                for ch in anim.channels:
                    sampler = anim.samplers[ch.sampler]
                    for acc_idx in [sampler.input, sampler.output]:
                        acc = gltf.accessors[acc_idx]
                        bv = gltf.bufferViews[acc.bufferView]
                        data = blob[bv.byteOffset:bv.byteOffset + bv.byteLength]
                        anim_hash.update(data)
                if anim_hash.hexdigest() != orig_hash:
                    keep_anims.append(ai)
                else:
                    stripped_anims += 1
        elif gltf.animations:
            # No hashes in manifest — keep all (can't determine what changed)
            keep_anims = list(range(len(gltf.animations)))

        has_changes = bool(keep_indices) or bool(keep_anims) or bool(images_to_keep)
        if not has_changes:
            print("  No changes detected — nothing to strip")
            return None

        # Rebuild GLB with only kept meshes
        new_meshes = [gltf.meshes[i] for i in sorted(set(keep_indices))]
        old_to_new = {old: new for new, old in enumerate(sorted(set(keep_indices)))}

        for node in gltf.nodes:
            if node.mesh is not None:
                node.mesh = old_to_new.get(node.mesh)

        gltf.meshes = new_meshes

        # Strip unchanged animations
        if gltf.animations and original_anim_hashes:
            gltf.animations = [gltf.animations[i] for i in keep_anims]

        kept_names = [m.name for m in new_meshes]
        kept_originals = sum(1 for n in kept_names if n in original_names)
        stripped_count = len(original_names) - kept_originals
        if kept_names:
            print(f"  Keeping {len(new_meshes)} mesh(es): {', '.join(kept_names[:5])}")
        print(f"  Stripped {stripped_count} unchanged mesh(es)")
        if stripped_anims:
            print(f"  Stripped {stripped_anims} unchanged animation(s), "
                  f"keeping {len(keep_anims)}")

        tmp = tempfile.mktemp(suffix=".glb")
        gltf.save(tmp)
        with open(tmp, "rb") as f:
            data = f.read()
        os.unlink(tmp)
        return data

    except Exception as e:
        print(f"  Strip failed: {e}")
        traceback.print_exc()
        return None


def _infer_operations(mod):
    """
    Infer what operations a mod performs from its assets.
    Returns a set of operations: {'adds_meshes', 'replaces_meshes',
                                   'replaces_textures', 'adds_textures',
                                   'patches_animations'}
    """
    ops = set()
    mod_type = mod.get('type', '')
    assets = mod.get('assets', {})
    meshes = mod.get('meshes', [])
    textures = assets.get('textures', [])

    # `type` may be a single string or a list of strings (for mods that
    # declare multiple ops, e.g. `["mesh_add", "mesh_replace"]`).
    if isinstance(mod_type, list):
        for t in mod_type:
            if t == 'mesh_add': ops.add('adds_meshes')
            elif t == 'mesh_replace': ops.add('replaces_meshes')
            elif t == 'texture_replace': ops.add('replaces_textures')
            elif t == 'animation_patch': ops.add('patches_animations')
    elif mod_type:
        if mod_type == 'mesh_add': ops.add('adds_meshes')
        elif mod_type == 'mesh_replace': ops.add('replaces_meshes')
        elif mod_type == 'texture_replace': ops.add('replaces_textures')
        elif mod_type == 'animation_patch': ops.add('patches_animations')

    # Infer from assets
    if assets.get('glb'):
        # Check if meshes are new or replacing existing
        has_replacements = any(m.get('replaces') for m in meshes)
        has_new = any(not m.get('replaces') for m in meshes)
        if has_replacements:
            ops.add('replaces_meshes')
        if has_new or not meshes:
            # GLB with no explicit mesh declarations = could be add or replace
            if 'replaces_meshes' not in ops:
                ops.add('adds_meshes')

    for tex in textures:
        if tex.get('custom'):
            ops.add('adds_textures')
        elif tex.get('replaces') or tex.get('name'):
            ops.add('replaces_textures')

    # Infer animation operations from assets
    animations = assets.get('animations', {})
    if isinstance(animations, dict) and animations.get('patch'):
        ops.add('patches_animations')

    return ops


def detect_conflicts(mod_dir, r2_plugins_dir):
    """Scan r2modman plugins directory for conflicts with the current mod.

    Returns True if safe to install, False if a hard conflict was found.
    Always prints warnings/errors it discovers.
    """
    mod_json_path = os.path.join(mod_dir, 'mod.json')
    if not os.path.isfile(mod_json_path):
        print("CONFLICT CHECK: No mod.json found, skipping.")
        return True

    with open(mod_json_path) as f:
        mod = json.load(f)

    my_ops = _infer_operations(mod)
    my_character = mod.get('target', {}).get('character', '')
    my_name = mod.get('metadata', {}).get('name', 'UnnamedMod')
    my_textures = {t.get('name') for t in mod.get('assets', {}).get('textures', [])
                   if t.get('name')}

    if not r2_plugins_dir or not os.path.isdir(r2_plugins_dir):
        print("CONFLICT CHECK: Plugins directory not found, skipping.")
        return True

    # Scan all installed CG3H mods (any directory containing mod.json)
    other_mods = []
    for entry in os.listdir(r2_plugins_dir):
        entry_path = os.path.join(r2_plugins_dir, entry)
        if not os.path.isdir(entry_path):
            continue
        # Check plugins_data sub-dirs and the entry itself
        for search_dir in [entry_path]:
            candidate = os.path.join(search_dir, 'mod.json')
            if os.path.isfile(candidate) and os.path.abspath(candidate) != os.path.abspath(mod_json_path):
                try:
                    with open(candidate) as f:
                        other_mods.append(json.load(f))
                except (json.JSONDecodeError, OSError):
                    pass  # skip mods with invalid/unreadable mod.json

    if not other_mods:
        print(f"CONFLICT CHECK: No other CG3H mods found in {r2_plugins_dir}")
        return True

    safe = True
    for other in other_mods:
        o_name = other.get('metadata', {}).get('name', '?')
        o_ops = _infer_operations(other)
        o_char = other.get('target', {}).get('character', '')
        o_textures = {t.get('name') for t in other.get('assets', {}).get('textures', [])
                      if t.get('name')}

        # Texture conflict: two mods replacing/adding the same texture name
        if my_textures & o_textures:
            overlap = my_textures & o_textures
            print(f"CONFLICT: '{my_name}' and '{o_name}' both modify "
                  f"texture(s): {', '.join(sorted(overlap))}")
            safe = False

        # Same character checks
        if my_character and my_character == o_char:
            # Two mesh_replace = hard conflict
            if 'replaces_meshes' in my_ops and 'replaces_meshes' in o_ops:
                print(f"CONFLICT: '{my_name}' and '{o_name}' both replace "
                      f"meshes for '{my_character}'")
                safe = False

            # mesh_add + mesh_replace = warning
            my_adds = 'adds_meshes' in my_ops
            my_replaces = 'replaces_meshes' in my_ops
            o_adds = 'adds_meshes' in o_ops
            o_replaces = 'replaces_meshes' in o_ops
            if (my_adds and o_replaces) or (my_replaces and o_adds):
                print(f"WARNING: '{my_name}' and '{o_name}' both target "
                      f"'{my_character}' (add + replace may conflict)")

            # Animation conflicts: two mods patching animations for same character
            if 'patches_animations' in my_ops and 'patches_animations' in o_ops:
                # Check filter overlap — if both have filters, they may target different anims
                my_anim_filter = mod.get('assets', {}).get('animations', {}).get('filter', '')
                o_anim_filter = other.get('assets', {}).get('animations', {}).get('filter', '')
                if not my_anim_filter or not o_anim_filter or my_anim_filter == o_anim_filter:
                    print(f"CONFLICT: '{my_name}' and '{o_name}' both patch "
                          f"animations for '{my_character}'")
                    safe = False
                else:
                    print(f"OK: '{my_name}' and '{o_name}' patch different "
                          f"animations for '{my_character}' "
                          f"('{my_anim_filter}' vs '{o_anim_filter}')")

            # mesh_add + mesh_add = OK (mergeable)
            # patches + anything = OK (vertex edits are independent)

    if safe:
        print(f"CONFLICT CHECK: No conflicts detected for '{my_name}'")
    return safe


def _build_conflicts_json(mod):
    """Build a conflicts.json describing what this mod touches."""
    touches = {}
    character = mod.get('target', {}).get('character', '')
    ops = _infer_operations(mod)

    if character:
        touches['character'] = character
    touches['operations'] = sorted(ops)

    tex_names = [t.get('name') for t in mod.get('assets', {}).get('textures', [])
                 if t.get('name')]
    if tex_names:
        touches['textures'] = sorted(tex_names)

    mesh_entries = mod.get('target', {}).get('mesh_entries', [])
    if mesh_entries:
        touches['mesh_entries'] = mesh_entries

    return touches


def _sync_mod_json(mod_dir):
    """Detect workspace changes and update mod.json to match reality.

    Two authoring flows feed in:
      1. Blender flow — compares GLB against manifest.json to find new
         meshes, edited meshes, changed/new textures.  Requires both
         GLB + manifest.
      2. Folder-mirror flow (v3.12) — walks <mod>/textures/ for PNG
         overrides whose path mirrors the PKG entry path.  No GLB or
         manifest required; lets a portrait-only / texture-only mod
         author skip Blender entirely.

    Updates mod.json's type and assets.textures so that
    _infer_operations produces the correct build plan.
    """
    mod_json_path = os.path.join(mod_dir, 'mod.json')
    if not os.path.isfile(mod_json_path):
        return

    with open(mod_json_path) as f:
        mod = json.load(f)

    manifest_path = os.path.join(mod_dir, 'manifest.json')
    manifest = {}
    if os.path.isfile(manifest_path):
        with open(manifest_path) as f:
            manifest = json.load(f)

    assets = mod.setdefault('assets', {})
    glb_name = assets.get('glb', '')
    glb_path = os.path.join(mod_dir, glb_name) if glb_name else ''
    has_glb = bool(glb_path) and os.path.isfile(glb_path)
    # Blender-flow paths (mesh / animation detection) require GLB +
    # manifest both; the folder-mirror flow requires neither.  Each
    # Blender-flow section guards on its own manifest entries, so an
    # empty manifest dict naturally short-circuits them.

    changed = False
    types = set()
    if isinstance(mod.get('type'), list):
        types = set(mod['type'])
    elif mod.get('type'):
        types = {mod['type']}

    # ── Detect mesh changes ──
    manifest_meshes = {m['name']: m for m in manifest.get('meshes', [])}
    if manifest_meshes and has_glb:
        try:
            gltf = pygltflib.GLTF2().load(glb_path)
            blob = gltf.binary_blob()

            baseline_path = os.path.join(mod_dir, '.baseline_positions.npz')
            try:
                baseline = dict(np.load(baseline_path)) if os.path.isfile(baseline_path) else {}
            except Exception:
                baseline = {}
            baseline_available = bool(baseline)

            has_new = False
            has_edited = False
            for mesh in (gltf.meshes or []):
                if not mesh.name:
                    continue
                orig = manifest_meshes.get(mesh.name)
                if orig is None:
                    has_new = True
                    continue
                if _is_mesh_changed(gltf, mesh, blob, orig, baseline.get(mesh.name)):
                    has_edited = True

            if has_new and 'mesh_add' not in types:
                types.add('mesh_add')
                changed = True
            if has_edited and 'mesh_replace' not in types:
                types.add('mesh_replace')
                changed = True
            # Heal spurious mesh_replace only when we have POSITIVE
            # evidence of no edit — i.e., a baseline file was present
            # and positions matched within tolerance.  The Blender
            # export pipeline doesn't ship a baseline today, so without
            # this guard every Blender-authored replacer would lose the
            # tag and build as "type": "" (unclassified, invisible to
            # the picker).  mod_dir has a baseline only when the mod
            # was imported through `gr2_to_gltf.py` (CLI path) which
            # writes one alongside the GLB.
            if baseline_available and not has_edited and 'mesh_replace' in types:
                types.discard('mesh_replace')
                changed = True
        except Exception as e:
            print(f"  WARNING: mesh change detection failed: {e}")

    # ── Detect texture changes (same logic as _strip_unchanged_data) ──
    manifest_textures = manifest.get('textures', {})
    if manifest_textures:
        try:
            existing_tex_names = {t.get('name') for t in assets.get('textures', [])}
            for tex_name, tex_info in manifest_textures.items():
                if tex_info.get('variant') or tex_name in existing_tex_names:
                    continue
                orig_hash = tex_info.get('png_hash', '')
                if not orig_hash:
                    continue
                png_file = os.path.join(mod_dir, f"{tex_name}.png")
                if os.path.isfile(png_file):
                    with open(png_file, 'rb') as fh:
                        cur_hash = hashlib.md5(fh.read()).hexdigest()
                    if cur_hash != orig_hash:
                        assets.setdefault('textures', []).append({
                            'name': tex_name,
                            'file': f"{tex_name}.png",
                            'replaces': True,
                            'width': tex_info.get('width', 512),
                            'height': tex_info.get('height', 512),
                            'pkg_entry_name': tex_info.get('pkg_entry_name', f"GR2\\{tex_name}"),
                            'pkgs': tex_info.get('pkgs', []),
                        })
                        if 'texture_replace' not in types:
                            types.add('texture_replace')
                        changed = True
        except Exception as e:
            print(f"  WARNING: texture change detection failed: {e}")

    # ── v3.12: folder-mirror authoring path ──
    # If the modder dropped PNGs under <mod>/textures/ mirroring PKG entry
    # paths, the walker emits one descriptor per file.  We dedupe against
    # the flat-with-manifest entries above by `pkg_entry_name`, so a mod
    # using both modes for the same PKG path lands a single override.
    try:
        existing_pkg_entries = {
            t.get('pkg_entry_name') for t in assets.get('textures', [])
            if t.get('pkg_entry_name')
        }
        existing_tex_files = {
            t.get('file') for t in assets.get('textures', [])
            if t.get('file')
        }
        added_from_mirror = 0
        for ovr in walk_texture_overrides(mod_dir):
            pkg_entry = ovr['pkg_entry']
            if pkg_entry in existing_pkg_entries:
                continue
            # Use the relative path from textures/ (forward-slash form)
            # as both the file ref and the texture name.  Slash-separated
            # so cross-platform builds produce identical mod.json output.
            rel = ovr['rel_path']
            file_ref = f"textures/{rel}"
            if file_ref in existing_tex_files:
                continue
            tex_name = os.path.splitext(rel)[0]
            assets.setdefault('textures', []).append({
                'name': tex_name,
                'file': file_ref,
                'replaces': True,
                'pkg_entry_name': pkg_entry,
            })
            existing_pkg_entries.add(pkg_entry)
            existing_tex_files.add(file_ref)
            added_from_mirror += 1
        if added_from_mirror:
            if 'texture_replace' not in types:
                types.add('texture_replace')
            changed = True
            print(f"  detected {added_from_mirror} texture override(s) "
                  f"from textures/ folder")
    except Exception as e:
        print(f"  WARNING: folder-mirror texture detection failed: {e}")

    # ── Pick up custom textures from new meshes ──
    # The converter writes *_custom_textures.json; also check build
    # output.  Requires GLB (we extract PNGs from it via pygltflib).
    custom_tex_dirs = [mod_dir, os.path.join(mod_dir, 'build')] if has_glb else []
    for search_dir in custom_tex_dirs:
        for root, dirs, files in os.walk(search_dir):
            for fname in files:
                if fname.endswith('_custom_textures.json'):
                    ct_path = os.path.join(root, fname)
                    try:
                        with open(ct_path) as f:
                            custom = json.load(f)
                        existing_tex_names = {t.get('name') for t in assets.get('textures', [])}
                        # Extract PNGs from GLB for custom textures
                        gltf = pygltflib.GLTF2().load(glb_path)
                        blob = gltf.binary_blob()
                        for tex_name, info in custom.items():
                            if tex_name in existing_tex_names:
                                continue
                            img_idx = info.get('glb_image_index')
                            if img_idx is not None and img_idx < len(gltf.images or []):
                                img = gltf.images[img_idx]
                                if img.bufferView is not None:
                                    bv = gltf.bufferViews[img.bufferView]
                                    png_data = blob[bv.byteOffset:bv.byteOffset + bv.byteLength]
                                    png_file = f"{tex_name}.png"
                                    with open(os.path.join(mod_dir, png_file), 'wb') as pf:
                                        pf.write(png_data)
                                    assets.setdefault('textures', []).append({
                                        'name': tex_name,
                                        'file': png_file,
                                        'custom': True,
                                        'width': 512,
                                        'height': 512,
                                    })
                                    changed = True
                    except Exception:
                        pass  # ignore unreadable custom_textures.json files

    # ── Remove stale texture_replace if nothing left ──
    if types - {'texture_replace'} and not assets.get('textures'):
        types.discard('texture_replace')

    # ── Detect edited animations (canonical list) ──
    # Auto-populate target.animations with the names of animation tracks
    # this mod actually edits, so conflict detection at scan time can do
    # an exact set-intersect across mods without re-parsing GLBs.
    edited_animations = []
    if glb_path and os.path.isfile(glb_path):
        try:
            gltf_anim = pygltflib.GLTF2().load(glb_path)
            blob_anim = gltf_anim.binary_blob()
            anim_hashes = manifest.get('animations', {}).get('hashes', {})
            for anim in (gltf_anim.animations or []):
                if not anim.name:
                    continue
                orig_hash = anim_hashes.get(anim.name, '')
                if not orig_hash:
                    # No baseline hash → conservative: assume edited.
                    edited_animations.append(anim.name)
                    continue
                # Hash channel data the same way the export pass did and
                # compare.  Different hash → modder edited this clip.
                h = hashlib.md5()
                for ch in anim.channels:
                    sampler = anim.samplers[ch.sampler]
                    for acc_idx in (sampler.input, sampler.output):
                        acc = gltf_anim.accessors[acc_idx]
                        bv = gltf_anim.bufferViews[acc.bufferView]
                        h.update(blob_anim[bv.byteOffset:bv.byteOffset + bv.byteLength])
                if h.hexdigest() != orig_hash:
                    edited_animations.append(anim.name)
        except Exception as e:
            print(f"  WARNING: animation detection failed: {e}")

    target = mod.setdefault('target', {})
    existing_anims = target.get('animations', [])
    if edited_animations:
        # Sort for deterministic diffs.  Keep dict-shape compatibility
        # with the legacy `assets.animations.filter` (a single string) by
        # writing the list under target.animations only.
        new_anims = sorted(set(edited_animations))
        if new_anims != existing_anims:
            target['animations'] = new_anims
            changed = True
        if 'animation_patch' not in types:
            types.add('animation_patch')
            changed = True
    elif existing_anims and 'animation_patch' not in types:
        # mod.json claims animations but type doesn't say animation_patch
        # — heal it.  This runs when the mod was authored by hand.
        types.add('animation_patch')
        changed = True

    # ── Detect new animations (v3.11 animation_add) ──
    # An "animation" in the GLB whose name doesn't exist in manifest.
    # animations.hashes is a brand-new clip the modder authored.  Auto-
    # populate target.new_animations so the builder routes it through
    # the animation_add path.  Modders can override any field by hand-
    # editing mod.json; we only fill in entries the modder hasn't.
    discovered_new = []
    if glb_path and os.path.isfile(glb_path):
        try:
            gltf_for_add = pygltflib.GLTF2().load(glb_path)
            anim_hashes = manifest.get('animations', {}).get('hashes', {})
            for anim in (gltf_for_add.animations or []):
                if anim.name and anim.name not in anim_hashes:
                    discovered_new.append(anim.name)
        except Exception as e:
            print(f"  WARNING: new-animation detection failed: {e}")

    if discovered_new:
        # Author slug: prefer mod metadata.author, fall back to "Mod"
        # so we always produce a parseable granny_name.  The Blender
        # addon auto-prefixes action names with the same slug at export
        # time (so cross-mod logical_name collisions are avoided);
        # detect that here and strip BEFORE recomposing granny_name to
        # avoid <Char>_<Author>_<Author>_<Action>_C_00 double-prefix.
        meta = mod.get('metadata', {})
        author = (meta.get('author') or 'Mod').replace(' ', '').replace('-', '')
        author_slug = ''.join(c for c in author if c.isalnum())
        author_prefix = author_slug + '_' if author_slug else ''
        character = target.get('character') or 'Unknown'

        # Default clone_from: prefer a base-idle entry on the same
        # character (safe same-skeleton template), else the first
        # entry in the manifest's hashes alphabetically.  Modders
        # override per-entry if they want a specific template.
        anim_hashes = manifest.get('animations', {}).get('hashes', {})
        stock_names = sorted(anim_hashes.keys())
        default_template = next(
            (n for n in stock_names if 'NoWeapon_Base_Idle' in n),
            stock_names[0] if stock_names else None,
        )

        existing = target.get('new_animations', []) or []
        existing_by_logical = {
            e.get('logical_name'): e for e in existing
            if isinstance(e, dict) and e.get('logical_name')
        }

        def _build_defaults(action_name):
            # action_name is what the engine sees as logical_name (and
            # what the modder will call from Lua).  When the Blender
            # addon auto-prefixed it with author, strip that here so
            # the granny_name has exactly one author segment.
            stem = action_name
            if author_prefix and stem.startswith(author_prefix):
                stem = stem[len(author_prefix):]
            d = {
                'granny_name': f"{character}_{author_slug}_{stem}_C_00",
                'source_glb_action': action_name,
            }
            if default_template:
                d['clone_from'] = default_template
            return d

        added = 0
        merged = 0
        for action_name in discovered_new:
            entry = existing_by_logical.get(action_name)
            if entry is None:
                entry = {'logical_name': action_name}
                entry.update(_build_defaults(action_name))
                existing.append(entry)
                added += 1
                continue
            # Entry already declared by the addon (or by hand) — fill in
            # any fields the modder didn't set without overwriting their
            # values.  Lets the Blender addon write a minimal entry
            # ({logical_name, loop}) and have the builder complete it.
            defaults = _build_defaults(action_name)
            entry_changed = False
            for k, v in defaults.items():
                if not entry.get(k):
                    entry[k] = v
                    entry_changed = True
            if entry_changed:
                merged += 1
        if added or merged:
            target['new_animations'] = existing
            changed = True
            if added:
                print(f"  detected {added} new animation(s) "
                      f"(target.new_animations populated)")
            if merged:
                print(f"  filled in defaults on {merged} pre-declared "
                      f"new_animations entr{'y' if merged == 1 else 'ies'}")
        if 'animation_add' not in types:
            types.add('animation_add')
            changed = True
    elif (target.get('new_animations') or []) and 'animation_add' not in types:
        # Hand-authored mod.json with new_animations but missing type.
        types.add('animation_add')
        changed = True

    # ── Save updated mod.json ──
    if changed:
        mod['type'] = sorted(types) if len(types) > 1 else (next(iter(types)) if types else '')
        with open(mod_json_path, 'w') as f:
            json.dump(mod, f, indent=2)
        print(f"  mod.json updated: type={mod['type']}")


def build_mod(mod_dir, game_dir=None, r2_plugins_dir=None):
    """Build an H2M-compatible mod package from a mod.json directory."""
    mod_json_path = os.path.join(mod_dir, 'mod.json')
    if not os.path.isfile(mod_json_path):
        print(f"ERROR: No mod.json found in {mod_dir}")
        return False

    # Sync mod.json with actual workspace changes before building
    _sync_mod_json(mod_dir)

    with open(mod_json_path) as f:
        mod = json.load(f)

    meta = mod.get('metadata', {})
    mod_type = mod.get('type', '')
    ops = _infer_operations(mod)
    target = mod.get('target', {})
    assets = mod.get('assets', {})
    character = target.get('character', '')
    author = meta.get('author', 'Unknown')
    name = meta.get('name', 'UnnamedMod')
    mod_id = f"{author}-{name}".replace(' ', '')

    # Run conflict detection if plugins dir provided
    if r2_plugins_dir:
        detect_conflicts(mod_dir, r2_plugins_dir)

    print(f"Building: {meta.get('name', '?')} by {author}")
    print(f"  Operations: {', '.join(sorted(ops)) or mod_type}")
    print(f"  Character: {character}")

    # Auto-detect game dir
    if game_dir is None:
        game_dir = find_game_path()
    if not game_dir:
        print("ERROR: Game directory not found. Use --game-dir.")
        return False

    # Setup build output — mods are data-only (no main.lua).
    # The CG3HBuilder Thunderstore plugin handles all runtime logic.
    # A minimal plugins/ stub is needed so H2M recognizes the plugins_data/ owner.
    build_dir = os.path.join(mod_dir, 'build')
    plugins_data = os.path.join(build_dir, 'plugins_data', mod_id)
    plugins = os.path.join(build_dir, 'plugins', mod_id)
    # Wipe the mod's prior build content so renames (e.g. v3.7 PKG prefix
    # change) don't leave stale artifacts that get packaged alongside the
    # current ones — r2modman falls back to legacy extraction when the
    # plugins_data payload looks anomalous, producing double-nested installs.
    if os.path.isdir(plugins_data):
        shutil.rmtree(plugins_data)
    if os.path.isdir(plugins):
        shutil.rmtree(plugins)
    os.makedirs(plugins_data, exist_ok=True)
    os.makedirs(plugins, exist_ok=True)

    # ── Copy GLB for mesh/animation mods ──
    # GPK building is a RUNTIME job handled by CG3HBuilder plugin.
    # The mod builder only ships the GLB (+ export manifest for mesh routing).
    glb_name = assets.get('glb', '')
    glb_path = os.path.join(mod_dir, glb_name)

    has_anim_ops = 'patches_animations' in ops
    has_mesh_ops = ops & {'adds_meshes', 'replaces_meshes'}
    has_mesh_or_anim_ops = has_mesh_ops or has_anim_ops
    if has_mesh_or_anim_ops and os.path.isfile(glb_path):
        # Strip unchanged stock meshes before deploying.  Keeps the
        # runtime-builder input small (fewer meshes → faster merge),
        # and — more importantly — lets `_triage_mod_meshes` at runtime
        # classify cleanly without re-doing this work every time.  Falls
        # back to copying the full GLB if stripping isn't possible
        # (missing manifest/baseline, parse failure, etc).
        deploy_glb = os.path.join(plugins_data, glb_name)
        stripped = _strip_unchanged_data(glb_path, mod_dir)
        if stripped:
            with open(deploy_glb, 'wb') as f:
                f.write(stripped)
            print(f"\n  Included GLB: {glb_name} (stripped)")
        else:
            shutil.copy2(glb_path, deploy_glb)
            print(f"\n  Included GLB: {glb_name} (full — no strip baseline)")

        # Copy export manifest if present (for mesh name routing)
        manifest_path = os.path.join(mod_dir, 'manifest.json')
        if os.path.isfile(manifest_path):
            shutil.copy2(manifest_path, os.path.join(plugins_data, 'manifest.json'))

        # Copy baseline so the runtime triage has the position oracle.
        # Without it, `_triage_mod_meshes` falls back to manifest-names
        # and can't distinguish 'reference' from 'replaced' precisely.
        baseline_path = os.path.join(mod_dir, '.baseline_positions.npz')
        if os.path.isfile(baseline_path):
            shutil.copy2(baseline_path, os.path.join(plugins_data, '.baseline_positions.npz'))

    # ── Build PKG (if mod has custom textures) ──
    # Auto-detect textures from the GLB if not listed in mod.json
    tex_list = assets.get('textures', [])
    if not tex_list and os.path.isfile(glb_path):
        try:
            gltf = pygltflib.GLTF2().load(glb_path)

            # Load manifest to identify original meshes
            manifest_path = os.path.join(mod_dir, 'manifest.json')
            original_mesh_names = set()
            if os.path.isfile(manifest_path):
                with open(manifest_path) as mf:
                    manifest = json.load(mf)
                original_mesh_names = {m['name'] for m in manifest.get('meshes', [])}

            # Find image indices used only by NEW meshes (not original character meshes)
            new_image_indices = set()
            for mesh in gltf.meshes:
                if mesh.name in original_mesh_names:
                    continue
                for prim in mesh.primitives:
                    if prim.material is not None and prim.material < len(gltf.materials or []):
                        mat = gltf.materials[prim.material]
                        if mat.pbrMetallicRoughness and mat.pbrMetallicRoughness.baseColorTexture:
                            tex_idx = mat.pbrMetallicRoughness.baseColorTexture.index
                            if tex_idx < len(gltf.textures or []):
                                src = gltf.textures[tex_idx].source
                                if src is not None:
                                    new_image_indices.add(src)

            blob = gltf.binary_blob()
            for img_idx in new_image_indices:
                img = gltf.images[img_idx]
                if img.name and img.bufferView is not None:
                    bv = gltf.bufferViews[img.bufferView]
                    png_path = os.path.join(mod_dir, f"{img.name}.png")
                    if not os.path.isfile(png_path):
                        with open(png_path, 'wb') as pf:
                            pf.write(blob[bv.byteOffset:bv.byteOffset + bv.byteLength])
                    tex_list.append({
                        "name": img.name,
                        "file": f"{img.name}.png",
                        "custom": True,
                        "width": 512,
                        "height": 512,
                    })
            if tex_list:
                print(f"\n  Auto-detected {len(tex_list)} texture(s) from GLB")
                assets['textures'] = tex_list
                mod['assets'] = assets
                with open(mod_json_path, 'w') as f:
                    json.dump(mod, f, indent=2)
        except Exception as e:
            print(f"  Texture auto-detect warning: {e}")

    custom_textures = [t for t in tex_list if t.get('custom', False)]

    # ── v3.6: auto-prefix custom texture names with mod_id ────────────────
    # PKGs are baked per-mod and loaded into one shared texture namespace at
    # game launch.  Two mods shipping a texture with the same name would
    # collide at runtime, so we always prefix custom textures with the mod
    # id at build time.  Idempotent: skips textures already prefixed.
    texture_renames = {}
    for tex in custom_textures:
        old_name = tex.get('name', '')
        if not old_name or old_name.startswith(f"{mod_id}_"):
            continue
        new_name = f"{mod_id}_{old_name}"
        texture_renames[old_name] = new_name
        tex['name'] = new_name
    if texture_renames:
        for old, new in texture_renames.items():
            print(f"  INFO: texture {old!r} auto-prefixed -> {new!r}")

    if custom_textures:
        # PKG filename must contain a registered H2M module GUID.  Data-
        # only CG3H mods don't have a main.lua so they aren't registered,
        # but the caller (Enderclem-CG3HBuilder) is.  H2M's LoadPackages
        # validator (data.cpp) checks stem.contains(mod->guid()) against
        # every registered module — prefix with the full builder folder
        # name so that check finds Enderclem-CG3HBuilder in the stem.
        pkg_path = os.path.join(
            plugins_data, f"{CG3H_BUILDER_FOLDER}-{mod_id}.pkg")

        print(f"\n  Building standalone PKG: {len(custom_textures)} custom texture(s)")

        pkg_textures = []
        new_tex_names = []
        for tex in custom_textures:
            tex_name = tex.get('name', '')
            tex_file = tex.get('file', '')
            full_path = os.path.join(mod_dir, tex_file)
            if not os.path.isfile(full_path):
                print(f"  WARNING: texture file not found: {full_path}")
                continue
            entry_name = f"GR2\\{tex_name}"
            w = min(tex.get('width', 512), 512)
            h = min(tex.get('height', 512), 512)
            if full_path.lower().endswith('.png'):
                pkg_textures.append({
                    'name': entry_name, 'png_path': full_path,
                    'width': w, 'height': h, 'fmt': 0x1C, 'mip_count': 6,
                })
            elif full_path.lower().endswith('.dds'):
                pkg_textures.append({'name': entry_name, 'dds_path': full_path})
            new_tex_names.append(tex_name)
        if pkg_textures:
            build_standalone_pkg(pkg_textures, pkg_path)

    # Write the (possibly texture-renamed) mod.json to the build output.  We
    # rewrite from the in-memory `mod` dict instead of copying the source so
    # the v3.6 texture auto-prefix never touches the modder's source files.
    with open(os.path.join(plugins_data, 'mod.json'), 'w') as f:
        json.dump(mod, f, indent=2)

    # Mutate the shipped GLB's image names to match the renamed textures so
    # the runtime converter (gltf_to_gr2.convert) reads the prefixed name and
    # writes the matching FromFileName into the GR2 material chain.
    if texture_renames and has_mesh_or_anim_ops:
        shipped_glb = os.path.join(plugins_data, glb_name)
        if os.path.isfile(shipped_glb):
            try:
                gltf = pygltflib.GLTF2().load(shipped_glb)
                renamed = 0
                for img in (gltf.images or []):
                    if img.name and img.name in texture_renames:
                        img.name = texture_renames[img.name]
                        renamed += 1
                if renamed:
                    gltf.save(shipped_glb)
                    print(f"  Renamed {renamed} GLB image(s) to match prefixed textures")
            except Exception as e:
                print(f"  WARNING: failed to rename GLB images for texture dedup: {e}")

    # Write minimal plugin stub (manifest only — no main.lua).
    # H2M requires plugins/{id}/ to exist as owner of plugins_data/{id}/.
    h2m_manifest = {
        "name": name,
        "version_number": meta.get('version', '1.0.0'),
        "website_url": meta.get('url', ''),
        "description": meta.get('description', ''),
        "dependencies": [
            H2M_DEPENDENCY,
            CG3H_BUILDER_DEPENDENCY,
        ],
    }
    with open(os.path.join(plugins, 'manifest.json'), 'w') as f:
        json.dump(h2m_manifest, f, indent=2)

    print("\n  Build complete!")
    print(f"  Output: {build_dir}")
    return build_dir


def package_thunderstore(mod_dir):
    """Create a Thunderstore-ready ZIP from the built mod."""
    mod_json_path = os.path.join(mod_dir, 'mod.json')
    with open(mod_json_path) as f:
        mod = json.load(f)

    meta = mod.get('metadata', {})
    author = meta.get('author', 'Unknown')
    name = meta.get('name', 'UnnamedMod').replace(' ', '')
    version = meta.get('version', '1.0.0')

    build_dir = os.path.join(mod_dir, 'build')
    if not os.path.isdir(build_dir):
        print("ERROR: Run build first")
        return False

    mod_id = f"{author}-{name}".replace(' ', '')
    zip_name = f"{author}-{name}-{version}"
    zip_path = os.path.join(mod_dir, zip_name)

    # Thunderstore expects: manifest.json, icon.png, README.md at root + plugins/ + plugins_data/
    with zipfile.ZipFile(zip_path + '.zip', 'w', zipfile.ZIP_DEFLATED) as zf:
        # Root manifest (Thunderstore format)
        ts_manifest = {
            "name": name,
            "version_number": version,
            "website_url": meta.get('url', ''),
            "description": meta.get('description', ''),
            "dependencies": [
                H2M_DEPENDENCY,
                CG3H_BUILDER_DEPENDENCY,
            ],
        }
        zf.writestr('manifest.json', json.dumps(ts_manifest, indent=2))

        # Icon — mod's own > alongside this script (Blender addon
        # bundles icon.png here) > repo root (CLI invocation case).
        # Missing icon is a hard error: r2modman treats icon-less ZIPs
        # as legacy (non-Thunderstore) packages and double-nests their
        # extraction under plugins_data/<mod>/<mod>/, which breaks both
        # CG3HBuilder's scan and the H2M plugin loader.  We'd rather
        # fail the build loudly than ship a quietly-broken ZIP.
        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(mod_dir, meta.get('preview', 'icon.png')),
            os.path.join(mod_dir, 'icon.png'),
            os.path.join(script_dir, 'icon.png'),         # bundled alongside this script
            os.path.join(script_dir, '..', 'icon.png'),   # repo root (CLI case)
        ]
        icon_path = next((c for c in candidates if os.path.isfile(c)), None)
        if not icon_path:
            raise SystemExit(
                "ERROR: no icon.png found for Thunderstore package.\n"
                "  Searched (in order):\n    "
                + "\n    ".join(candidates)
                + "\n  Every ZIP needs a root icon.png or r2modman "
                "mis-extracts it.  Drop any 256x256 PNG at any of the "
                "paths above (the repo's default icon.png is the usual "
                "fallback)."
            )
        zf.write(icon_path, 'icon.png')

        # README
        readme_path = os.path.join(mod_dir, 'README.md')
        if os.path.isfile(readme_path):
            zf.write(readme_path, 'README.md')
        else:
            zf.writestr('README.md', f'# {name}\n\n{meta.get("description", "")}\n')

        # Include GLB in plugins_data/ (stripped if possible).
        # Prefer the GLB from build_dir/plugins_data/ — that's the copy with
        # v3.6 texture-name prefixes applied.  Fall back to source if absent.
        pd_prefix = f'plugins_data/{mod_id}'
        assets_cfg = mod.get('assets', {})
        glb = assets_cfg.get('glb', '')
        glb_arc_path = None
        if glb:
            renamed_glb = os.path.join(build_dir, 'plugins_data', mod_id, glb)
            glb_full = renamed_glb if os.path.isfile(renamed_glb) else os.path.join(mod_dir, glb)
            if os.path.isfile(glb_full):
                glb_arc_path = f'{pd_prefix}/{glb}'
                stripped = _strip_unchanged_data(glb_full, mod_dir)
                if stripped:
                    zf.writestr(glb_arc_path, stripped)
                    print("  Packaged GLB: unchanged data stripped")
                else:
                    zf.write(glb_full, glb_arc_path)
                    print("  Packaged GLB: full (no manifest for stripping)")

        # Build output — exclude .gpk files (contain original game data)
        # and skip any file already added above (stripped GLB)
        for root, dirs, files in os.walk(build_dir):
            for f in files:
                if f.endswith('.gpk') or f.startswith('.baseline_'):
                    continue  # CC content / build artifacts — not distributed
                full = os.path.join(root, f)
                arc = os.path.relpath(full, build_dir)
                if glb_arc_path and arc.replace('\\', '/') == glb_arc_path:
                    continue  # already added (possibly stripped)
                zf.write(full, arc)

    print(f"\n  Thunderstore package: {zip_path}.zip")
    return True


def main():
    parser = argparse.ArgumentParser(description="CG3H Build — H2M mod builder")
    parser.add_argument('mod_dir', help='Directory containing mod.json')
    parser.add_argument('--game-dir', default=None,
                        help='Hades II game directory (auto-detected from Steam)')
    parser.add_argument('--package', action='store_true',
                        help='Also create Thunderstore ZIP')
    parser.add_argument('--r2-plugins-dir', default=None,
                        help='r2modman plugins directory for conflict detection')
    parser.add_argument('--check-conflicts', action='store_true',
                        help='Only check for conflicts, do not build')
    args = parser.parse_args()

    if args.check_conflicts:
        r2_dir = args.r2_plugins_dir
        if not r2_dir:
            print("ERROR: --r2-plugins-dir is required with --check-conflicts")
            sys.exit(1)
        safe = detect_conflicts(args.mod_dir, r2_dir)
        sys.exit(0 if safe else 1)

    ok = build_mod(args.mod_dir, game_dir=args.game_dir,
                   r2_plugins_dir=args.r2_plugins_dir)
    if ok and args.package:
        package_thunderstore(args.mod_dir)


if __name__ == '__main__':
    main()
