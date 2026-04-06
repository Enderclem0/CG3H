"""
CG3H Mod Merger — Merges multiple mods targeting the same character into
a single GPK + PKG for H2M.

When two mesh_add mods both target Melinoe, we can't have two Melinoe.gpk
files. The merger:
1. Scans installed CG3H mods (plugins_data + plugins)
2. Loads mod_priority.json for merge order (generates default if missing)
3. Groups by target character
4. For each character with multiple mods: builds a merged GPK + merged PKG
5. Places the merged files in a shared location H2M can find

Usage:
    python mod_merger.py <r2_plugins_dir> [--game-dir DIR]
"""
import argparse
import json
import os
import shutil
import sys

STEAM_PATHS = [
    "C:/Program Files (x86)/Steam/steamapps/common/Hades II",
    "C:/Program Files/Steam/steamapps/common/Hades II",
    "D:/Steam/steamapps/common/Hades II",
    "D:/SteamLibrary/steamapps/common/Hades II",
    "E:/SteamLibrary/steamapps/common/Hades II",
]

_tools_dir = os.path.dirname(os.path.abspath(__file__))
if _tools_dir not in sys.path:
    sys.path.insert(0, _tools_dir)


def load_priority(r2_base_dir):
    """
    Load mod_priority.json — defines merge order per character.
    Higher index = applied later = wins conflicts.
    Returns {character: [mod_id_list_in_order]} or empty dict.
    """
    path = os.path.join(r2_base_dir, 'cg3h_mod_priority.json')
    if os.path.isfile(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_priority(r2_base_dir, priority):
    """Save mod_priority.json."""
    path = os.path.join(r2_base_dir, 'cg3h_mod_priority.json')
    with open(path, 'w') as f:
        json.dump(priority, f, indent=2)
    print(f"  Saved priority: {path}")


def generate_default_priority(r2_base_dir, mods):
    """
    Generate default mod_priority.json from installed mods.
    Default order: alphabetical by mod id.
    Only generates for characters with multiple mods.
    Preserves existing priority entries, appends new mods at the end.
    """
    existing = load_priority(r2_base_dir)
    groups = group_by_character(mods)
    changed = False

    for character, char_mods in groups.items():
        if len(char_mods) <= 1:
            continue
        mod_ids = [m['id'] for m in char_mods]
        if character not in existing:
            # New character — alphabetical default
            existing[character] = sorted(mod_ids)
            changed = True
        else:
            # Existing — append any new mods not yet in the list
            for mid in mod_ids:
                if mid not in existing[character]:
                    existing[character].append(mid)
                    changed = True
            # Remove mods no longer installed
            existing[character] = [mid for mid in existing[character] if mid in mod_ids]

    if changed:
        save_priority(r2_base_dir, existing)
    return existing


def scan_cg3h_mods(r2_base_dir):
    """
    Scan ReturnOfModding directory for installed CG3H mods.
    Returns list of mod dicts with paths and metadata.
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

        # Check for mod.json in plugins_data
        data_path = os.path.join(plugins_data_dir, entry)
        mod_json = os.path.join(data_path, 'mod.json')

        # Also check parent of plugins_data (Thunderstore layout)
        if not os.path.isfile(mod_json):
            # Try finding mod.json alongside the plugin
            for search in [plugin_path, data_path]:
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
    """Group mods by target character. Returns {character: [mod_list]}."""
    groups = {}
    for m in mods:
        char = m['mod'].get('target', {}).get('character', '')
        if char:
            groups.setdefault(char, []).append(m)
    return groups


def check_conflicts(group):
    """
    Check a group of mods targeting the same character for conflicts.
    Returns (warnings, errors) lists.
    """
    from cg3h_build import _infer_operations

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

    return warnings, errors


def merge_character_mods(character, mods, game_dir, output_dir):
    """
    Merge multiple mods for the same character into a single GPK + PKG.

    Args:
        character: character name (e.g. "Melinoe")
        mods: list of mod dicts from scan_cg3h_mods
        game_dir: game installation directory
        output_dir: where to write merged files
    """
    print(f"\n{'='*60}")
    print(f"  Merging {len(mods)} mod(s) for {character}")
    print(f"{'='*60}")

    for m in mods:
        name = m['mod'].get('metadata', {}).get('name', m['id'])
        mtype = m['mod'].get('type', '?')
        print(f"  - {name} ({mtype})")

    # Check conflicts
    warnings, errors = check_conflicts(mods)
    for w in warnings:
        print(f"  {w}")
    for e in errors:
        print(f"  {e}")

    if errors:
        print(f"  Skipping merge due to conflicts")
        return False

    ship_dir = os.path.join(game_dir, "Ship")
    gpk_dir = os.path.join(game_dir, "Content", "GR2", "_Optimized")
    dll_path = os.path.join(ship_dir, "granny2_x64.dll")

    # Collect all GLB files from all mods
    glb_files = []
    manifest_files = []
    custom_textures = []

    for m in mods:
        mod = m['mod']
        mod_dir = os.path.dirname(m['mod_json_path'])
        glb = mod.get('assets', {}).get('glb', '')
        if glb:
            glb_path = os.path.join(mod_dir, glb)
            if os.path.isfile(glb_path):
                glb_files.append(glb_path)
            # Also check data_path
            elif os.path.isfile(os.path.join(m['data_path'], glb)):
                glb_files.append(os.path.join(m['data_path'], glb))

        manifest = os.path.join(mod_dir, 'manifest.json')
        if os.path.isfile(manifest):
            manifest_files.append(manifest)

        for tex in mod.get('assets', {}).get('textures', []):
            if tex.get('custom'):
                tex_file = os.path.join(mod_dir, tex.get('file', ''))
                if os.path.isfile(tex_file):
                    custom_textures.append({
                        'name': tex.get('name', ''),
                        'file': tex_file,
                        'mod_id': m['id'],
                    })

    os.makedirs(output_dir, exist_ok=True)

    # Build merged GPK: import each GLB sequentially
    if glb_files:
        gpk_path = os.path.join(gpk_dir, f"{character}.gpk")
        sdb_path = os.path.join(gpk_dir, f"{character}.sdb")

        if not os.path.isfile(gpk_path):
            print(f"  ERROR: Original {character}.gpk not found")
            return False

        output_gpk = os.path.join(output_dir, f"{character}.gpk")
        print(f"\n  Building merged GPK from {len(glb_files)} GLB(s)...")

        from gltf_to_gr2 import convert

        # First mod: build from original
        # Subsequent mods: build from previous output
        current_gpk = gpk_path
        for i, glb_path in enumerate(glb_files):
            is_last = (i == len(glb_files) - 1)
            out = output_gpk if is_last else os.path.join(output_dir, f"{character}_tmp_{i}.gpk")

            manifest = manifest_files[i] if i < len(manifest_files) else None

            try:
                convert(
                    glb_path=glb_path,
                    gpk_path=current_gpk,
                    sdb_path=sdb_path,
                    dll_path=dll_path,
                    output_gpk=out,
                    manifest_path=manifest,
                    allow_topology_change=True,
                )
                current_gpk = out
            except Exception as e:
                print(f"  ERROR: Failed to merge GLB {i}: {e}")
                import traceback
                traceback.print_exc()
                return False

        # Clean up temp files
        for i in range(len(glb_files) - 1):
            tmp = os.path.join(output_dir, f"{character}_tmp_{i}.gpk")
            if os.path.isfile(tmp):
                os.unlink(tmp)

        print(f"  Merged GPK: {output_gpk}")

    # Build merged PKG with all custom textures
    if custom_textures:
        from pkg_texture import build_standalone_pkg

        # Determine merged pkg name from first mod
        first_id = mods[0]['id']
        merged_pkg_name = f"CG3H-Merged-{character}"
        pkg_path = os.path.join(output_dir, f"{merged_pkg_name}.pkg")

        pkg_entries = []
        for tex in custom_textures:
            entry_name = f"GR2\\{tex['name']}"
            if tex['file'].lower().endswith('.png'):
                pkg_entries.append({
                    'name': entry_name,
                    'png_path': tex['file'],
                    'width': 512, 'height': 512,
                    'fmt': 0x1C, 'mip_count': 6,
                })
            elif tex['file'].lower().endswith('.dds'):
                pkg_entries.append({
                    'name': entry_name,
                    'dds_path': tex['file'],
                })

        if pkg_entries:
            build_standalone_pkg(pkg_entries, pkg_path)
            print(f"  Merged PKG: {pkg_path}")

    # Generate merged Lua companion
    lua_path = os.path.join(output_dir, 'main.lua')
    mod_names = [m['mod'].get('metadata', {}).get('name', m['id']) for m in mods]
    lua_lines = [
        f'-- CG3H Merged: {character}',
        f'-- Mods: {", ".join(mod_names)}',
        f'',
    ]
    if custom_textures:
        merged_pkg_name = f"CG3H-Merged-{character}"
        lua_lines.extend([
            f'local _loaded = false',
            f'rom.on_import.post(function(script_name)',
            f'    if _loaded then return end',
            f'    if script_name == "Main.lua" then',
            f'        _loaded = true',
            f'        local pkg_path = rom.path.combine(_PLUGIN.plugins_data_mod_folder_path, "{merged_pkg_name}")',
            f'        rom.game.LoadPackages{{Name = pkg_path}}',
            f'        rom.log.info("[CG3H] Loaded merged package for {character}")',
            f'    end',
            f'end)',
        ])
    lua_lines.append(f'rom.log.info("[CG3H] Merged: {character} ({len(mods)} mods)")')

    with open(lua_path, 'w') as f:
        f.write('\n'.join(lua_lines) + '\n')

    print(f"\n  Merge complete for {character}")
    return True


def merge_all(r2_base_dir, game_dir=None):
    """Scan for all CG3H mods and merge where needed."""
    print("Scanning for CG3H mods...")
    mods = scan_cg3h_mods(r2_base_dir)
    print(f"Found {len(mods)} CG3H mod(s)")

    if not mods:
        return

    groups = group_by_character(mods)
    print(f"Characters: {', '.join(groups.keys())}")

    # Generate/update priority file
    priority = generate_default_priority(r2_base_dir, mods)
    if priority:
        print(f"Mod priority: {json.dumps(priority, indent=2)}")

    # Auto-detect game dir
    if game_dir is None:
        for p in STEAM_PATHS:
            if os.path.isdir(p):
                game_dir = p
                break

    if not game_dir:
        print("ERROR: Game directory not found. Use --game-dir.")
        return

    plugins_data = os.path.join(r2_base_dir, 'plugins_data')

    for character, char_mods in sorted(groups.items()):
        # Sort mods by priority (higher index = applied later = wins conflicts)
        char_priority = priority.get(character, [])
        if char_priority:
            mod_id_to_idx = {mid: i for i, mid in enumerate(char_priority)}
            char_mods.sort(key=lambda m: mod_id_to_idx.get(m['id'], 999))

        if len(char_mods) == 1:
            print(f"\n{character}: 1 mod, no merge needed")
            continue

        # Merge into a shared CG3H-Merged directory
        output_dir = os.path.join(plugins_data, f"CG3H-Merged-{character}")
        merge_character_mods(character, char_mods, game_dir, output_dir)


def main():
    parser = argparse.ArgumentParser(description="CG3H Mod Merger")
    parser.add_argument('r2_dir', help='ReturnOfModding directory '
                        '(e.g. .../profiles/Default/ReturnOfModding)')
    parser.add_argument('--game-dir', default=None,
                        help='Hades II game directory')
    parser.add_argument('--character', default=None,
                        help='Only merge mods for this character')
    args = parser.parse_args()

    if args.character:
        mods = scan_cg3h_mods(args.r2_dir)
        groups = group_by_character(mods)
        if args.character in groups:
            game_dir = args.game_dir
            if not game_dir:
                for p in STEAM_PATHS:
                    if os.path.isdir(p):
                        game_dir = p
                        break
            output_dir = os.path.join(args.r2_dir, 'plugins_data',
                                      f"CG3H-Merged-{args.character}")
            merge_character_mods(args.character, groups[args.character],
                                game_dir, output_dir)
        else:
            print(f"No mods found for {args.character}")
    else:
        merge_all(args.r2_dir, args.game_dir)


if __name__ == '__main__':
    main()
