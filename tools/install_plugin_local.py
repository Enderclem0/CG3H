"""
Install the CG3HBuilder runtime plugin into the local r2modman profile.

Used for testing v3.7+ development without going through CI.  Rebuilds
cg3h_builder.exe via PyInstaller, then copies every file (manifest,
README, icon, all Lua modules, the exe) into the ReturnOfModding
plugins/ and plugins_data/ folders under the expected
``Enderclem-CG3HBuilder`` folder name.

Usage:
    python tools/install_plugin_local.py
    python tools/install_plugin_local.py --no-build   (skip pyinstaller)
    python tools/install_plugin_local.py --r2-dir <path>
"""
import argparse
import os
import shutil
import subprocess
import sys

_tools = os.path.dirname(os.path.abspath(__file__))
_repo = os.path.dirname(_tools)
if _tools not in sys.path:
    sys.path.insert(0, _tools)

from cg3h_constants import CG3H_BUILDER_FOLDER, CG3H_VERSION

THUNDERSTORE_SRC = os.path.join(_repo, '.github', 'thunderstore')
LUA_FILES = ['main.lua', 'mod_state.lua', 'runtime.lua', 'ui.lua']
DEFAULT_R2 = os.path.expandvars(
    r"%APPDATA%\r2modmanPlus-local\HadesII\profiles\Default\ReturnOfModding")


def _rebuild_exe():
    """Run pyinstaller to rebuild cg3h_builder.exe from current source."""
    print("[install] Rebuilding cg3h_builder.exe ...")
    subprocess.check_call(
        [sys.executable, '-m', 'PyInstaller', 'cg3h_builder.spec',
         '--distpath', os.path.join(_repo, 'dist', 'builder'),
         '--workpath', os.path.join(_repo, 'build', 'pyinstaller'),
         '--noconfirm'],
        cwd=_tools,
    )


def _copy_plugin(r2_dir):
    plugins = os.path.join(r2_dir, 'plugins', CG3H_BUILDER_FOLDER)
    plugins_data = os.path.join(r2_dir, 'plugins_data', CG3H_BUILDER_FOLDER)

    # Nuke and recreate so stale files from earlier installs can't linger.
    for d in (plugins, plugins_data):
        if os.path.isdir(d):
            shutil.rmtree(d)
        os.makedirs(d)

    # Lua modules go under plugins/{folder}/
    for f in LUA_FILES:
        src = os.path.join(THUNDERSTORE_SRC, f)
        if not os.path.isfile(src):
            print(f"  WARNING: missing {src}")
            continue
        shutil.copy2(src, os.path.join(plugins, f))
        print(f"  plugins/{CG3H_BUILDER_FOLDER}/{f}")

    # Plugin manifest — single source of truth is .github/thunderstore/
    # manifest.json.  Override the `name` field to the folder-matching
    # form (Enderclem-CG3HBuilder) so H2M's hyphen check passes and the
    # in-game UI resolves the right plugin; everything else (version,
    # dependencies, website_url, description) comes verbatim from the
    # Thunderstore manifest so the local install can't drift from what
    # Thunderstore ships.
    import json
    ts_manifest_path = os.path.join(THUNDERSTORE_SRC, 'manifest.json')
    with open(ts_manifest_path, 'r') as f:
        manifest_dict = json.load(f)
    manifest_dict['name'] = CG3H_BUILDER_FOLDER
    with open(os.path.join(plugins, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest_dict, f, ensure_ascii=False, indent=2)
    print(f"  plugins/{CG3H_BUILDER_FOLDER}/manifest.json"
          f"  (dep: {manifest_dict.get('dependencies', [])})")

    # Builder exe goes under plugins_data/{folder}/
    exe_src = os.path.join(_repo, 'dist', 'builder', 'cg3h_builder.exe')
    if not os.path.isfile(exe_src):
        raise SystemExit(f"ERROR: {exe_src} missing — run without --no-build")
    shutil.copy2(exe_src, os.path.join(plugins_data, 'cg3h_builder.exe'))
    print(f"  plugins_data/{CG3H_BUILDER_FOLDER}/cg3h_builder.exe")


def main():
    ap = argparse.ArgumentParser(description="Install CG3HBuilder locally for testing")
    ap.add_argument('--no-build', action='store_true', help='skip PyInstaller rebuild')
    ap.add_argument('--r2-dir', default=DEFAULT_R2,
                    help=f'ReturnOfModding dir (default: {DEFAULT_R2})')
    args = ap.parse_args()

    if not os.path.isdir(args.r2_dir):
        raise SystemExit(f"ERROR: r2modman profile not found: {args.r2_dir}")

    if not args.no_build:
        _rebuild_exe()

    print(f"[install] Copying plugin to {args.r2_dir}")
    _copy_plugin(args.r2_dir)
    print(f"[install] Done — CG3HBuilder {CG3H_VERSION} installed as "
          f"{CG3H_BUILDER_FOLDER}")


if __name__ == '__main__':
    main()
