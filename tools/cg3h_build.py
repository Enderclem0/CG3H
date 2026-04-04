"""
CG3H Build — Produces Hell2Modding-compatible mod packages.

Usage:
    python cg3h_build.py <mod_dir>
    python cg3h_build.py <mod_dir> --package  (also creates Thunderstore ZIP)

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


def build_mod(mod_dir, game_dir=None):
    """Build an H2M-compatible mod package from a mod.json directory."""
    mod_json_path = os.path.join(mod_dir, 'mod.json')
    if not os.path.isfile(mod_json_path):
        print(f"ERROR: No mod.json found in {mod_dir}")
        return False

    with open(mod_json_path) as f:
        mod = json.load(f)

    meta = mod.get('metadata', {})
    mod_type = mod.get('type', '')
    target = mod.get('target', {})
    assets = mod.get('assets', {})
    character = target.get('character', '')
    author = meta.get('author', 'Unknown')
    name = meta.get('name', 'UnnamedMod')
    mod_id = f"{author}-{name}".replace(' ', '')

    print(f"Building: {meta.get('name', '?')} by {author}")
    print(f"  Type: {mod_type}")
    print(f"  Character: {character}")

    # Auto-detect game dir
    if game_dir is None:
        for p in [
            "C:/Program Files (x86)/Steam/steamapps/common/Hades II",
            "D:/Steam/steamapps/common/Hades II",
        ]:
            if os.path.isdir(p):
                game_dir = p
                break
    if not game_dir:
        print("ERROR: Game directory not found. Use --game-dir.")
        return False

    ship_dir = os.path.join(game_dir, "Ship")
    gpk_dir = os.path.join(game_dir, "Content", "GR2", "_Optimized")
    dll_path = os.path.join(ship_dir, "granny2_x64.dll")

    # Setup build output
    build_dir = os.path.join(mod_dir, 'build')
    plugins_data = os.path.join(build_dir, 'plugins_data', mod_id)
    plugins = os.path.join(build_dir, 'plugins', mod_id)
    os.makedirs(plugins_data, exist_ok=True)
    os.makedirs(plugins, exist_ok=True)

    # ── Build GPK (if mod has meshes) ──
    glb_path = os.path.join(mod_dir, assets.get('glb', ''))
    mesh_entries = target.get('mesh_entries', [f"{character}_Mesh"])

    if mod_type in ('mesh_add', 'mesh_replace', 'mesh_patch') and os.path.isfile(glb_path):
        gpk_path = os.path.join(gpk_dir, f"{character}.gpk")
        sdb_path = os.path.join(gpk_dir, f"{character}.sdb")

        if not os.path.isfile(gpk_path):
            print(f"ERROR: Original {character}.gpk not found at {gpk_path}")
            return False

        output_gpk = os.path.join(plugins_data, f"{character}.gpk")

        print(f"\n  Building GPK: {glb_path} -> {output_gpk}")

        from gltf_to_gr2 import convert
        try:
            # Use manifest from export if available
            manifest_path = os.path.join(mod_dir, 'manifest.json')
            convert(
                glb_path=glb_path,
                gpk_path=gpk_path,
                sdb_path=sdb_path,
                dll_path=dll_path,
                output_gpk=output_gpk,
                manifest_path=manifest_path if os.path.isfile(manifest_path) else None,
                allow_topology_change=(mod_type in ('mesh_replace', 'mesh_add')),
            )
        except Exception as e:
            print(f"ERROR: GPK build failed: {e}")
            import traceback
            traceback.print_exc()
            return False

    # ── Build standalone PKG (if mod has custom textures) ──
    tex_list = assets.get('textures', [])
    custom_textures = [t for t in tex_list if t.get('custom', True)]

    if custom_textures:
        pkg_path = os.path.join(plugins_data, f"{mod_id}.pkg")
        pkg_manifest_path = pkg_path + '_manifest'

        print(f"\n  Building PKG: {len(custom_textures)} custom texture(s)")

        from pkg_texture import build_standalone_pkg, png_to_dds

        pkg_textures = []
        for tex in custom_textures:
            tex_name = tex.get('name', '')
            tex_file = tex.get('file', '')
            full_path = os.path.join(mod_dir, tex_file)

            if not os.path.isfile(full_path):
                print(f"  WARNING: texture file not found: {full_path}")
                continue

            entry_name = f"GR2\\{tex_name}"
            w = tex.get('width', 512)
            h = tex.get('height', 512)

            if full_path.lower().endswith('.png'):
                pkg_textures.append({
                    'name': entry_name,
                    'png_path': full_path,
                    'width': min(w, 512),
                    'height': min(h, 512),
                    'fmt': 0x1C,
                    'mip_count': 6,
                })
            elif full_path.lower().endswith('.dds'):
                pkg_textures.append({
                    'name': entry_name,
                    'dds_path': full_path,
                })

        if pkg_textures:
            build_standalone_pkg(pkg_textures, pkg_path, pkg_manifest_path)

    # ── Generate H2M manifest ──
    h2m_manifest = {
        "name": name,
        "version_number": meta.get('version', '1.0.0'),
        "website_url": meta.get('url', ''),
        "description": meta.get('description', ''),
        "dependencies": ["Hell2Modding-Hell2Modding-0.2.0"],
    }
    with open(os.path.join(plugins, 'manifest.json'), 'w') as f:
        json.dump(h2m_manifest, f, indent=2)

    # ── Generate companion Lua ──
    lua_lines = [
        f'-- Auto-generated by CG3H v3.0',
        f'-- Mod: {name} by {author}',
        f'',
    ]

    # If we built a custom .pkg, load it when game Lua state is ready
    if custom_textures:
        lua_lines.extend([
            f'-- Load custom texture package after game Lua is initialized',
            f'local _loaded = false',
            f'rom.on_import.post(function(script_name)',
            f'    if _loaded then return end',
            f'    if script_name == "Main.lua" then',
            f'        _loaded = true',
            f'        local pkg_path = rom.path.combine(_PLUGIN.plugins_data_mod_folder_path, _PLUGIN.guid)',
            f'        rom.game.LoadPackages{{Name = pkg_path}}',
            f'        rom.log.info("[CG3H] Loaded package: " .. pkg_path)',
            f'    end',
            f'end)',
            f'rom.log.info("[CG3H] Registered: {name} (package will load on Main.lua import)")',
        ])
    else:
        lua_lines.append(f'rom.log.info("[CG3H] Loaded: {name}")')

    with open(os.path.join(plugins, 'main.lua'), 'w') as f:
        f.write('\n'.join(lua_lines) + '\n')

    print(f"\n  Build complete!")
    print(f"  Output: {build_dir}")
    return True


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
            "dependencies": ["Hell2Modding-Hell2Modding-0.2.0"],
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

        # Include source assets for user-side GPK build
        zf.writestr('mod.json', json.dumps(mod, indent=2))
        assets_cfg = mod.get('assets', {})
        glb = assets_cfg.get('glb', '')
        if glb:
            glb_full = os.path.join(mod_dir, glb)
            if os.path.isfile(glb_full):
                zf.write(glb_full, glb)

        # Include export manifest if present (for mesh routing)
        manifest = os.path.join(mod_dir, 'manifest.json')
        if os.path.isfile(manifest):
            zf.write(manifest, 'manifest.json')

    print(f"\n  Thunderstore package: {zip_path}.zip")
    return True


def main():
    parser = argparse.ArgumentParser(description="CG3H Build — H2M mod builder")
    parser.add_argument('mod_dir', help='Directory containing mod.json')
    parser.add_argument('--game-dir', default=None,
                        help='Hades II game directory (auto-detected from Steam)')
    parser.add_argument('--package', action='store_true',
                        help='Also create Thunderstore ZIP')
    args = parser.parse_args()

    ok = build_mod(args.mod_dir, game_dir=args.game_dir)
    if ok and args.package:
        package_thunderstore(args.mod_dir)


if __name__ == '__main__':
    main()
