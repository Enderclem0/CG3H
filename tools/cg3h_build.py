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
import json
import os
import shutil
import sys

_tools_dir = os.path.dirname(os.path.abspath(__file__))
if _tools_dir not in sys.path:
    sys.path.insert(0, _tools_dir)

STEAM_PATHS = [
    "C:/Program Files (x86)/Steam/steamapps/common/Hades II",
    "C:/Program Files/Steam/steamapps/common/Hades II",
    "D:/Steam/steamapps/common/Hades II",
    "D:/SteamLibrary/steamapps/common/Hades II",
    "E:/SteamLibrary/steamapps/common/Hades II",
]


def _strip_unchanged_data(glb_path, mod_dir):
    """
    Strip unchanged meshes and textures from a GLB for distribution.
    Compares against the original export to detect what actually changed.
    Keeps: new meshes, edited meshes, new textures, edited textures.
    Removes: unmodified meshes, unmodified textures, skeleton (rebuilt from game).
    Returns stripped GLB bytes, or None if nothing changed or stripping failed.
    """
    try:
        import pygltflib
        import numpy as np
        import hashlib

        manifest_path = os.path.join(mod_dir, 'manifest.json')
        if not os.path.isfile(manifest_path):
            return None

        with open(manifest_path) as f:
            manifest = json.load(f)

        # Load the modified GLB
        gltf = pygltflib.GLTF2().load(glb_path)
        blob = gltf.binary_blob()

        # Build original mesh data hashes from manifest
        # We hash vertex data to detect changes — if hash matches original, mesh is unmodified
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

            # Check if mesh data changed by hashing vertex positions
            for prim in mesh.primitives:
                if prim.attributes.POSITION is not None:
                    acc = gltf.accessors[prim.attributes.POSITION]
                    bv = gltf.bufferViews[acc.bufferView]
                    data = blob[bv.byteOffset:bv.byteOffset + bv.byteLength]
                    # If vertex count changed from export, mesh was edited
                    orig_mesh = next((m for m in manifest.get('meshes', [])
                                      if m['name'] == mesh.name), None)
                    if orig_mesh:
                        # Can't compare vertex data without the original GLB,
                        # but vertex count change is a strong signal
                        if acc.count != orig_mesh.get('vertex_count', acc.count):
                            keep_indices.append(i)
                            break
                    # Check if indices changed (topology)
                    if prim.indices is not None:
                        idx_acc = gltf.accessors[prim.indices]
                        if orig_mesh and idx_acc.count != orig_mesh.get('index_count', idx_acc.count):
                            keep_indices.append(i)
                            break

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
            print(f"  No changes detected — nothing to strip")
            return None

        # Rebuild GLB with only kept meshes
        new_meshes = [gltf.meshes[i] for i in sorted(set(keep_indices))]
        old_to_new = {old: new for new, old in enumerate(sorted(set(keep_indices)))}

        for node in gltf.nodes:
            if node.mesh is not None:
                if node.mesh in old_to_new:
                    node.mesh = old_to_new[node.mesh]
                else:
                    node.mesh = None

        gltf.meshes = new_meshes

        # Strip unchanged animations
        if gltf.animations and original_anim_hashes:
            gltf.animations = [gltf.animations[i] for i in keep_anims]

        kept_names = [m.name for m in new_meshes]
        if kept_names:
            print(f"  Keeping {len(new_meshes)} mesh(es): {', '.join(kept_names[:5])}")
        print(f"  Stripped {len(original_names) - len(keep_indices)} unchanged mesh(es)")
        if stripped_anims:
            print(f"  Stripped {stripped_anims} unchanged animation(s), "
                  f"keeping {len(keep_anims)}")

        import io
        buf = io.BytesIO()
        gltf.save(buf)
        return buf.getvalue()

    except Exception as e:
        import traceback
        print(f"  Strip failed: {e}")
        traceback.print_exc()
        return None


def _infer_operations(mod):
    """
    Infer what operations a mod performs from its assets.
    Returns a set of operations: {'adds_meshes', 'replaces_meshes', 'patches_meshes',
                                   'replaces_textures', 'adds_textures',
                                   'patches_animations'}
    """
    ops = set()
    mod_type = mod.get('type', '')
    assets = mod.get('assets', {})
    meshes = mod.get('meshes', [])
    textures = assets.get('textures', [])

    # Explicit type (backwards compat)
    if isinstance(mod_type, list):
        for t in mod_type:
            if t == 'mesh_add': ops.add('adds_meshes')
            elif t == 'mesh_replace': ops.add('replaces_meshes')
            elif t == 'mesh_patch': ops.add('patches_meshes')
            elif t == 'texture_replace': ops.add('replaces_textures')
            elif t == 'animation_patch': ops.add('patches_animations')
    elif mod_type:
        if mod_type == 'mesh_add': ops.add('adds_meshes')
        elif mod_type == 'mesh_replace': ops.add('replaces_meshes')
        elif mod_type == 'mesh_patch': ops.add('patches_meshes')
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
            if 'replaces_meshes' not in ops and 'patches_meshes' not in ops:
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
                    pass

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

    Compares the GLB against manifest.json to find new meshes, edited meshes,
    and changed/new textures.  Updates mod.json's type and assets.textures
    so that _infer_operations produces the correct build plan.
    """
    mod_json_path = os.path.join(mod_dir, 'mod.json')
    manifest_path = os.path.join(mod_dir, 'manifest.json')
    if not os.path.isfile(mod_json_path) or not os.path.isfile(manifest_path):
        return

    with open(mod_json_path) as f:
        mod = json.load(f)
    with open(manifest_path) as f:
        manifest = json.load(f)

    assets = mod.setdefault('assets', {})
    glb_name = assets.get('glb', '')
    glb_path = os.path.join(mod_dir, glb_name) if glb_name else ''
    if not glb_path or not os.path.isfile(glb_path):
        return

    changed = False
    types = set()
    if isinstance(mod.get('type'), list):
        types = set(mod['type'])
    elif mod.get('type'):
        types = {mod['type']}

    # ── Detect mesh changes ──
    manifest_meshes = {m['name']: m for m in manifest.get('meshes', [])}
    if manifest_meshes:
        try:
            import pygltflib
            import hashlib

            gltf = pygltflib.GLTF2().load(glb_path)
            blob = gltf.binary_blob()

            has_new = False
            has_edited = False
            for mesh in (gltf.meshes or []):
                if not mesh.name:
                    continue
                orig = manifest_meshes.get(mesh.name)
                if orig is None:
                    has_new = True
                    continue
                for prim in mesh.primitives:
                    if prim.attributes.POSITION is not None:
                        acc = gltf.accessors[prim.attributes.POSITION]
                        if acc.count != orig.get('vertex_count', acc.count):
                            has_edited = True
                        else:
                            bv = gltf.bufferViews[acc.bufferView]
                            data = blob[bv.byteOffset:bv.byteOffset + bv.byteLength]
                            if hashlib.md5(data).hexdigest() != orig.get('position_hash', ''):
                                has_edited = True
                    if prim.indices is not None:
                        idx_acc = gltf.accessors[prim.indices]
                        if idx_acc.count != orig.get('index_count', idx_acc.count):
                            has_edited = True
                    if has_edited:
                        break

            if has_new and 'mesh_add' not in types:
                types.add('mesh_add')
                changed = True
            if has_edited and 'mesh_replace' not in types:
                types.add('mesh_replace')
                changed = True
        except Exception:
            pass

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
        except Exception:
            pass

    # ── Pick up custom textures from new meshes ──
    # The converter writes *_custom_textures.json; also check build output
    character = mod.get('target', {}).get('character', '')
    for search_dir in [mod_dir, os.path.join(mod_dir, 'build')]:
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
                        pass

    # ── Remove stale texture_replace if nothing left ──
    if types - {'texture_replace'} and not assets.get('textures'):
        types.discard('texture_replace')

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
        for p in STEAM_PATHS:
            if os.path.isdir(p):
                game_dir = p
                break
    if not game_dir:
        print("ERROR: Game directory not found. Use --game-dir.")
        return False

    ship_dir = os.path.join(game_dir, "Ship")
    gpk_dir = os.path.join(game_dir, "Content", "GR2", "_Optimized")
    dll_path = os.path.join(ship_dir, "granny2_x64.dll")

    # Setup build output — mods are data-only (no main.lua).
    # The CG3HBuilder Thunderstore plugin handles all runtime logic.
    # A minimal plugins/ stub is needed so H2M recognizes the plugins_data/ owner.
    build_dir = os.path.join(mod_dir, 'build')
    plugins_data = os.path.join(build_dir, 'plugins_data', mod_id)
    plugins = os.path.join(build_dir, 'plugins', mod_id)
    os.makedirs(plugins_data, exist_ok=True)
    os.makedirs(plugins, exist_ok=True)

    # ── Copy GLB for mesh/animation mods ──
    # GPK building is a RUNTIME job handled by CG3HBuilder plugin.
    # The mod builder only ships the GLB (+ export manifest for mesh routing).
    glb_name = assets.get('glb', '')
    glb_path = os.path.join(mod_dir, glb_name)

    has_anim_ops = 'patches_animations' in ops
    has_mesh_ops = ops & {'adds_meshes', 'replaces_meshes', 'patches_meshes'}
    has_mesh_or_anim_ops = has_mesh_ops or has_anim_ops
    if has_mesh_or_anim_ops and os.path.isfile(glb_path):
        import shutil
        shutil.copy2(glb_path, os.path.join(plugins_data, glb_name))
        print(f"\n  Included GLB: {glb_name}")

        # Copy export manifest if present (for mesh name routing)
        manifest_path = os.path.join(mod_dir, 'manifest.json')
        if os.path.isfile(manifest_path):
            shutil.copy2(manifest_path, os.path.join(plugins_data, 'manifest.json'))

    # ── Build PKG + register in manifest (if mod has custom textures) ──
    # Two steps are needed for new texture names:
    #   1. Manifest registration — the game only creates texture handles for
    #      entries listed in the .pkg_manifest.  Without this, new texture
    #      names are never registered and LoadPackages has nothing to fill.
    #   2. Standalone PKG — provides the actual pixel data, loaded via
    #      LoadPackages in the Lua companion.
    # For REPLACEMENT textures (same name as existing), only step 2 is needed
    # since the hash is already registered from the original manifest.
    tex_list = assets.get('textures', [])
    custom_textures = [t for t in tex_list if t.get('custom', True)]

    if custom_textures:
        pkg_dir = os.path.join(game_dir, "Content", "Packages", "1080p")
        # Name PKG with CG3HBuilder prefix so the builder plugin can call
        # LoadPackages on it (H2M validates the calling plugin's GUID is in the filename)
        pkg_path = os.path.join(plugins_data, f"CG3HBuilder-{mod_id}.pkg")

        from pkg_texture import build_standalone_pkg, png_to_dds, add_manifest_entry

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

    # Copy mod.json to build output (the CG3HBuilder plugin discovers mods via this file)
    import shutil
    shutil.copy2(os.path.join(mod_dir, 'mod.json'), os.path.join(plugins_data, 'mod.json'))

    # Write minimal plugin stub (manifest only — no main.lua).
    # H2M requires plugins/{id}/ to exist as owner of plugins_data/{id}/.
    h2m_manifest = {
        "name": name,
        "version_number": meta.get('version', '1.0.0'),
        "website_url": meta.get('url', ''),
        "description": meta.get('description', ''),
        "dependencies": [
            "Hell2Modding-Hell2Modding-0.2.0",
            "Enderclem-CG3HBuilder-3.0.0",
        ],
    }
    with open(os.path.join(plugins, 'manifest.json'), 'w') as f:
        json.dump(h2m_manifest, f, indent=2)

    print(f"\n  Build complete!")
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
    import zipfile
    with zipfile.ZipFile(zip_path + '.zip', 'w', zipfile.ZIP_DEFLATED) as zf:
        # Root manifest (Thunderstore format)
        ts_manifest = {
            "name": name,
            "version_number": version,
            "website_url": meta.get('url', ''),
            "description": meta.get('description', ''),
            "dependencies": [
                "Hell2Modding-Hell2Modding-0.2.0",
                "Enderclem-CG3HBuilder-3.0.0",
            ],
        }
        zf.writestr('manifest.json', json.dumps(ts_manifest, indent=2))

        # Icon
        icon_path = os.path.join(mod_dir, meta.get('preview', 'icon.png'))
        if os.path.isfile(icon_path):
            zf.write(icon_path, 'icon.png')

        # README
        readme_path = os.path.join(mod_dir, 'README.md')
        if os.path.isfile(readme_path):
            zf.write(readme_path, 'README.md')
        else:
            zf.writestr('README.md', f'# {name}\n\n{meta.get("description", "")}\n')

        # Build output — exclude .gpk files (contain original game data)
        for root, dirs, files in os.walk(build_dir):
            for f in files:
                if f.endswith('.gpk'):
                    continue  # CC content — must be built on user's machine
                full = os.path.join(root, f)
                arc = os.path.relpath(full, build_dir)
                zf.write(full, arc)

        # Include GLB in plugins_data/ (stripped if possible)
        pd_prefix = f'plugins_data/{mod_id}'
        assets_cfg = mod.get('assets', {})
        glb = assets_cfg.get('glb', '')
        if glb:
            glb_full = os.path.join(mod_dir, glb)
            if os.path.isfile(glb_full):
                stripped = _strip_unchanged_data(glb_full, mod_dir)
                if stripped:
                    zf.writestr(f'{pd_prefix}/{glb}', stripped)
                    print(f"  Packaged GLB: unchanged data stripped")
                else:
                    zf.write(glb_full, f'{pd_prefix}/{glb}')
                    print(f"  WARNING: Could not strip unchanged data")

        # Include export manifest if present (for mesh routing)
        manifest_file = os.path.join(mod_dir, 'manifest.json')
        if os.path.isfile(manifest_file):
            zf.write(manifest_file, f'{pd_prefix}/manifest.json')

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
