"""
CG3H Runtime Builder — builds GPKs from mod assets + user's game files.

Called by the CG3HBuilder H2M plugin at game launch.
Reads mod.json from the given directory, finds the GLB and game files,
and calls gltf_to_gr2.convert() to produce {character}.gpk.

Usage:
    cg3h_builder.exe <mod_dir>
    python cg3h_builder_entry.py <mod_dir>
"""
import sys
import os
import json

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)
if sys.stdout and sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from cg3h_constants import find_game_path as _find_game_path


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
    import pygltflib

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
    import pygltflib

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
    import pygltflib

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

    from gltf_to_gr2 import convert
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
        import traceback
        traceback.print_exc()
        return False


def scan_and_build_all(plugins_data_dir, game_dir=None):
    """Scan plugins_data/ for all CG3H mods, group by character, build merged GPKs.

    Merged GPKs are output to plugins_data/CG3HBuilder/{character}.gpk.
    For multiple mods targeting the same character, each mod's GLB is applied
    sequentially to produce one merged GPK with all meshes.
    """
    if not game_dir:
        game_dir = find_game_dir()
    if not game_dir:
        print("ERROR: Hades II game directory not found")
        return False

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

        by_character.setdefault(character, []).append({
            'id': entry,
            'mod': mod,
            'mod_dir': mod_dir,
            'glb_path': glb_path,
            'manifest_path': os.path.join(mod_dir, 'manifest.json'),
        })

    # Output GPKs next to cg3h_builder.exe (handles r2modman nesting)
    # When running as PyInstaller exe, sys.executable is the exe path
    if getattr(sys, 'frozen', False):
        builder_dir = os.path.dirname(sys.executable)
    else:
        builder_dir = os.path.join(plugins_data_dir, 'CG3HBuilder')
    os.makedirs(builder_dir, exist_ok=True)

    # Clean up GPKs for characters that no longer have mods
    for f in os.listdir(builder_dir):
        if f.endswith('.gpk'):
            char = f[:-4]
            if char not in by_character:
                os.unlink(os.path.join(builder_dir, f))
                cache_key = os.path.join(builder_dir, f"{char}.cache_key")
                if os.path.isfile(cache_key):
                    os.unlink(cache_key)
                print(f"  Removed stale GPK: {f}")

    if not by_character:
        print("No CG3H mesh mods found")
        return True
    gpk_dir = os.path.join(game_dir, "Content", "GR2", "_Optimized")
    sdb_dir = gpk_dir
    dll_path = os.path.join(game_dir, "Ship", "granny2_x64.dll")

    from gltf_to_gr2 import convert

    built = 0
    cached = 0
    failed = 0

    for character, char_mods in sorted(by_character.items()):
        output_gpk = os.path.join(builder_dir, f"{character}.gpk")
        cache_key_path = os.path.join(builder_dir, f"{character}.cache_key")

        # Build a cache key from sorted mod ids + GLB modification times
        current_key = ""
        for mi in sorted(char_mods, key=lambda m: m['id']):
            glb_mtime = os.path.getmtime(mi['glb_path']) if os.path.isfile(mi['glb_path']) else 0
            current_key += f"{mi['id']}:{glb_mtime}\n"

        # Check cache — rebuild if mods changed
        if os.path.isfile(output_gpk) and os.path.isfile(cache_key_path):
            with open(cache_key_path) as f:
                saved_key = f.read()
            if saved_key == current_key:
                print(f"  {character}: cached ({len(char_mods)} mod(s))")
                cached += 1
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
            continue

        print(f"  {character}: building from {len(char_mods)} mod(s)...")

        # Merge all GLBs into one temp GLB, then build once.
        # This avoids the double-serialize problem where custom MaterialBindings
        # get lost across multiple convert() calls.
        try:
            merged_glb, collisions = _merge_glbs(char_mods, builder_dir, character)
        except Exception as e:
            print(f"  {character}: ERROR — GLB merge failed: {e}")
            failed += 1
            continue
        if not merged_glb:
            print(f"  {character}: ERROR — GLB merge returned no output")
            failed += 1
            continue

        # Collect operations and routing from all mods
        allow_topo = False
        patch_anims = False
        merged_routing = {}
        for mod_info in char_mods:
            mod = mod_info['mod']
            mod_type = mod.get('type', '')
            types = mod_type if isinstance(mod_type, list) else [mod_type] if mod_type else []
            if any(t in ('mesh_add', 'mesh_replace') for t in types):
                allow_topo = True
            if 'animation_patch' in types:
                patch_anims = True
            # Merge new_mesh_routing from all mods (apply dedup renames per-mod)
            routing = mod.get('target', {}).get('new_mesh_routing', {})
            for mesh_name, entries in routing.items():
                key = f"{mod_info['id']}_{mesh_name}" if collisions and mesh_name in collisions else mesh_name
                merged_routing[key] = entries

        # Merge manifests from all mods (with collision renames applied per-mod)
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
            import traceback
            traceback.print_exc()
            ok = False

        if os.path.isfile(merged_glb):
            os.unlink(merged_glb)

        if ok:
            built += 1
            # Save cache key so we can detect mod changes
            with open(cache_key_path, 'w') as f:
                f.write(current_key)
            print(f"  {character}: done -> {output_gpk}")
        else:
            failed += 1

    print(f"\nScan complete: {built} built, {cached} cached, {failed} failed")
    return failed == 0


def main():
    if len(sys.argv) < 2:
        print("Usage: cg3h_builder <mod_dir> [--game-dir <path>]")
        print("       cg3h_builder --scan-all <plugins_data_dir> [--game-dir <path>]")
        sys.exit(1)

    # Optional --game-dir flag
    game_dir = None
    if '--game-dir' in sys.argv:
        idx = sys.argv.index('--game-dir')
        if idx + 1 < len(sys.argv):
            game_dir = sys.argv[idx + 1]

    if sys.argv[1] == '--scan-all':
        if len(sys.argv) < 3 or sys.argv[2].startswith('--'):
            print("Usage: cg3h_builder --scan-all <plugins_data_dir> [--game-dir <path>]")
            sys.exit(1)
        ok = scan_and_build_all(sys.argv[2], game_dir=game_dir)
        sys.exit(0 if ok else 1)

    mod_dir = sys.argv[1]
    if not os.path.isdir(mod_dir):
        print(f"ERROR: Not a directory: {mod_dir}")
        sys.exit(1)

    ok = build_gpk(mod_dir, game_dir=game_dir)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
