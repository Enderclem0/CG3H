"""
mod_info — read CG3H mod metadata, group by character, detect conflicts.

Lightweight helpers extracted from the deleted mod_merger.py (the runtime
merging itself moved to cg3h_builder_entry.py at v3.0).  Used by the GUI
Mods tab and the test suite.
"""
import json
import os
import sys

import pygltflib

_tools_dir = os.path.dirname(os.path.abspath(__file__))
if _tools_dir not in sys.path:
    sys.path.insert(0, _tools_dir)

from cg3h_build import _infer_operations


def load_priority(r2_base_dir):
    """Load cg3h_mod_priority.json — defines merge order per character.

    Higher index = applied later = wins conflicts.
    Returns ``{character: [mod_id_list_in_order]}`` or an empty dict.
    """
    path = os.path.join(r2_base_dir, 'cg3h_mod_priority.json')
    if os.path.isfile(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_priority(r2_base_dir, priority):
    """Save cg3h_mod_priority.json."""
    path = os.path.join(r2_base_dir, 'cg3h_mod_priority.json')
    with open(path, 'w') as f:
        json.dump(priority, f, indent=2)
    print(f"  Saved priority: {path}")


def generate_default_priority(r2_base_dir, mods):
    """Generate (or update) cg3h_mod_priority.json from installed mods.

    Default order is alphabetical by mod id.  Only generates entries for
    characters with multiple mods.  Existing entries are preserved; new
    mods are appended at the end; uninstalled mods are removed.
    """
    existing = load_priority(r2_base_dir)
    groups = group_by_character(mods)
    changed = False

    for character, char_mods in groups.items():
        if len(char_mods) <= 1:
            continue
        mod_ids = [m['id'] for m in char_mods]
        if character not in existing:
            existing[character] = sorted(mod_ids)
            changed = True
        else:
            for mid in mod_ids:
                if mid not in existing[character]:
                    existing[character].append(mid)
                    changed = True
            existing[character] = [mid for mid in existing[character] if mid in mod_ids]

    if changed:
        save_priority(r2_base_dir, existing)
    return existing


def scan_cg3h_mods(r2_base_dir):
    """Scan a r2modman / Thunderstore profile for installed CG3H mods.

    Returns a list of mod info dicts with keys ``id``, ``mod_json_path``,
    ``plugin_path``, ``data_path``, ``mod`` (parsed mod.json).
    """
    plugins_dir = os.path.join(r2_base_dir, 'plugins')
    plugins_data_dir = os.path.join(r2_base_dir, 'plugins_data')
    mods = []

    if not os.path.isdir(plugins_dir):
        return mods

    for entry in sorted(os.listdir(plugins_dir)):
        plugin_path = os.path.join(plugins_dir, entry)
        if not os.path.isdir(plugin_path):
            continue

        data_path = os.path.join(plugins_data_dir, entry)
        mod_json = os.path.join(data_path, 'mod.json')

        if not os.path.isfile(mod_json):
            for search in (plugin_path, data_path):
                candidate = os.path.join(search, 'mod.json')
                if os.path.isfile(candidate):
                    mod_json = candidate
                    break

        if not os.path.isfile(mod_json):
            continue

        try:
            with open(mod_json) as f:
                mod = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        if mod.get('format', '').startswith('cg3h-mod'):
            mods.append({
                'id': entry,
                'mod_json_path': mod_json,
                'plugin_path': plugin_path,
                'data_path': data_path,
                'mod': mod,
            })

    return mods


def group_by_character(mods):
    """Group mods by target character. Returns ``{character: [mod_list]}``."""
    groups = {}
    for m in mods:
        char = m['mod'].get('target', {}).get('character', '')
        if char:
            groups.setdefault(char, []).append(m)
    return groups


def check_conflicts(group):
    """Check a group of mods targeting the same character for conflicts.

    Returns ``(warnings, errors)`` lists.  Errors are blocking conflicts;
    warnings are informational (e.g. mesh name auto-prefixing at merge).
    """
    warnings = []
    errors = []
    names = [m['mod'].get('metadata', {}).get('name', m['id']) for m in group]
    all_ops = [_infer_operations(m['mod']) for m in group]

    # Multiple mesh replacers = hard conflict
    replacers = [n for n, ops in zip(names, all_ops) if 'replaces_meshes' in ops]
    if len(replacers) > 1:
        errors.append(f"CONFLICT: {len(replacers)} mesh_replace mods: {', '.join(replacers)}")

    # mesh_add + mesh_replace = warning
    has_add = any('adds_meshes' in ops for ops in all_ops)
    has_replace = any('replaces_meshes' in ops for ops in all_ops)
    if has_add and has_replace:
        warnings.append("WARNING: mesh_add + mesh_replace -- additive meshes may not "
                        "work with the replacement model")

    # Same texture modified by multiple mods
    all_textures = {}
    for m in group:
        for tex in m['mod'].get('assets', {}).get('textures', []):
            tex_name = tex.get('name', '')
            if tex_name:
                all_textures.setdefault(tex_name, []).append(
                    m['mod'].get('metadata', {}).get('name', m['id']))
    for tex_name, mod_names in all_textures.items():
        if len(mod_names) > 1:
            errors.append(f"CONFLICT: texture '{tex_name}' modified by: {', '.join(mod_names)}")

    # mesh_add mods with shared mesh names — auto-prefixed at merge time
    adders = [m for m, ops in zip(group, all_ops) if 'adds_meshes' in ops]
    if len(adders) > 1:
        mesh_names_by_mod = {}
        for m in adders:
            glb_name = m['mod'].get('assets', {}).get('glb', '')
            if not glb_name:
                continue
            glb_path = os.path.join(m.get('mod_dir', ''), glb_name)
            if not os.path.isfile(glb_path):
                continue
            try:
                gltf = pygltflib.GLTF2().load(glb_path)
                for mesh in gltf.meshes:
                    mesh_names_by_mod.setdefault(mesh.name, []).append(
                        m['mod'].get('metadata', {}).get('name', m['id']))
            except Exception as e:
                warnings.append(f"WARNING: could not load {glb_path}: {e}")
        for mname, mods_list in mesh_names_by_mod.items():
            if len(mods_list) > 1:
                warnings.append(f"INFO: mesh '{mname}' used by {', '.join(mods_list)} "
                                f"— will be auto-prefixed at merge time")

    return warnings, errors
