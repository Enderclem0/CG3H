"""
CG3H Runtime Builder — builds GPKs from mod assets + user's game files.

Called by the CG3HBuilder H2M plugin at game launch.
Reads mod.json from the given directory, finds the GLB and game files,
and calls gltf_to_gr2.convert() to produce {character}.gpk.

Usage:
    cg3h_builder.exe <mod_dir>
    python cg3h_builder_entry.py <mod_dir>
"""
import datetime
import json
import os
import sys
import time
import traceback

import pygltflib

# Schema version for cg3h_status.json.  Bump when fields change in
# backwards-incompatible ways so the Lua reader can degrade gracefully.
CG3H_STATUS_SCHEMA_VERSION = 1


def _sanitize_mod_id(mod_id):
    """Produce a Granny-safe identifier from a Thunderstore mod id.

    Thunderstore mod ids are ``Author-ModName`` (hyphenated).  The game's
    model loader treats entry names with long/separator-heavy prefixes
    unreliably (observed weapon-model corruption with ``___`` in the name).
    Replace hyphens with underscores and strip whitespace so the variant
    entry matches the game's own ``{Tag}_Mesh`` convention.
    """
    return mod_id.replace('-', '_').replace(' ', '_')


def _variant_entry_name(character, mod_id, scene_index):
    """Canonical variant entry name: ``{Character}_{sanitized_mod_id}_V{N}_Mesh``.

    The leading character name is critical — Hades's model loader uses the
    entry name prefix to link the mesh to the right skeleton / bone-matrix
    set at draw time.  Without the prefix, the variant renders for one
    frame (cached draw state) and then hangs on the next (bone-matrix
    lookup fails).

    ``scene_index`` is 0, 1, 2... — one per stock entry the mod targets.
    The (stock_entry, mod_id, variant_name) mapping is recorded under
    ``characters[*].variants`` in cg3h_status.json so the runtime can
    look it up without parsing the name.
    """
    return f"{character}_{_sanitize_mod_id(mod_id)}_V{scene_index}_Mesh"

# Schema version for cg3h_mod_state.json (user-writable per-mod state —
# enabled flag etc.).  Separate file from cg3h_status.json because one is
# builder output and one is user input.
CG3H_MOD_STATE_SCHEMA_VERSION = 1


def _classify_mod(mod):
    """Single source of truth for v3.9 mod classification.

    Returns (is_variant, is_accessory).  Both can be False (e.g. a pure
    mesh_patch mod).  Both can NOT be True — mesh_add is the dominant
    signal (any mesh_add presence forces additive).

    Rule:
      - PURE mesh_replace (mesh_replace in type, mesh_add NOT in type)
        and non-empty target.mesh_entries → picker variant.
      - Anything with mesh_add in type (including mixed with
        mesh_replace) → additive accessory.
    """
    mod_type = mod.get('type', '')
    types = mod_type if isinstance(mod_type, list) \
        else [mod_type] if mod_type else []
    has_entries = bool(mod.get('target', {}).get('mesh_entries', []))
    is_pure_replacer = 'mesh_replace' in types and 'mesh_add' not in types
    is_variant = is_pure_replacer and has_entries
    is_accessory = 'mesh_add' in types
    return is_variant, is_accessory


def _load_mod_state(builder_dir):
    """Read cg3h_mod_state.json.  Missing / unreadable file → empty dict."""
    path = os.path.join(builder_dir, "cg3h_mod_state.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
        return doc.get("mods", {})
    except (OSError, json.JSONDecodeError) as e:
        print(f"  WARNING: could not read {path}: {e}")
        return {}


def _is_mod_enabled(mod_state, mod_id):
    """Default to enabled if the mod has no state entry."""
    entry = mod_state.get(mod_id)
    if not entry:
        return True
    return entry.get("enabled", True)

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)
if sys.stdout and sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from cg3h_constants import CG3H_VERSION
from cg3h_constants import find_game_path as _find_game_path
from gltf_to_gr2 import convert


def _merge_manifests(char_mods, collisions=None):
    """Merge manifest.json from all mods targeting the same character.

    Returns combined manifest dict with union of mesh entries and mesh mappings,
    or None if no manifests exist.  If collisions is provided (set of mesh names
    that appear in multiple mods), mesh names are prefixed with the mod id.
    """
    merged = {'meshes': [], 'mesh_entries': []}
    seen_meshes = set()
    seen_entries = set()

    for mod_info in char_mods:
        mp = mod_info.get('manifest_path', '')
        if not mp or not os.path.isfile(mp):
            continue
        with open(mp) as f:
            m = json.load(f)
        for entry in m.get('mesh_entries', []):
            if entry not in seen_entries:
                merged['mesh_entries'].append(entry)
                seen_entries.add(entry)
        for mesh in m.get('meshes', []):
            name = mesh['name']
            if collisions and name in collisions:
                mesh = dict(mesh)
                mesh['name'] = f"{mod_info['id']}_{name}"
                name = mesh['name']
            if name not in seen_meshes:
                merged['meshes'].append(mesh)
                seen_meshes.add(name)

    return merged if merged['meshes'] else None


def _copy_accessor(base_gltf, base_blob, other_gltf, other_blob,
                   acc_idx, bv_offset_map, acc_offset_map):
    """Copy a single accessor (and its BufferView data) from other into base.

    Returns the new accessor index in base_gltf.  Reuses already-copied
    BufferViews/Accessors via the offset maps (mutated in place).
    """
    if acc_idx in acc_offset_map:
        return acc_offset_map[acc_idx]

    acc = other_gltf.accessors[acc_idx]
    bv_idx = acc.bufferView
    if bv_idx not in bv_offset_map:
        bv = other_gltf.bufferViews[bv_idx]
        new_offset = len(base_blob)
        base_blob.extend(other_blob[bv.byteOffset:bv.byteOffset + bv.byteLength])
        new_bv = pygltflib.BufferView(
            buffer=0, byteOffset=new_offset, byteLength=bv.byteLength,
            byteStride=bv.byteStride, target=bv.target)
        new_bv_idx = len(base_gltf.bufferViews)
        base_gltf.bufferViews.append(new_bv)
        bv_offset_map[bv_idx] = new_bv_idx

    new_acc = pygltflib.Accessor(
        bufferView=bv_offset_map[bv_idx],
        byteOffset=acc.byteOffset,
        componentType=acc.componentType,
        count=acc.count,
        type=acc.type,
        max=acc.max, min=acc.min)
    new_acc_idx = len(base_gltf.accessors)
    base_gltf.accessors.append(new_acc)
    acc_offset_map[acc_idx] = new_acc_idx
    return new_acc_idx


def _merge_animations(base_gltf, base_blob, other_gltf, other_blob,
                      bv_offset_map, acc_offset_map, mod_id):
    """Copy animations from other_gltf into base_gltf with node remapping."""

    if not other_gltf.animations:
        return

    # Build name→index map for the current merged node list
    bone_map = {node.name: i for i, node in enumerate(base_gltf.nodes)}

    # Track existing animation names for dedup
    existing_names = {}
    if base_gltf.animations:
        for i, a in enumerate(base_gltf.animations):
            existing_names[a.name] = i

    for anim in other_gltf.animations:
        channels = []
        samplers = []

        for ch in anim.channels:
            if ch.target.node is None or ch.target.node >= len(other_gltf.nodes):
                continue
            bone_name = other_gltf.nodes[ch.target.node].name
            new_node_idx = bone_map.get(bone_name)
            if new_node_idx is None:
                print(f"    WARNING: animation '{anim.name}' targets bone "
                      f"'{bone_name}' not found in merged skeleton, skipping channel")
                continue

            # Copy sampler data
            old_sampler = anim.samplers[ch.sampler]
            new_input = _copy_accessor(base_gltf, base_blob, other_gltf, other_blob,
                                       old_sampler.input, bv_offset_map, acc_offset_map)
            new_output = _copy_accessor(base_gltf, base_blob, other_gltf, other_blob,
                                        old_sampler.output, bv_offset_map, acc_offset_map)
            sampler_idx = len(samplers)
            samplers.append(pygltflib.AnimationSampler(
                input=new_input, output=new_output,
                interpolation=old_sampler.interpolation))
            channels.append(pygltflib.AnimationChannel(
                sampler=sampler_idx,
                target=pygltflib.AnimationChannelTarget(
                    node=new_node_idx, path=ch.target.path)))

        if not channels:
            continue

        new_anim = pygltflib.Animation(
            name=anim.name, channels=channels, samplers=samplers,
            extras=anim.extras)

        # Dedup: overwrite existing animation with same name (last wins)
        if anim.name in existing_names:
            idx = existing_names[anim.name]
            print(f"    WARNING: animation '{anim.name}' from {mod_id} "
                  f"overwrites existing (last mod wins)")
            base_gltf.animations[idx] = new_anim
        else:
            if base_gltf.animations is None:
                base_gltf.animations = []
            existing_names[anim.name] = len(base_gltf.animations)
            base_gltf.animations.append(new_anim)


def _merge_glbs(char_mods, output_dir, character):
    """Merge meshes, materials, textures, and animations from multiple GLBs.

    Takes the first mod's GLB as the base, then appends NEW meshes and
    animations from subsequent mods.  When two mods use the same mesh name,
    both are kept with a ``{mod_id}_`` prefix (collision-only deduplication).

    Returns (merged_path, collisions) where collisions is a set of original
    mesh names that appeared in multiple mods and were prefixed.
    """
    # ── Pre-scan: detect mesh name collisions across all mods ──
    name_to_mods = {}
    for mod_info in char_mods:
        gltf = pygltflib.GLTF2().load(mod_info['glb_path'])
        for mesh in gltf.meshes:
            name_to_mods.setdefault(mesh.name, []).append(mod_info['id'])
    collisions = {name for name, mods in name_to_mods.items() if len(mods) > 1}

    if collisions:
        for name in sorted(collisions):
            mods_list = name_to_mods[name]
            print(f"    WARNING: mesh '{name}' used by {', '.join(mods_list)} "
                  f"— renaming to avoid collision")

    # ── Load base GLB ──
    base_mod = char_mods[0]
    base_gltf = pygltflib.GLTF2().load(base_mod['glb_path'])

    # Retroactive rename of base mod's colliding meshes
    if collisions:
        for i, mesh in enumerate(base_gltf.meshes):
            if mesh.name in collisions:
                new_name = f"{base_mod['id']}_{mesh.name}"
                mesh.name = new_name
                for node in base_gltf.nodes:
                    if node.mesh == i:
                        node.name = new_name
                        break

    base_mesh_names = {m.name for m in base_gltf.meshes}

    # ── Merge each subsequent mod ──
    for mod_info in char_mods[1:]:
        other_gltf = pygltflib.GLTF2().load(mod_info['glb_path'])
        other_blob = other_gltf.binary_blob()
        base_blob = bytearray(base_gltf.binary_blob())

        bv_offset_map = {}
        acc_offset_map = {}

        # Copy images/textures/materials FIRST so we know the material offset
        mat_offset = len(base_gltf.materials or [])
        img_offset = len(base_gltf.images or [])
        tex_offset = len(base_gltf.textures or [])

        if other_gltf.images:
            for img in other_gltf.images:
                if img.bufferView is not None:
                    bv = other_gltf.bufferViews[img.bufferView]
                    new_offset = len(base_blob)
                    base_blob.extend(other_blob[bv.byteOffset:bv.byteOffset + bv.byteLength])
                    new_bv = pygltflib.BufferView(buffer=0, byteOffset=new_offset, byteLength=bv.byteLength)
                    new_bv_idx = len(base_gltf.bufferViews)
                    base_gltf.bufferViews.append(new_bv)
                    new_img = pygltflib.Image(name=img.name, mimeType=img.mimeType, bufferView=new_bv_idx)
                else:
                    new_img = pygltflib.Image(name=img.name, uri=img.uri, mimeType=img.mimeType)
                if base_gltf.images is None:
                    base_gltf.images = []
                base_gltf.images.append(new_img)

            for tex in (other_gltf.textures or []):
                new_tex = pygltflib.Texture(source=tex.source + img_offset if tex.source is not None else None)
                if base_gltf.textures is None:
                    base_gltf.textures = []
                base_gltf.textures.append(new_tex)

        # Materials copied outside the images block — untextured materials are valid
        for mat in (other_gltf.materials or []):
            new_mat = pygltflib.Material(name=mat.name)
            if mat.pbrMetallicRoughness and mat.pbrMetallicRoughness.baseColorTexture:
                new_mat.pbrMetallicRoughness = pygltflib.PbrMetallicRoughness(
                    baseColorTexture=pygltflib.TextureInfo(
                        index=mat.pbrMetallicRoughness.baseColorTexture.index + tex_offset))
            if base_gltf.materials is None:
                base_gltf.materials = []
            base_gltf.materials.append(new_mat)

        # Copy new meshes with correct material remapping
        for mesh in other_gltf.meshes:
            original_name = mesh.name
            merge_name = original_name
            if original_name in collisions:
                merge_name = f"{mod_info['id']}_{original_name}"

            if merge_name in base_mesh_names:
                # Shouldn't happen after dedup, but guard against it
                print(f"    WARNING: mesh '{merge_name}' already exists, skipping")
                continue

            for prim in mesh.primitives:
                acc_indices = []
                if prim.attributes.POSITION is not None:
                    acc_indices.append(prim.attributes.POSITION)
                if prim.attributes.NORMAL is not None:
                    acc_indices.append(prim.attributes.NORMAL)
                if prim.attributes.TEXCOORD_0 is not None:
                    acc_indices.append(prim.attributes.TEXCOORD_0)
                if prim.attributes.JOINTS_0 is not None:
                    acc_indices.append(prim.attributes.JOINTS_0)
                if prim.attributes.WEIGHTS_0 is not None:
                    acc_indices.append(prim.attributes.WEIGHTS_0)
                if prim.indices is not None:
                    acc_indices.append(prim.indices)

                for acc_idx in acc_indices:
                    _copy_accessor(base_gltf, base_blob, other_gltf, other_blob,
                                   acc_idx, bv_offset_map, acc_offset_map)

            # Build all primitives with remapped accessors and materials
            new_prims = []
            for prim in mesh.primitives:
                new_mat_idx = None
                if prim.material is not None:
                    new_mat_idx = prim.material + mat_offset
                new_prims.append(pygltflib.Primitive(
                    attributes=pygltflib.Attributes(
                        POSITION=acc_offset_map.get(prim.attributes.POSITION),
                        NORMAL=acc_offset_map.get(prim.attributes.NORMAL),
                        TEXCOORD_0=acc_offset_map.get(prim.attributes.TEXCOORD_0),
                        JOINTS_0=acc_offset_map.get(prim.attributes.JOINTS_0),
                        WEIGHTS_0=acc_offset_map.get(prim.attributes.WEIGHTS_0),
                    ),
                    indices=acc_offset_map.get(prim.indices),
                    material=new_mat_idx,
                ))

            new_mesh = pygltflib.Mesh(name=merge_name, primitives=new_prims)
            new_mesh_idx = len(base_gltf.meshes)
            base_gltf.meshes.append(new_mesh)
            base_mesh_names.add(merge_name)

            new_node = pygltflib.Node(name=merge_name, mesh=new_mesh_idx)
            if base_gltf.skins:
                new_node.skin = 0
            base_gltf.nodes.append(new_node)
            if base_gltf.scenes:
                base_gltf.scenes[0].nodes.append(len(base_gltf.nodes) - 1)

        # Merge animations from this mod
        _merge_animations(base_gltf, base_blob, other_gltf, other_blob,
                          bv_offset_map, acc_offset_map, mod_info['id'])

        # Update buffer size
        base_gltf.set_binary_blob(bytes(base_blob))
        base_gltf.buffers[0].byteLength = len(base_blob)

    # Save merged GLB
    merged_path = os.path.join(output_dir, f"{character}_merged.glb")
    base_gltf.save(merged_path)
    print(f"    Merged {len(base_gltf.meshes)} meshes into {merged_path}")
    return merged_path, collisions


def find_game_dir():
    return _find_game_path() or None


def build_gpk(mod_dir, game_dir=None):
    """Build a GPK from a CG3H mod directory."""
    mod_json_path = os.path.join(mod_dir, 'mod.json')
    if not os.path.isfile(mod_json_path):
        print(f"ERROR: No mod.json in {mod_dir}")
        return False

    with open(mod_json_path) as f:
        mod = json.load(f)

    character = mod.get('target', {}).get('character', '')
    if not character:
        print("ERROR: No target.character in mod.json")
        return False

    glb_name = mod.get('assets', {}).get('glb', '')
    glb_path = os.path.join(mod_dir, glb_name)
    if not glb_name or not os.path.isfile(glb_path):
        print(f"ERROR: GLB not found: {glb_path}")
        return False

    if not game_dir:
        game_dir = find_game_dir()
    if not game_dir:
        print("ERROR: Hades II game directory not found")
        return False

    gpk_path = os.path.join(game_dir, "Content", "GR2", "_Optimized", f"{character}.gpk")
    sdb_path = os.path.join(game_dir, "Content", "GR2", "_Optimized", f"{character}.sdb")
    dll_path = os.path.join(game_dir, "Ship", "granny2_x64.dll")

    for path, label in [(gpk_path, "GPK"), (sdb_path, "SDB"), (dll_path, "DLL")]:
        if not os.path.isfile(path):
            print(f"ERROR: {label} not found: {path}")
            return False

    output_gpk = os.path.join(mod_dir, f"{character}.gpk")
    manifest_path = os.path.join(mod_dir, 'manifest.json')

    # Determine operations
    mod_type = mod.get('type', '')
    types = mod_type if isinstance(mod_type, list) else [mod_type] if mod_type else []
    allow_topo = any(t in ('mesh_add', 'mesh_replace') for t in types)
    patch_anims = 'animation_patch' in types
    new_mesh_routing = mod.get('target', {}).get('new_mesh_routing')

    print(f"Building GPK: {character}")
    print(f"  GLB: {glb_path}")
    print(f"  Game: {game_dir}")

    try:
        convert(
            glb_path=glb_path,
            gpk_path=gpk_path,
            sdb_path=sdb_path,
            dll_path=dll_path,
            output_gpk=output_gpk,
            manifest_path=manifest_path if os.path.isfile(manifest_path) else None,
            allow_topology_change=allow_topo,
            patch_animations=patch_anims,
            new_mesh_routing=new_mesh_routing,
        )
        print(f"  Output: {output_gpk}")
        return True
    except Exception as e:
        print(f"ERROR: GPK build failed: {e}")
        traceback.print_exc()
        return False


def _build_variant_entries(character, variant_mods, accessory_mods,
                            output_gpk, original_gpk, sdb_path, dll_path,
                            builder_dir):
    """Build per-body variant entries and inject them into ``output_gpk``.

    For each ``mesh_replace`` (body) mod, merges the mod's GLB together
    with every ``mesh_add`` (accessory) mod's GLB so accessories stay
    visible regardless of which body the player selects at runtime.
    The merged GLB is run through ``convert()`` against the stock GPK,
    and the overwritten target entries are pulled out, renamed to
    ``{sanitized_mod_id}_V{N}_Mesh``, and injected into ``output_gpk``.

    Returns a dict mapping stock entry names to per-mod variant names,
    which the caller splices into cg3h_status.json under
    ``characters[char].variants``::

        {
          "HecateHub_Mesh": {"Enderclem-HecateBiMod": "Enderclem_HecateBiMod_V0_Mesh"},
          "HecateBattle_Mesh": {"Enderclem-HecateBiMod": "Enderclem_HecateBiMod_V1_Mesh"},
        }
    """
    if not variant_mods:
        return {}

    from gpk_pack import extract_gpk, pack_gpk

    # Read current entries in the output GPK (we inject into this in-place).
    main_entries = extract_gpk(output_gpk)

    # Per-character mapping: stock_entry -> {mod_id: variant_name}
    variants = {}

    for mod_info in variant_mods:
        mod = mod_info['mod']
        mod_id = mod_info['id']

        # List of stock entry names this mod provides a variant for, in
        # the order declared in mod.json.  That order defines the V{N} index.
        mesh_entries = mod.get('target', {}).get('mesh_entries', [])
        if not mesh_entries:
            print(f"    {mod_id}: no target.mesh_entries — skipping variant emit")
            continue

        # Merge the body's GLB with every accessory GLB so accessories
        # bake into the variant entry.  If there are no accessories, the
        # merge is a no-op (just the body).
        bundle = [mod_info] + list(accessory_mods)
        variant_glb = None
        variant_collisions = set()
        try:
            if len(bundle) == 1:
                # Single mod — no merge needed, just use its GLB directly.
                variant_glb = bundle[0]['glb_path']
            else:
                variant_glb, variant_collisions = _merge_glbs(
                    bundle, builder_dir, f"{character}_{mod_id}"
                )
        except Exception as e:
            print(f"    {mod_id}: variant GLB merge failed: {e}")
            continue
        if not variant_glb or not os.path.isfile(variant_glb):
            print(f"    {mod_id}: variant GLB merge produced no output")
            continue

        # Merged manifest + routing across body + accessories.
        variant_manifest = _merge_manifests(
            bundle, collisions=variant_collisions or None
        )
        variant_routing = {}
        for mi in bundle:
            routing = mi['mod'].get('target', {}).get('new_mesh_routing', {})
            for mesh_name, entries in routing.items():
                key = (f"{mi['id']}_{mesh_name}"
                       if variant_collisions and mesh_name in variant_collisions
                       else mesh_name)
                variant_routing[key] = entries

        # Build a temp GPK containing this mod's body + accessories.
        # `convert()` overwrites stock entries with the GLB's meshes; we
        # then pull those entries out and rename them.
        temp_gpk = os.path.join(builder_dir, f"_tmp_variant_{character}_{mod_id}.gpk")
        try:
            convert(
                glb_path=variant_glb,
                gpk_path=original_gpk,
                sdb_path=sdb_path,
                dll_path=dll_path,
                output_gpk=temp_gpk,
                manifest_dict=variant_manifest,
                allow_topology_change=True,
                patch_animations=False,
                new_mesh_routing=variant_routing or None,
            )
        except Exception as e:
            print(f"    {mod_id}: variant build failed: {e}")
            if os.path.isfile(temp_gpk):
                os.unlink(temp_gpk)
            if len(bundle) > 1 and variant_glb and os.path.isfile(variant_glb):
                os.unlink(variant_glb)
            continue
        finally:
            if len(bundle) > 1 and variant_glb and os.path.isfile(variant_glb):
                os.unlink(variant_glb)

        if not os.path.isfile(temp_gpk):
            continue

        try:
            temp_entries = extract_gpk(temp_gpk)
        except Exception as e:
            print(f"    {mod_id}: variant extract failed: {e}")
            os.unlink(temp_gpk)
            continue

        acc_tag = f" +{len(accessory_mods)} accessor(ies)" if accessory_mods else ""
        for scene_idx, entry_name in enumerate(mesh_entries):
            if entry_name not in temp_entries:
                print(f"    {mod_id}: entry '{entry_name}' missing in temp GPK — skipping")
                continue
            vname = _variant_entry_name(character, mod_id, scene_idx)
            main_entries[vname] = temp_entries[entry_name]
            variants.setdefault(entry_name, {})[mod_id] = vname
            print(f"    {mod_id}: {entry_name} -> {vname}{acc_tag}")

        try:
            os.unlink(temp_gpk)
        except OSError:
            pass

    # Emit a TRUE-STOCK variant per scene entry that at least one
    # variant_mod targets.  RAW byte-copy from the stock GPK: the merged
    # GPK preserves byte-identical stock entries for untouched entries
    # (e.g. HecateHubDream_Mesh), so raw stock bytes are compatible.
    # The earlier failure was naming ("Stock_HecateHub_Mesh" missed the
    # character prefix the engine uses for skeleton linkage); using the
    # `{Character}_Stock_V{N}_Mesh` convention fixes that.
    stock_entries_touched = set()
    for mod_info in variant_mods:
        for e in mod_info['mod'].get('target', {}).get('mesh_entries', []):
            stock_entries_touched.add(e)
    if stock_entries_touched:
        stock_gpk_entries = extract_gpk(original_gpk)
        sorted_touched = sorted(stock_entries_touched)
        for idx, stock_entry in enumerate(sorted_touched):
            if stock_entry not in stock_gpk_entries:
                print(f"    stock: '{stock_entry}' not in stock GPK — skipping")
                continue
            stock_vname = _variant_entry_name(character, "Stock", idx)
            main_entries[stock_vname] = stock_gpk_entries[stock_entry]
            variants.setdefault(stock_entry, {})["stock"] = stock_vname
            print(f"    stock: {stock_entry} -> {stock_vname} "
                  f"({len(stock_gpk_entries[stock_entry]):,} bytes, raw-copy)")

    # Repack the main GPK with the injected variant entries.
    if variants:
        pack_gpk(main_entries, output_gpk)
        print(f"  {character}: injected "
              f"{sum(len(v) for v in variants.values())} variant entr(ies)")

    return variants


def scan_and_build_all(plugins_data_dir, game_dir=None, only_character=None):
    """Scan plugins_data/ for all CG3H mods, group by character, build merged GPKs.

    Merged GPKs are output to plugins_data/CG3HBuilder/{character}.gpk.
    For multiple mods targeting the same character, each mod's GLB is applied
    sequentially to produce one merged GPK with all meshes.

    If ``only_character`` is provided, only that character is processed (the
    UI uses this for single-character rebuilds after a mod toggle).  Its
    cache key is deleted first so the rebuild always fires.
    """
    if not game_dir:
        game_dir = find_game_dir()
    if not game_dir:
        print("ERROR: Hades II game directory not found")
        return False

    # Output GPKs next to cg3h_builder.exe (handles r2modman nesting)
    # When running as PyInstaller exe, sys.executable is the exe path
    if getattr(sys, 'frozen', False):
        builder_dir = os.path.dirname(sys.executable)
    else:
        # Dev-only path (direct `python cg3h_builder_entry.py`).  Use a
        # distinct `_devbuild` subdir so we never write GPKs into a
        # folder name H2M's startup scan picks up — the real deployed
        # exe writes to `Enderclem-CG3HBuilder/` and having a second
        # `CG3HBuilder/` dir nearby causes duplicate add_granny_file
        # registrations and "Could not open Granny Packfile" errors.
        builder_dir = os.path.join(plugins_data_dir, '_cg3h_devbuild')
    os.makedirs(builder_dir, exist_ok=True)

    # v3.8: read per-mod enable/disable state.  Disabled mods are filtered
    # out of the scan so they contribute nothing to the merged GPK, but
    # they still get a record in cg3h_status.json so the UI can grey them.
    mod_state = _load_mod_state(builder_dir)
    disabled_records = {}  # character → list of mod details that were skipped

    # Scan for CG3H mods with GLBs
    # Handles both flat layout (manual install) and nested layout (r2modman)
    #   flat:   plugins_data/{mod_id}/mod.json
    #   nested: plugins_data/{mod_id}/{mod_id}/mod.json
    by_character = {}
    for entry in sorted(os.listdir(plugins_data_dir)):
        mod_dir = os.path.join(plugins_data_dir, entry)
        if not os.path.isdir(mod_dir):
            continue
        mod_json_path = os.path.join(mod_dir, 'mod.json')
        if not os.path.isfile(mod_json_path):
            # Check one level deeper (r2modman nesting)
            for sub in os.listdir(mod_dir):
                sub_path = os.path.join(mod_dir, sub)
                if os.path.isdir(sub_path):
                    candidate = os.path.join(sub_path, 'mod.json')
                    if os.path.isfile(candidate):
                        mod_json_path = candidate
                        mod_dir = sub_path
                        break
        if not os.path.isfile(mod_json_path):
            continue

        with open(mod_json_path) as f:
            mod = json.load(f)

        if not mod.get('format', '').startswith('cg3h-mod'):
            continue

        character = mod.get('target', {}).get('character', '')
        glb_name = mod.get('assets', {}).get('glb', '')
        if not character or not glb_name:
            continue

        glb_path = os.path.join(mod_dir, glb_name)
        if not os.path.isfile(glb_path):
            continue

        mod_info = {
            'id': entry,
            'mod': mod,
            'mod_dir': mod_dir,
            'glb_path': glb_path,
            'manifest_path': os.path.join(mod_dir, 'manifest.json'),
        }

        if not _is_mod_enabled(mod_state, entry):
            # Disabled: don't contribute to the build, but track so the UI
            # can show the mod greyed out under its target character.
            disabled_records.setdefault(character, []).append(mod_info)
            print(f"  {entry}: disabled, skipping")
            continue

        by_character.setdefault(character, []).append(mod_info)

    # --character: narrow the build set to that character only, deleting
    # its cache key first so the rebuild always fires.
    if only_character is not None:
        print(f"  [--character] target={only_character!r}")
        print(f"  [--character] enabled chars before filter={sorted(by_character.keys())!r}")
        print(f"  [--character] disabled chars before filter={sorted(disabled_records.keys())!r}")
        by_character = {
            k: v for k, v in by_character.items() if k == only_character
        }
        disabled_records = {
            k: v for k, v in disabled_records.items() if k == only_character
        }
        cache_key_path = os.path.join(builder_dir, f"{only_character}.cache_key")
        if os.path.isfile(cache_key_path):
            os.unlink(cache_key_path)
            print(f"  {only_character}: cache key cleared for forced rebuild")

        # If --character was given but the character now has zero enabled
        # mods (the user just disabled the last one), the build loop won't
        # run for it.  Restore stock by deleting the merged GPK and cache
        # key right here, so the runtime falls back to the original file.
        if only_character not in by_character:
            stock_gpk = os.path.join(builder_dir, f"{only_character}.gpk")
            if os.path.isfile(stock_gpk):
                os.unlink(stock_gpk)
                print(f"  {only_character}: no enabled mods, removed merged GPK (revert to stock)")

    # Clean up GPKs for characters that no longer have *enabled* mods.
    # Pre-v3.8 this was keyed on `by_character`, but a character with only
    # disabled mods should also revert to stock — so we compare against
    # the union, and if a character has zero enabled mods the GPK is
    # deleted below during the status-record pass.
    active_chars = set(by_character.keys())
    if only_character is None:
        # Only prune during full scans — a --character run must not touch
        # other characters' files.
        for f in os.listdir(builder_dir):
            if f.endswith('.gpk'):
                char = f[:-4]
                if char not in active_chars:
                    os.unlink(os.path.join(builder_dir, f))
                    cache_key = os.path.join(builder_dir, f"{char}.cache_key")
                    if os.path.isfile(cache_key):
                        os.unlink(cache_key)
                    print(f"  Removed stale GPK: {f} (no enabled mods)")

    gpk_dir = os.path.join(game_dir, "Content", "GR2", "_Optimized")
    sdb_dir = gpk_dir
    dll_path = os.path.join(game_dir, "Ship", "granny2_x64.dll")

    built = 0
    cached = 0
    failed = 0

    # v3.7: per-character status for cg3h_status.json (consumed by the
    # in-game mod manager UI).  Populated as we iterate so every branch
    # (cached, built, failed-at-merge, failed-at-convert) writes a record.
    status_characters = {}

    # v3.8 hot-reload helper: extract each GPK entry as a standalone
    # .gr2.lz4 file alongside the merged GPK so the game's per-entry
    # loader (sgg::Granny3D::OpenOptimizedGrannyFile) can find them via
    # H2M's file redirect.  Runs on both fresh-build and cached-build
    # paths — on cached path it's a no-op if the files already exist.
    def _extract_entries(character_name, gpk_path, force=False):
        try:
            from gpk_pack import extract_gpk_raw
            entries_raw = extract_gpk_raw(gpk_path)
            n_written = 0
            for entry_name, lz4_bytes in entries_raw.items():
                entry_path = os.path.join(builder_dir, f"{entry_name}.gr2.lz4")
                # On cache hits (force=False), skip if the file exists and
                # matches size.  On fresh builds (force=True), always
                # overwrite — two files with different content can have
                # the same compressed size (e.g. position-only edits).
                if (not force and os.path.isfile(entry_path)
                        and os.path.getsize(entry_path) == len(lz4_bytes)):
                    continue
                with open(entry_path, 'wb') as f:
                    f.write(lz4_bytes)
                n_written += 1
            if n_written > 0:
                print(f"  {character_name}: extracted {n_written} entry file(s)")
        except Exception as e:
            print(f"  {character_name}: WARNING extract_gpk_raw failed: {e}")

    # Empty-mods case: skip the loop but still fall through to the status
    # JSON write so the UI sees fresh state (important when a modder just
    # uninstalled everything — stale status would be misleading).
    if not by_character:
        print("No CG3H mesh mods found")

    def _variants_map_for(character, variant_mods):
        """Deterministic {stock_entry -> {mod_id -> variant_name}} for a
        character's variant_mods list.  Used on cache-hit to re-populate
        status.json without re-running convert().  Mirrors the shape that
        `_build_variant_entries` produces, including the "stock" key for
        each touched entry."""
        vmap = {}
        touched = set()
        for mi in variant_mods:
            mod = mi['mod']
            mesh_entries = mod.get('target', {}).get('mesh_entries', [])
            for idx, entry_name in enumerate(mesh_entries):
                vname = _variant_entry_name(character, mi['id'], idx)
                vmap.setdefault(entry_name, {})[mi['id']] = vname
                touched.add(entry_name)
        for idx, entry_name in enumerate(sorted(touched)):
            vmap.setdefault(entry_name, {})["stock"] = _variant_entry_name(character, "Stock", idx)
        return vmap

    def _record(char, state, char_mods, gpk_path, error, duration_ms,
                disabled_mods=None):
        # mod_details lists ALL mods on this character (enabled + disabled)
        # so the UI can render the full picture; the per-mod "enabled"
        # flag tells it which to grey out.
        all_mods = list(char_mods) + list(disabled_mods or [])
        status_characters[char] = {
            "state": state,
            "gpk_path": gpk_path if gpk_path and os.path.isfile(gpk_path) else None,
            "mods": [mi['id'] for mi in char_mods],
            "mod_details": [
                {
                    "id": mi['id'],
                    "name": mi['mod'].get('metadata', {}).get('name', mi['id']),
                    "version": mi['mod'].get('metadata', {}).get('version', ''),
                    "author": mi['mod'].get('metadata', {}).get('author', ''),
                    "enabled": mi in char_mods,
                }
                for mi in all_mods
            ],
            "error": error,
            "duration_ms": int(duration_ms) if duration_ms is not None else None,
        }

    for character, char_mods in sorted(by_character.items()):
        char_start = time.monotonic()
        output_gpk = os.path.join(builder_dir, f"{character}.gpk")
        cache_key_path = os.path.join(builder_dir, f"{character}.cache_key")
        char_disabled = disabled_records.get(character, [])

        # Build a cache key from sorted mod ids + GLB mtimes.  v3.8 appends
        # a "disabled:<id>" line for every disabled mod targeting this
        # character — toggling a mod naturally invalidates the cache.
        current_key = ""
        for mi in sorted(char_mods, key=lambda m: m['id']):
            glb_mtime = os.path.getmtime(mi['glb_path']) if os.path.isfile(mi['glb_path']) else 0
            current_key += f"{mi['id']}:{glb_mtime}\n"
        for mi in sorted(char_disabled, key=lambda m: m['id']):
            current_key += f"disabled:{mi['id']}\n"

        # Check cache — rebuild if mods changed
        if os.path.isfile(output_gpk) and os.path.isfile(cache_key_path):
            with open(cache_key_path) as f:
                saved_key = f.read()
            if saved_key == current_key:
                print(f"  {character}: cached ({len(char_mods)} mod(s))")
                cached += 1
                # Ensure per-entry standalone files exist for hot-reload
                # even on cache hits (so profiles built before v3.8 get
                # them on first launch after upgrading).
                _extract_entries(character, output_gpk)
                _record(character, "cached", char_mods, output_gpk, None,
                        (time.monotonic() - char_start) * 1000,
                        disabled_mods=char_disabled)
                # v3.9: re-populate variants map on cache hit so status.json
                # stays accurate without re-running convert().  Uses the
                # same classifier as the build path (_classify_mod) so
                # mixed mesh_add+mesh_replace mods aren't accidentally
                # treated as variants here.
                cached_vmap = _variants_map_for(character, [
                    mi for mi in char_mods if _classify_mod(mi['mod'])[0]
                ])
                if cached_vmap:
                    status_characters[character]["variants"] = cached_vmap
                continue
            else:
                print(f"  {character}: mods changed, rebuilding...")
                os.unlink(output_gpk)
        elif os.path.isfile(output_gpk):
            # No cache key file — legacy cache, rebuild
            print(f"  {character}: no cache key, rebuilding...")
            os.unlink(output_gpk)

        original_gpk = os.path.join(gpk_dir, f"{character}.gpk")
        sdb_path = os.path.join(sdb_dir, f"{character}.sdb")
        if not os.path.isfile(original_gpk) or not os.path.isfile(sdb_path):
            print(f"  {character}: ERROR — game GPK/SDB not found")
            failed += 1
            _record(character, "failed", char_mods, None,
                    "game GPK/SDB not found",
                    (time.monotonic() - char_start) * 1000,
                    disabled_mods=char_disabled)
            continue

        # v3.9: classify mods for the outfit picker via the shared
        # _classify_mod helper (same rule used on cache-hit below).
        # EVERY mesh-bearing mod merges into the default stock entry —
        # that's what keeps the drawable pre-sized for the MAX footprint,
        # so runtime swap to a slim variant always fits.
        variant_mods = []
        accessory_mods = []
        for mi in char_mods:
            is_variant, is_accessory = _classify_mod(mi['mod'])
            if is_variant:
                variant_mods.append(mi)
            elif is_accessory:
                accessory_mods.append(mi)

        print(f"  {character}: building from {len(char_mods)} mod(s)")

        merged_glb = None
        collisions = set()
        merged_manifest = None
        merged_routing = {}
        allow_topo = False
        patch_anims = False
        convert_error = None

        try:
            merged_glb, collisions = _merge_glbs(char_mods, builder_dir, character)
        except Exception as e:
            print(f"  {character}: ERROR — GLB merge failed: {e}")
            failed += 1
            _record(character, "failed", char_mods, None,
                    f"GLB merge failed: {e}",
                    (time.monotonic() - char_start) * 1000,
                    disabled_mods=char_disabled)
            continue
        if not merged_glb:
            print(f"  {character}: ERROR — GLB merge returned no output")
            failed += 1
            _record(character, "failed", char_mods, None,
                    "GLB merge returned no output",
                    (time.monotonic() - char_start) * 1000,
                    disabled_mods=char_disabled)
            continue

        for mod_info in char_mods:
            mod = mod_info['mod']
            mod_type = mod.get('type', '')
            types = mod_type if isinstance(mod_type, list) else [mod_type] if mod_type else []
            if any(t in ('mesh_add', 'mesh_replace') for t in types):
                allow_topo = True
            if 'animation_patch' in types:
                patch_anims = True
            routing = mod.get('target', {}).get('new_mesh_routing', {})
            for mesh_name, entries in routing.items():
                key = (f"{mod_info['id']}_{mesh_name}"
                       if collisions and mesh_name in collisions else mesh_name)
                merged_routing[key] = entries

        merged_manifest = _merge_manifests(char_mods, collisions=collisions or None)

        for mi in char_mods:
            print(f"    - {mi['id']}")

        try:
            convert(
                glb_path=merged_glb,
                gpk_path=original_gpk,
                sdb_path=sdb_path,
                dll_path=dll_path,
                output_gpk=output_gpk,
                manifest_dict=merged_manifest,
                allow_topology_change=allow_topo,
                patch_animations=patch_anims,
                new_mesh_routing=merged_routing or None,
            )
            ok = True
        except Exception as e:
            print(f"    ERROR: {e}")
            traceback.print_exc()
            ok = False
            convert_error = str(e)

        if merged_glb and os.path.isfile(merged_glb):
            os.unlink(merged_glb)

        duration_ms = (time.monotonic() - char_start) * 1000
        if ok:
            built += 1
            # Save cache key so we can detect mod changes
            with open(cache_key_path, 'w') as f:
                f.write(current_key)
            print(f"  {character}: done -> {output_gpk}")

            # v3.9: inject per-mod variant entries into the merged GPK.
            # Each variant contains only that mod's contribution — strictly
            # <= stock entry in size, so the drawable allocated at scene-load
            # (sized for the merged stock) always has room.
            variants_map = {}
            if variant_mods:
                try:
                    variants_map = _build_variant_entries(
                        character, variant_mods, accessory_mods,
                        output_gpk, original_gpk, sdb_path, dll_path,
                        builder_dir,
                    )
                except Exception as e:
                    print(f"  {character}: WARNING — variant emit failed: {e}")
                    traceback.print_exc()

            # Extract per-entry .gr2.lz4 AFTER variants are injected so they
            # show up in the per-entry file set too.
            _extract_entries(character, output_gpk, force=True)

            _record(character, "built", char_mods, output_gpk, None, duration_ms,
                    disabled_mods=char_disabled)
            if variants_map:
                status_characters[character]["variants"] = variants_map
        else:
            failed += 1
            _record(character, "failed", char_mods, None,
                    convert_error or "convert() failed", duration_ms,
                    disabled_mods=char_disabled)

    # Disabled-only characters: a character whose ONLY mods are all
    # disabled has already been pruned from the builder dir above (or
    # never built), but we still need a status record so the UI can
    # show it as "stock" with the disabled mods greyed out.
    for character, disabled_mods in disabled_records.items():
        if character in status_characters:
            continue  # already recorded via the build loop
        _record(character, "disabled", [], None, None, 0,
                disabled_mods=disabled_mods)

    print(f"\nScan complete: {built} built, {cached} cached, {failed} failed")

    # v3.7: write the status JSON.  Consumed by the in-game mod manager
    # Lua plugin.  Best-effort — builder success does not depend on the
    # write succeeding.
    # v3.8: --character runs touch only one character, but the on-disk
    # status JSON must keep records for every built character.  If we're
    # in --character mode, load the existing JSON and merge our new
    # records on top instead of overwriting.
    merged_characters = dict(status_characters)
    status_path = os.path.join(builder_dir, "cg3h_status.json")
    if only_character is not None and os.path.isfile(status_path):
        try:
            with open(status_path, encoding='utf-8') as f:
                prev = json.load(f)
            prev_chars = prev.get("characters", {})
            for char, rec in prev_chars.items():
                if char not in merged_characters:
                    merged_characters[char] = rec
        except (OSError, json.JSONDecodeError) as e:
            print(f"  WARNING: could not merge prior status JSON: {e}")

    status_doc = {
        "version": CG3H_STATUS_SCHEMA_VERSION,
        "builder_version": CG3H_VERSION,
        "built_at": datetime.datetime.now(datetime.timezone.utc)
                    .replace(microsecond=0).isoformat(),
        # v3.8: expose game_dir so the Lua runtime can find stock GPKs
        # when reverting a disabled character back to vanilla.
        "game_dir": game_dir.replace("\\", "/") if game_dir else "",
        "summary": {"built": built, "cached": cached, "failed": failed},
        "characters": merged_characters,
    }
    try:
        with open(status_path, 'w', encoding='utf-8') as f:
            json.dump(status_doc, f, indent=2)
    except OSError as e:
        print(f"  WARNING: could not write {status_path}: {e}")

    # Note: v3.9 variant data is emitted inline as `variants` on each
    # character entry in cg3h_status.json — the runtime reads it from
    # there.  An earlier draft also wrote a separate cg3h_variants.json
    # fed by `all_variants`; that dict was never populated, the file
    # never appeared, and no code reads it.  Removed.

    return failed == 0


def main():
    if len(sys.argv) < 2:
        print("Usage: cg3h_builder <mod_dir> [--game-dir <path>]")
        print("       cg3h_builder --scan-all <plugins_data_dir> [--game-dir <path>] [--character <name>]")
        sys.exit(1)

    # Optional --game-dir flag
    game_dir = None
    if '--game-dir' in sys.argv:
        idx = sys.argv.index('--game-dir')
        if idx + 1 < len(sys.argv):
            game_dir = sys.argv[idx + 1]

    # Optional --character flag (v3.8): limits --scan-all to one character
    # and forces rebuild for that character by clearing its cache key.
    only_character = None
    if '--character' in sys.argv:
        idx = sys.argv.index('--character')
        if idx + 1 < len(sys.argv):
            only_character = sys.argv[idx + 1]

    if sys.argv[1] == '--scan-all':
        if len(sys.argv) < 3 or sys.argv[2].startswith('--'):
            print("Usage: cg3h_builder --scan-all <plugins_data_dir> [--game-dir <path>] [--character <name>]")
            sys.exit(1)
        ok = scan_and_build_all(sys.argv[2], game_dir=game_dir,
                                only_character=only_character)
        sys.exit(0 if ok else 1)

    mod_dir = sys.argv[1]
    if not os.path.isdir(mod_dir):
        print(f"ERROR: Not a directory: {mod_dir}")
        sys.exit(1)

    ok = build_gpk(mod_dir, game_dir=game_dir)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
