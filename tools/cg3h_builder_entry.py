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

import numpy as np
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


def _skins_map_for(char_mods):
    """v3.12: per-character skins registry for cg3h_status.json.

    Returns `{ mod_id: { name, version, granny_texture, pkg_entries,
    preview } }` for every `texture_replace` mod in `char_mods`.
    `granny_texture` is the unique-renamed PKG entry the runtime feeds
    to `SetThingProperty(GrannyTexture=...)` for live skin swap.
    """
    skins = {}
    for mi in char_mods:
        mod = mi['mod']
        mod_type = mod.get('type', '')
        types = mod_type if isinstance(mod_type, list) \
            else [mod_type] if mod_type else []
        if 'texture_replace' not in types:
            continue
        metadata = mod.get('metadata', {})
        textures = mod.get('assets', {}).get('textures', []) or []
        # Match the second-pass rename rule: `<mod_id>_<basename>` per
        # entry; first one is the primary atlas.
        pkg_entries = []
        granny_texture = None
        for t in textures:
            pkg_entry_name = t.get('pkg_entry_name')
            if not pkg_entry_name:
                continue
            basename = pkg_entry_name.replace('\\', '/').split('/')[-1]
            unique_name = f"{mi['id']}_{basename}"
            pkg_entries.append(unique_name)
            if granny_texture is None:
                granny_texture = unique_name
        preview = None
        preview_path = os.path.join(mi.get('mod_dir', ''), 'preview.png')
        if mi.get('mod_dir') and os.path.isfile(preview_path):
            preview = preview_path.replace('\\', '/')
        skins[mi['id']] = {
            'name': metadata.get('name', mi['id']),
            'version': metadata.get('version', ''),
            'granny_texture': granny_texture,
            'pkg_entries': sorted(pkg_entries),
            'preview': preview,
        }
    return skins


def _classify_mod(mod):
    """Single source of truth for mod classification.

    Returns (is_variant, is_accessory, is_animation_only, is_animation_add).
    All four can be False (e.g. a pure texture_replace mod).  is_variant and
    is_accessory cannot both be True — mesh_add is the dominant signal
    (any mesh_add presence forces additive).

    Rule:
      - PURE mesh_replace (mesh_replace in type, mesh_add NOT in type)
        and non-empty target.mesh_entries → picker variant.
      - Anything with mesh_add in type (including mixed with
        mesh_replace) → additive accessory.
      - animation_patch in type AND no mesh_add/mesh_replace → standalone
        animation_only (v3.10).  Mixed with mesh_* falls into the
        variant/accessory bucket; animation patches still apply during
        convert() in that case.
      - animation_add in type → ships brand-new animation entries plus
        their SJSON aliases (v3.11).  Independent of mesh_* — a single
        mod can be both a variant AND ship new animations.
    """
    mod_type = mod.get('type', '')
    types = mod_type if isinstance(mod_type, list) \
        else [mod_type] if mod_type else []
    has_entries = bool(mod.get('target', {}).get('mesh_entries', []))
    has_mesh_ops = 'mesh_add' in types or 'mesh_replace' in types
    is_pure_replacer = 'mesh_replace' in types and 'mesh_add' not in types
    is_variant = is_pure_replacer and has_entries
    is_accessory = 'mesh_add' in types
    is_animation_only = 'animation_patch' in types and not has_mesh_ops
    is_animation_add = 'animation_add' in types
    return is_variant, is_accessory, is_animation_only, is_animation_add


def _collect_animation_adds(animation_add_mods, character):
    """v3.11 — gather convert()-side specs + alias dicts from animation_add mods.

    Returns (add_specs, alias_entries):
      add_specs     - [{template, target, glb_action?}] passed to convert()
      alias_entries - [{logical_name, granny_name, sjson, ...}] persisted
                      to status.json for the Lua runtime to register as
                      SJSON Animation entries

    Each entry in target.new_animations supports:
      logical_name      (required) — Lua-facing alias name
      granny_name       (required) — GPK entry key the alias points at
      clone_from        (required) — existing GR2 entry to use as the
                                     bone-track template
      source_glb_action (optional) — GLB animation name to use for
                                     curves (v3.11+).  When absent, the
                                     template's curves are kept as-is
                                     (pure byte-clone).
      loop              (optional, default false)
      inherit_from      (optional, default character-specific base)
      chain_to          (optional)
      blends            (optional list of {from, duration})
      cancel_on_owner_move (optional bool)

    `target.alias_sjson` (mod-level) overrides the per-character SJSON
    home.  Defaults come from anim_sjson_routing.alias_home_for(character).
    """
    add_specs = []
    alias_entries = []
    default_sjson = alias_home_for(character)

    for mi in animation_add_mods:
        mod = mi['mod']
        target = mod.get('target', {}) or {}
        mod_alias_sjson = target.get('alias_sjson') or default_sjson
        for raw in target.get('new_animations', []):
            if not isinstance(raw, dict):
                print(f"    WARNING: {mi['id']} new_animations entry is not "
                      f"an object: {raw!r}")
                continue
            logical = raw.get('logical_name')
            granny = raw.get('granny_name')
            clone_from = raw.get('clone_from')
            glb_action = raw.get('source_glb_action')
            if not (logical and granny):
                print(f"    WARNING: {mi['id']} new_animations entry "
                      f"missing logical_name or granny_name: {raw!r}")
                continue
            if not clone_from:
                # v3.11 requires a template.  v3.12 may relax this if we
                # build a "from skeleton only" path that doesn't need
                # a stock animation as bone-track scaffolding.
                print(f"    WARNING: {mi['id']} new_animations entry needs "
                      f"clone_from (template GR2 entry): {logical!r}")
                continue
            add_specs.append({
                'template': clone_from,
                'target': granny,
                'glb_action': glb_action,  # None → byte-clone
            })
            sjson_basename = raw.get('alias_sjson') or mod_alias_sjson
            if not sjson_basename:
                print(f"    WARNING: {mi['id']} no alias_sjson home for "
                      f"character {character!r} and none specified; "
                      f"alias {logical!r} will not be registered")
                continue
            # v3.15: full SJSON Animation field set.  Each top-level key
            # below maps to a single SJSON field on the injected alias.
            # Conventions:
            #   - bool:  None = field absent from emitted SJSON
            #            True = present as `Field = true`
            #            False also emits `Field = false` when explicitly set
            #            (some flags like Enable3DShadow have polarising
            #            stock values; see project_v315_anim_metadata.md).
            #   - float: None = absent; specific value = `Field = N`.
            #   - string: None or "" = absent; otherwise quoted in SJSON.
            # The Blender addon UI fills these from NLA strip properties +
            # CG3H sub-panel toggles; modders can hand-edit mod.json to
            # override any field.  Fields not in this set still flow via
            # the `_sjson` passthrough (v3.15.x — phase 2).
            def _b(k):
                v = raw.get(k)
                return None if v is None else bool(v)
            def _n(k):
                v = raw.get(k)
                return None if v is None else float(v)
            def _s(k):
                v = raw.get(k)
                if v is None or v == "":
                    return None
                return str(v)
            alias_entries.append({
                'mod_id': mi['id'],
                'character': character,
                'logical_name': logical,
                'granny_name': granny,
                'clone_from': clone_from,
                'source_glb_action': glb_action,
                'sjson': sjson_basename,
                # Always-present basics (ChainTo handled below)
                'loop': bool(raw.get('loop', False)),
                'inherit_from': _s('inherit_from'),
                'chain_to': _s('chain_to'),
                # Transition / playback
                'speed': _n('speed'),
                'blend_in_frames': _n('blend_in_frames'),
                # Gameplay flags (snake_case mirrors the Blender UI;
                # SJSON emit translates to PascalCase).
                'cancel_on_owner_move': _b('cancel_on_owner_move'),
                'hold_last_frame': _b('hold_last_frame'),
                'allow_restart': _b('allow_restart'),
                'owner_invulnerable': _b('owner_invulnerable'),
                'owner_immobile': _b('owner_immobile'),
                'owner_has_no_collision': _b('owner_has_no_collision'),
                'owner_untargetable': _b('owner_untargetable'),
                'disable_owner_manual_interact': _b('disable_owner_manual_interact'),
                # Polarising / overrides
                'enable_3d_shadow': _b('enable_3d_shadow'),
                'scale': _n('scale'),
                'native_move_speed': _n('native_move_speed'),
                # Carried but currently unused by SJSON injection;
                # retained for cg3h_status.json round-trip and future
                # source-specific blend support.
                'blends': raw.get('blends') or [],
            })
    return add_specs, alias_entries


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

from anim_sjson_routing import alias_home_for
from cg3h_build import _sync_mod_json
from cg3h_constants import (
    CG3H_BUILDER_FOLDER,
    CG3H_VERSION,
    find_game_path as _find_game_path,
)
from gltf_to_gr2 import convert
from pkg_texture import build_standalone_pkg
from texture_variant import load_or_build_pkg_entry_set


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


def _triage_mod_meshes(mod_info):
    """Classify each mesh in a mod's GLB as 'reference', 'replaced', or 'new'.

    - 'reference': position data matches the baseline exported at import time
      (author included this mesh for scene context but didn't edit it — the
      classic case is an accessory mod shipping unchanged stock meshes
      alongside its one new mesh).  These are DROPPED from the merge.

    - 'replaced': mesh name is known-stock AND positions differ from the
      baseline → the author edited this stock mesh.  Keeps the stock name
      so convert() patches the stock GR2.

    - 'new': mesh name is not a known-stock name → genuinely added mesh
      that needs new_mesh_routing to place it.

    The baseline is the ``.baseline_positions.npz`` the Blender addon
    preserves from import to export.  If it's missing (author exported
    by hand without the addon), fall back to the manifest's stock mesh
    names: treat a stock-named mesh as 'replaced' (safe default — we'd
    rather ship an unnecessary patch than silently drop a real edit)
    and non-stock names as 'new'.

    Returns a dict {mesh_name: 'reference' | 'replaced' | 'new'}.
    """
    gltf = pygltflib.GLTF2().load(mod_info['glb_path'])
    blob = gltf.binary_blob()

    # mod_dir is optional for test harnesses that feed in synthetic GLBs
    # with no on-disk workspace.  Without mod_dir we have no baseline and
    # no manifest — triage falls through to "every mesh is 'new'".
    mod_dir = mod_info.get('mod_dir')
    baseline = {}
    if mod_dir:
        baseline_path = os.path.join(mod_dir, '.baseline_positions.npz')
        if os.path.isfile(baseline_path):
            try:
                baseline = dict(np.load(baseline_path))
            except Exception as e:
                print(f"    {mod_info['id']}: baseline load failed ({e}) — "
                      f"triage falls back to manifest-name heuristic")

    manifest_stock_names = set()
    manifest_path = mod_info.get('manifest_path')
    if not manifest_path and mod_dir:
        manifest_path = os.path.join(mod_dir, 'manifest.json')
    if manifest_path and os.path.isfile(manifest_path):
        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
            manifest_stock_names = {m['name'] for m in manifest.get('meshes', [])}
        except Exception:
            pass  # manifest missing/unreadable — fine, we'll treat everything as new

    result = {}
    for mesh in gltf.meshes:
        baseline_pos = baseline.get(mesh.name)

        if baseline_pos is not None:
            if not mesh.primitives or mesh.primitives[0].attributes.POSITION is None:
                result[mesh.name] = 'new'
                continue
            prim = mesh.primitives[0]
            acc = gltf.accessors[prim.attributes.POSITION]
            bv = gltf.bufferViews[acc.bufferView]
            pos_bytes = blob[bv.byteOffset:bv.byteOffset + bv.byteLength]
            vc = acc.count

            if baseline_pos.shape != (vc, 3):
                # Vertex count changed — definitive edit (or topology change).
                result[mesh.name] = 'replaced'
                continue
            cur_pos = np.frombuffer(pos_bytes, dtype=np.float32).reshape(vc, 3)
            max_diff = float(np.max(np.abs(cur_pos - baseline_pos)))
            # Same threshold as _is_mesh_changed in cg3h_build.py (1e-4
            # is well above Blender's ~1e-5 export noise and catches any
            # visible vertex edit).
            result[mesh.name] = 'replaced' if max_diff > 1e-4 else 'reference'
        elif mesh.name in manifest_stock_names:
            result[mesh.name] = 'replaced'
        else:
            result[mesh.name] = 'new'

    return result


def _resolve_merge_keep_map(char_mods, primary_mod_id):
    """Decide per-mod which meshes to keep in the merge and what to call them.

    Returns a dict {mod_id: {mesh_name: keep_name | None}} where keep_name
    is the name to use in the merged GLB (same as original, or prefixed
    for collision-renamed new meshes), or None if the mesh should be
    dropped entirely.

    Also returns the set of original mesh names that had to be
    prefix-renamed for collision (consumed downstream by _merge_manifests
    and new_mesh_routing remapping so they keep pointing at the right
    meshes).

    Resolution rules:
      - 'reference' class: always dropped (it's authoring scaffolding).
      - 'replaced' class with no cross-mod collision: kept unprefixed.
      - 'replaced' class with cross-mod collision:
          - variant merge (primary_mod_id set): only the primary's copy
            survives, others drop.  Accessories can't silently override
            the active variant's edits.
          - main merge (primary_mod_id=None): biggest-by-vertex-count
            wins the unprefixed stock name; losers drop from main and
            will instead appear in their own variant build.
      - 'new' class with no cross-mod collision: kept unprefixed.
      - 'new' class with cross-mod collision: prefix-renamed on every
        side (coincidental same-name across unrelated mods, today's
        behaviour preserved).
    """
    per_mod_classes = {mi['id']: _triage_mod_meshes(mi) for mi in char_mods}
    per_mod_gltf = {mi['id']: pygltflib.GLTF2().load(mi['glb_path']) for mi in char_mods}

    # Accessory policy: mods without `mesh_replace` in their declared type
    # never contribute 'replaced' meshes.  Their stock-named meshes are
    # always reference geometry (Blender re-exports the scene alongside
    # the new accessory mesh), and without a baseline the triage can't
    # always tell apart unchanged-stock from a real edit.  Downgrading
    # `replaced` to `reference` for non-replacer mods fixes this at the
    # policy level — matches the v3.9 design where accessories only add.
    for mi in char_mods:
        mod_type = mi['mod'].get('type', '') if 'mod' in mi else ''
        types = (mod_type if isinstance(mod_type, list)
                 else [mod_type] if mod_type else [])
        if 'mesh_replace' not in types:
            classes = per_mod_classes[mi['id']]
            for name, cls in list(classes.items()):
                if cls == 'replaced':
                    classes[name] = 'reference'

    # Group mesh occurrences by (class, name) for collision detection.
    replaced_occurrences = {}  # name -> [(mod_id, vertex_count), ...]
    new_occurrences = {}       # name -> [mod_id, ...]
    for mi in char_mods:
        classes = per_mod_classes[mi['id']]
        gltf = per_mod_gltf[mi['id']]
        mesh_vc = {}
        for mesh in gltf.meshes:
            if not mesh.primitives or mesh.primitives[0].attributes.POSITION is None:
                mesh_vc[mesh.name] = 0
                continue
            acc_idx = mesh.primitives[0].attributes.POSITION
            mesh_vc[mesh.name] = gltf.accessors[acc_idx].count
        for name, cls in classes.items():
            if cls == 'replaced':
                replaced_occurrences.setdefault(name, []).append(
                    (mi['id'], mesh_vc.get(name, 0)))
            elif cls == 'new':
                new_occurrences.setdefault(name, []).append(mi['id'])

    replaced_winners = {}  # stock_name -> winning mod_id
    for name, occ in replaced_occurrences.items():
        if len(occ) == 1:
            replaced_winners[name] = occ[0][0]
        elif primary_mod_id is not None:
            # Variant merge: primary wins if it's in the contenders; else
            # none of the accessories' edits land (we only want the
            # variant's replaces, not accidental overrides).
            ids = [mid for mid, _ in occ]
            if primary_mod_id in ids:
                replaced_winners[name] = primary_mod_id
            # else: no winner — all get dropped.  Rare path, but safe.
        else:
            # Main merge: biggest vertex count wins.  Ties broken by
            # alphabetical mod id for determinism.
            occ_sorted = sorted(occ, key=lambda t: (-t[1], t[0]))
            replaced_winners[name] = occ_sorted[0][0]
            losers = [mid for mid, _ in occ_sorted[1:]]
            print(f"    main-merge: '{name}' replaced by {len(occ)} mods; "
                  f"winner={occ_sorted[0][0]} ({occ_sorted[0][1]} verts); "
                  f"losers deferred to their own variants: {', '.join(losers)}")

    new_collisions = {name for name, ids in new_occurrences.items() if len(ids) > 1}
    if new_collisions:
        for name in sorted(new_collisions):
            print(f"    new-mesh collision: '{name}' used by "
                  f"{', '.join(new_occurrences[name])} — prefix-renaming")

    keep_map = {}
    for mi in char_mods:
        mod_id = mi['id']
        classes = per_mod_classes[mod_id]
        per_mesh = {}
        for name, cls in classes.items():
            if cls == 'reference':
                per_mesh[name] = None
            elif cls == 'replaced':
                if replaced_winners.get(name) == mod_id:
                    per_mesh[name] = name  # keep stock name; convert() will patch
                else:
                    per_mesh[name] = None  # loser or primary-absent case
            else:  # 'new'
                if name in new_collisions:
                    per_mesh[name] = f"{mod_id}_{name}"
                else:
                    per_mesh[name] = name
        keep_map[mod_id] = per_mesh

    return keep_map, new_collisions


def _merge_glbs(char_mods, output_dir, character, primary_mod_id=None):
    """Merge meshes, materials, textures, and animations from multiple GLBs.

    Triage each mod's meshes (via _triage_mod_meshes + _resolve_merge_keep_map)
    before merging.  'reference' meshes drop; 'replaced' keep stock names for
    convert() to patch; 'new' merge through, prefix-renamed only on cross-mod
    name collision.

    primary_mod_id: set for variant builds (the one active mesh_replace mod
    in the bundle).  Ensures the active variant wins any 'replaced' tiebreak
    instead of an accessory accidentally overriding it.

    Returns (merged_path, collisions) where collisions is the set of
    originally-named 'new' meshes that had to be prefix-renamed because
    multiple mods coincidentally used the same name.  'replaced' collisions
    DON'T appear here — they're resolved in-place by the winner rule and
    downstream code doesn't need to know.
    """
    keep_map, collisions = _resolve_merge_keep_map(char_mods, primary_mod_id)

    def _kept(mod_id, name):
        """Return the target name for this mesh in the merged GLB, or None
        if it should be skipped.  Any mesh not mentioned in keep_map (test
        harnesses that feed minimal mod_info dicts) falls through
        unchanged."""
        per_mesh = keep_map.get(mod_id, {})
        return per_mesh.get(name, name) if name not in per_mesh else per_mesh[name]

    # ── Load base GLB ──
    base_mod = char_mods[0]
    base_gltf = pygltflib.GLTF2().load(base_mod['glb_path'])

    # Drop base mod's dropped-class meshes, rename collision-renamed ones.
    # Work backwards so indices stay valid while we mutate base_gltf.meshes.
    base_rename_map = {}  # old_mesh_idx -> new_name (or None to drop)
    for i in range(len(base_gltf.meshes) - 1, -1, -1):
        mesh = base_gltf.meshes[i]
        target = _kept(base_mod['id'], mesh.name)
        if target is None:
            base_rename_map[i] = None
        elif target != mesh.name:
            base_rename_map[i] = target
            mesh.name = target

    if any(v is None for v in base_rename_map.values()):
        # Rebuild meshes/nodes/scenes without the dropped ones.
        dropped_indices = {i for i, v in base_rename_map.items() if v is None}
        kept_meshes = []
        old_to_new_mesh_idx = {}
        for i, mesh in enumerate(base_gltf.meshes):
            if i in dropped_indices:
                continue
            old_to_new_mesh_idx[i] = len(kept_meshes)
            kept_meshes.append(mesh)
        base_gltf.meshes = kept_meshes

        kept_nodes = []
        old_to_new_node_idx = {}
        for i, node in enumerate(base_gltf.nodes):
            if node.mesh is not None and node.mesh in dropped_indices:
                continue
            old_to_new_node_idx[i] = len(kept_nodes)
            if node.mesh is not None:
                node.mesh = old_to_new_mesh_idx[node.mesh]
            kept_nodes.append(node)
        base_gltf.nodes = kept_nodes

        for scene in (base_gltf.scenes or []):
            scene.nodes = [old_to_new_node_idx[n]
                           for n in scene.nodes
                           if n in old_to_new_node_idx]

    # Rename nodes whose mesh got renamed (for authoring cleanliness only)
    for node in base_gltf.nodes:
        if node.mesh is None:
            continue
        if node.mesh < len(base_gltf.meshes):
            node.name = base_gltf.meshes[node.mesh].name

    base_mesh_names = {m.name for m in base_gltf.meshes}

    # Skin-joint index → name, used to remap JOINTS_0 buffers from every
    # merged mod whose own skin orders bones differently (e.g. one mod
    # exported 122 joints, another 123 with `Hat_Mesh` prepended).
    # Without remapping, the merged node still references `skin=0` but the
    # vertex joint indices silently point at the wrong bone in the base
    # skin, producing visibly-wrong bone bindings at convert() time.
    base_skin_joint_names = []
    if base_gltf.skins:
        base_skin_joint_names = [
            base_gltf.nodes[ni].name for ni in base_gltf.skins[0].joints
        ]
    base_joint_idx_by_name = {n: i for i, n in enumerate(base_skin_joint_names)}

    # ── Merge each subsequent mod ──
    for mod_info in char_mods[1:]:
        other_gltf = pygltflib.GLTF2().load(mod_info['glb_path'])
        other_blob = other_gltf.binary_blob()
        base_blob = bytearray(base_gltf.binary_blob())

        bv_offset_map = {}
        acc_offset_map = {}
        joints_remapped_accessors = set()

        # Source skin joint names for this mod — same lookup the base did,
        # against the mod's own node/skin graph.  Values not present in the
        # base skin fall through to themselves (there is no meaningful base
        # index), which is fine since those bones also aren't in convert()'s
        # template bb_names and get filtered out of bb_entries.
        src_skin_joint_names = []
        if other_gltf.skins:
            src_skin_joint_names = [
                other_gltf.nodes[ni].name for ni in other_gltf.skins[0].joints
            ]
        joint_remap_needed = False
        joint_remap = np.arange(max(len(src_skin_joint_names), 1), dtype=np.uint16)
        for src_idx, name in enumerate(src_skin_joint_names):
            base_idx = base_joint_idx_by_name.get(name)
            if base_idx is not None and base_idx != src_idx:
                joint_remap[src_idx] = base_idx
                joint_remap_needed = True
            elif base_idx is None:
                # Bone dropped from base skin — leave the index alone;
                # convert() will filter it when building bone bindings.
                pass

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

        # Copy each mesh the triage said we should keep.  Dropped meshes
        # (reference geometry, main-merge replace-losers, variant-merge
        # non-primary replaces) are skipped outright.
        for mesh in other_gltf.meshes:
            original_name = mesh.name
            merge_name = _kept(mod_info['id'], original_name)
            if merge_name is None:
                continue

            if merge_name in base_mesh_names:
                # With the triage, two mods emitting the same mesh name is
                # only possible when (a) it's a 'replaced' winner that the
                # base mod and another mod both produced (shouldn't happen —
                # winner is unique), or (b) a genuine same-name clash we
                # missed.  Either way, skip with a diagnostic.
                print(f"    WARNING: mesh '{merge_name}' already in merged "
                      f"GLB, skipping {mod_info['id']}'s copy")
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

                # Remap JOINTS_0 indices into the base skin's joint order.
                # Done after _copy_accessor so we can rewrite the bytes that
                # actually landed in base_blob (keyed by new accessor idx).
                j0 = prim.attributes.JOINTS_0
                if (joint_remap_needed and j0 is not None
                        and j0 in acc_offset_map
                        and acc_offset_map[j0] not in joints_remapped_accessors):
                    new_acc_idx = acc_offset_map[j0]
                    new_acc = base_gltf.accessors[new_acc_idx]
                    new_bv = base_gltf.bufferViews[new_acc.bufferView]
                    if new_acc.componentType == 5121:
                        dtype = np.uint8
                    elif new_acc.componentType == 5123:
                        dtype = np.uint16
                    else:
                        dtype = None
                    if dtype is not None:
                        off = new_bv.byteOffset + (new_acc.byteOffset or 0)
                        nbytes = new_acc.count * 4 * np.dtype(dtype).itemsize
                        arr = np.frombuffer(
                            bytes(base_blob[off:off + nbytes]),
                            dtype=dtype,
                        ).reshape(new_acc.count, 4).copy()
                        max_src = int(arr.max()) if arr.size else 0
                        lookup_sz = max(max_src + 1, len(joint_remap))
                        lookup = np.arange(lookup_sz, dtype=dtype)
                        n = min(len(joint_remap), lookup_sz)
                        lookup[:n] = joint_remap[:n].astype(dtype)
                        remapped = lookup[arr]
                        base_blob[off:off + nbytes] = remapped.tobytes()
                    joints_remapped_accessors.add(new_acc_idx)

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
                # Pass mod_id as the primary so accessory-supplied reference
                # geometry (their replaced copies of stock meshes) loses
                # any collision tiebreak to the variant's actual edits.
                variant_glb, variant_collisions = _merge_glbs(
                    bundle, builder_dir, f"{character}_{mod_id}",
                    primary_mod_id=mod_id,
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

    # Emit a "Stock" variant per scene entry that at least one
    # variant_mod targets.  Two paths:
    #
    # - If there are no accessories: raw byte-copy from the stock GPK.
    #   The merged GPK preserves byte-identical stock entries for
    #   untouched entries, so raw stock bytes are compatible.
    #
    # - If there are accessories: run convert() against the stock GPK
    #   with an accessory-only bundle so the resulting entries contain
    #   "stock body + all accessories".  Without this, picking Stock
    #   in the in-game picker would hide every accessory (the raw-stock
    #   entry doesn't have the accessory meshes merged in, so DoDraw3D
    #   can't render them and set_mesh_visible has nothing to flip).
    #
    # Entry naming: `{Character}_Stock_V{N}_Mesh` (prefix + index).  The
    # prefix matters for skeleton linkage in the engine; an earlier
    # iteration tried `Stock_<Entry>_Mesh` and broke that linkage.
    stock_entries_touched = set()
    for mod_info in variant_mods:
        for e in mod_info['mod'].get('target', {}).get('mesh_entries', []):
            stock_entries_touched.add(e)

    if stock_entries_touched and accessory_mods:
        # Build a stock-body-plus-accessories GPK, then extract the
        # targeted entries.
        try:
            if len(accessory_mods) == 1:
                stock_glb = accessory_mods[0]['glb_path']
                stock_collisions = set()
            else:
                stock_glb, stock_collisions = _merge_glbs(
                    accessory_mods, builder_dir, f"{character}_Stock"
                )
        except Exception as e:
            print(f"    stock: accessory merge failed ({e}); falling back to raw-copy")
            stock_glb = None
            stock_collisions = set()

        stock_gpk_entries = None
        if stock_glb and os.path.isfile(stock_glb):
            stock_manifest = _merge_manifests(
                accessory_mods, collisions=stock_collisions or None
            )
            stock_routing = {}
            for mi in accessory_mods:
                routing = mi['mod'].get('target', {}).get('new_mesh_routing', {})
                for mesh_name, entries in routing.items():
                    key = (f"{mi['id']}_{mesh_name}"
                           if stock_collisions and mesh_name in stock_collisions
                           else mesh_name)
                    stock_routing[key] = entries

            stock_tmp_gpk = os.path.join(
                builder_dir, f"_tmp_stock_{character}.gpk"
            )
            try:
                convert(
                    glb_path=stock_glb,
                    gpk_path=original_gpk,
                    sdb_path=sdb_path,
                    dll_path=dll_path,
                    output_gpk=stock_tmp_gpk,
                    manifest_dict=stock_manifest,
                    allow_topology_change=True,
                    patch_animations=False,
                    new_mesh_routing=stock_routing or None,
                )
                stock_gpk_entries = extract_gpk(stock_tmp_gpk)
            except Exception as e:
                print(f"    stock: accessory-merge build failed ({e}); "
                      f"falling back to raw-copy")
            finally:
                if os.path.isfile(stock_tmp_gpk):
                    os.unlink(stock_tmp_gpk)
                if (len(accessory_mods) > 1 and stock_glb
                        and os.path.isfile(stock_glb)):
                    os.unlink(stock_glb)

        if stock_gpk_entries is None:
            stock_gpk_entries = extract_gpk(original_gpk)
            stock_source_label = "raw-copy"
        else:
            stock_source_label = "stock+accessories"
    elif stock_entries_touched:
        stock_gpk_entries = extract_gpk(original_gpk)
        stock_source_label = "raw-copy"
    else:
        stock_gpk_entries = None  # nothing to emit

    if stock_entries_touched and stock_gpk_entries is not None:
        sorted_touched = sorted(stock_entries_touched)
        for idx, stock_entry in enumerate(sorted_touched):
            if stock_entry not in stock_gpk_entries:
                print(f"    stock: '{stock_entry}' not in stock GPK — skipping")
                continue
            stock_vname = _variant_entry_name(character, "Stock", idx)
            main_entries[stock_vname] = stock_gpk_entries[stock_entry]
            variants.setdefault(stock_entry, {})["stock"] = stock_vname
            print(f"    stock: {stock_entry} -> {stock_vname} "
                  f"({len(stock_gpk_entries[stock_entry]):,} bytes, "
                  f"{stock_source_label})")

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
            'mod_json_path': mod_json_path,
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

    def _verify_variants_in_gpk(vmap, gpk_path, character):
        """Drop claimed variants whose entry names aren't actually in the
        merged GPK on disk.  Guards against the trap where a previous
        build produced a variant-less GPK (e.g. because mod.json.type was
        temporarily clobbered to "") but the cache key was still valid,
        leaving status.json claiming variants the runtime can't resolve
        — observed as swap_to_variant hash=0 with no user-visible error.
        Returns the filtered map."""
        try:
            from gpk_pack import extract_gpk_raw
            gpk_entries = set(extract_gpk_raw(gpk_path).keys())
        except Exception as e:
            print(f"  {character}: WARNING could not verify variants "
                  f"against GPK: {e}")
            return vmap
        filtered = {}
        dropped = []
        for stock_entry, variants in vmap.items():
            kept = {k: v for k, v in variants.items() if v in gpk_entries}
            for k, v in variants.items():
                if v not in gpk_entries:
                    dropped.append(f"{k}->{v}")
            if kept:
                filtered[stock_entry] = kept
        if dropped:
            print(f"  {character}: WARNING {len(dropped)} claimed "
                  f"variant(s) missing from GPK, stripped from "
                  f"status.json: {', '.join(dropped[:3])}"
                  f"{'...' if len(dropped) > 3 else ''}")
            print(f"  {character}: GPK is likely stale — delete "
                  f"{os.path.basename(gpk_path)}.cache_key and relaunch "
                  f"to force rebuild")
        return filtered

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
            mod_json_mtime = (os.path.getmtime(mi['mod_json_path'])
                              if os.path.isfile(mi.get('mod_json_path', '')) else 0)
            # Include mod.json mtime so type/routing edits invalidate
            # the cache.  Without this, a user whose mod.json type
            # field gets repaired (e.g. "" -> "mesh_replace") sees the
            # OLD broken GPK reused because only glb_mtime was tracked.
            current_key += f"{mi['id']}:{glb_mtime}:{mod_json_mtime}\n"
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
                # Cross-check against the GPK's real entry list — see
                # _verify_variants_in_gpk docstring for the failure mode
                # this guards against.
                cached_vmap = _verify_variants_in_gpk(
                    cached_vmap, output_gpk, character)
                if cached_vmap:
                    status_characters[character]["variants"] = cached_vmap
                # v3.11 — animation aliases on cache hits.  Same shape
                # as the rebuild branch.  The cache key encodes
                # mod.json content, so on cache hit the alias set is
                # guaranteed to match what was last built.
                _, cached_aliases = _collect_animation_adds(
                    [mi for mi in char_mods
                     if _classify_mod(mi['mod'])[3]],
                    character)
                status_characters[character]["alias_animations"] = cached_aliases
                # v3.12: persist skins registry on cache hit too.
                cached_skins = _skins_map_for(char_mods)
                if cached_skins:
                    status_characters[character]["skins"] = cached_skins
                continue
            else:
                print(f"  {character}: mods changed, rebuilding...")
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
        animation_only_mods = []
        animation_add_mods = []
        for mi in char_mods:
            is_variant, is_accessory, is_animation_only, is_animation_add = \
                _classify_mod(mi['mod'])
            if is_variant:
                variant_mods.append(mi)
            elif is_accessory:
                accessory_mods.append(mi)
            elif is_animation_only:
                animation_only_mods.append(mi)
            # animation_add is orthogonal — a variant or accessory mod
            # can ALSO ship new animations.  Track separately.
            if is_animation_add:
                animation_add_mods.append(mi)

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

        # v3.11 — collect animation_add specs + alias data.  Aliases
        # are persisted to status.json so the Lua runtime can register
        # SJSON entries on next launch.
        add_specs, alias_entries = _collect_animation_adds(
            animation_add_mods, character)
        # If any animation_add entry sources a GLB action, the GLB
        # animations need to be parsed too — same flag drives both
        # animation_patch and animation_add curve injection.
        if any(s.get('glb_action') for s in add_specs):
            patch_anims = True

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
                add_animations=add_specs or None,
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
                variants_map = _verify_variants_in_gpk(
                    variants_map, output_gpk, character)
                if variants_map:
                    status_characters[character]["variants"] = variants_map

            # v3.11 — persist alias entries so main.lua can register
            # SJSON injections at next launch.  Empty list is fine; we
            # write [] rather than omit the key so the runtime can
            # detect "no aliases this build" cleanly.
            status_characters[character]["alias_animations"] = alias_entries

            # v3.12 — persist skins registry for the manager's picker.
            built_skins = _skins_map_for(char_mods)
            if built_skins:
                status_characters[character]["skins"] = built_skins
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

    # v3.12: second pass for texture-only mods (no GLB).  These are
    # `texture_replace` mods authored entirely via the folder-mirror
    # convention — they don't need to be in by_character (which drives
    # the GPK merge loop) but they DO need their .pkg built and a skin
    # entry in cg3h_status.json.
    texture_only_skins = {}  # character → { mod_id: skin_record }

    # v3.12: cache stock PKG entry names so we can warn modders when
    # they ship a texture for a path the game doesn't actually own
    # (typo prevention, surfaces silent no-ops at build time).  Empty
    # set → permissive mode (validation skipped).
    stock_pkg_entries = load_or_build_pkg_entry_set(game_dir, builder_dir)
    if stock_pkg_entries:
        print(f"PKG entry validation: {len(stock_pkg_entries)} stock "
              f"entries indexed")
    # Lowercase fast-lookup mirror for case-insensitive matching.
    stock_pkg_entries_ci = {e.lower() for e in stock_pkg_entries}
    for entry in sorted(os.listdir(plugins_data_dir)):
        mod_dir = os.path.join(plugins_data_dir, entry)
        if not os.path.isdir(mod_dir):
            continue
        mod_json_path = os.path.join(mod_dir, 'mod.json')
        if not os.path.isfile(mod_json_path):
            for sub in os.listdir(mod_dir):
                cand = os.path.join(mod_dir, sub, 'mod.json')
                if os.path.isfile(cand):
                    mod_json_path = cand
                    mod_dir = os.path.dirname(cand)
                    break
        if not os.path.isfile(mod_json_path):
            continue
        try:
            with open(mod_json_path) as f:
                mod = json.load(f)
        except Exception:
            continue
        if not mod.get('format', '').startswith('cg3h-mod'):
            continue
        # Skip mods that already went through the mesh build loop.
        if mod.get('assets', {}).get('glb'):
            continue
        # Only handle texture_replace types here.  Other GLB-less
        # types (animation_only, animation_add) have their own paths.
        mod_type = mod.get('type', '')
        types = mod_type if isinstance(mod_type, list) \
            else [mod_type] if mod_type else []
        if 'texture_replace' not in types:
            continue
        character = mod.get('target', {}).get('character', '')
        if not character:
            continue
        if not _is_mod_enabled(mod_state, entry):
            continue

        # Sync mod.json with the textures/ folder layout, then read it
        # back to get the populated assets.textures.
        try:
            _sync_mod_json(mod_dir)
        except Exception as e:
            print(f"  WARNING: {entry}: _sync_mod_json failed: {e}")
            continue
        with open(mod_json_path) as f:
            mod = json.load(f)
        textures = mod.get('assets', {}).get('textures', []) or []
        replaces = [t for t in textures if t.get('replaces')]
        if not replaces:
            continue

        # Build the per-mod replacement PKG NEXT TO mod.json.  Runtime
        # `load_textures` resolves the pkg as <mod.path>/<filename>,
        # where mod.path is the dir containing mod.json — which is
        # `mod_dir` here (already adjusted for r2modman nesting above).
        #
        # v3.12 B: rename each PKG entry to a unique form (`<mod_id>_
        # <basename>`) so it doesn't collide with the stock entry of the
        # same name.  The runtime then calls SetThingProperty(
        # GrannyTexture=<unique_name>) for live skin swap (MelSkin-style).
        # The modder's folder-mirror layout is unchanged — it tells us
        # WHICH stock atlas to swap; we just rebrand at pack time.
        pkg_path = os.path.join(
            mod_dir, f"{CG3H_BUILDER_FOLDER}-{entry}.pkg")
        pkg_textures = []
        granny_texture_name = None  # primary atlas → SetThingProperty target
        for tex in replaces:
            pkg_entry_name = tex.get('pkg_entry_name')
            tex_file = tex.get('file', '')
            full_path = os.path.join(mod_dir, tex_file)
            if not pkg_entry_name or not os.path.isfile(full_path):
                continue
            # v3.12: validation — warn (don't fail) when the modder
            # mirrored a path the game doesn't own.  Catches typos
            # like `textures/GR2/melinoe_color512.png` (lowercase)
            # before they ship as silent no-ops.  Skipped when the
            # stock index is empty (no game_dir or scan failed).
            if (stock_pkg_entries_ci
                    and pkg_entry_name.lower() not in stock_pkg_entries_ci):
                print(f"  WARNING: {entry}: {tex_file} mirrors "
                      f"PKG path {pkg_entry_name!r} which is NOT in any "
                      f"stock pkg_manifest — possible typo, override "
                      f"will silently no-op in-game")
            basename = pkg_entry_name.replace('\\', '/').split('/')[-1]
            unique_name = f"{entry}_{basename}"
            pkg_textures.append({
                'name': unique_name, 'png_path': full_path,
                'width': 512, 'height': 512, 'fmt': 0x1C, 'mip_count': 6,
            })
            # The first texture in the list becomes the primary atlas
            # SetThingProperty targets.  Multi-texture skins are out of
            # scope for live swap in v3.12.0.
            if granny_texture_name is None:
                granny_texture_name = unique_name
        if not pkg_textures:
            continue
        try:
            build_standalone_pkg(pkg_textures, pkg_path)
            print(f"  {entry}: built texture PKG with "
                  f"{len(pkg_textures)} replacement(s), "
                  f"primary={granny_texture_name!r}")
        except Exception as e:
            print(f"  WARNING: {entry}: PKG build failed: {e}")
            continue

        # Stash for status.json — the main skins-merge code below picks
        # this up alongside the mesh-bearing builds' skins.
        meta = mod.get('metadata', {})
        texture_only_skins.setdefault(character, {})[entry] = {
            'name': meta.get('name', entry),
            'version': meta.get('version', ''),
            'granny_texture': granny_texture_name,  # SetThingProperty target
            'pkg_entries': sorted(p['name'] for p in pkg_textures),
            'preview': None,
        }
        # Make sure the character has at least a stub status record so
        # the manager renders a Characters-tab entry for skin-only mods.
        if character not in status_characters:
            status_characters[character] = {
                'state': 'cached',
                'gpk_path': None,
                'mods': [entry],
                'mod_details': [{
                    'id': entry,
                    'name': meta.get('name', entry),
                    'version': meta.get('version', ''),
                    'author': meta.get('author', ''),
                    'enabled': True,
                }],
                'error': None,
                'duration_ms': 0,
            }

    # Merge texture-only skins into the per-character skins block.
    # texture_replace mods that ride alongside a mesh_replace mod for
    # the same character were already covered by the mesh-build loop's
    # _skins_map_for; this only adds the no-GLB-only ones.
    for character, skins in texture_only_skins.items():
        existing = status_characters.setdefault(character, {}).get("skins", {})
        existing.update(skins)
        status_characters[character]["skins"] = existing

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
