"""
Build the CG3H Blender addon ZIP locally.

Mirrors the .github/workflows/release.yml recipe plus two files CI misses
(`cg3h_core.py`, `cg3h_constants.py` — both required at runtime) so a
dev build and a release build produce the same addon.

Output: `dist/cg3h_blender_addon.zip` — unzip into Blender's addons
directory or use `--install` to deploy directly.

Usage:
    python tools/build_blender_addon.py              # build zip
    python tools/build_blender_addon.py --install    # build + install into Blender 4.2 addons
    python tools/build_blender_addon.py --no-build   # skip PyInstaller rebuild
"""
import argparse
import os
import shutil
import subprocess
import sys
import zipfile

_tools = os.path.dirname(os.path.abspath(__file__))
_repo = os.path.dirname(_tools)

# Files that live alongside __init__.py inside the addon dir at runtime.
# Keep in sync with what __init__.py + cg3h_build.py expect to import or
# invoke via _addon_dir().
BUNDLE_SOURCES = [
    # Source (tracked):
    ('blender_addon/cg3h/__init__.py',  'addon'),
    ('blender_addon/cg3h/cg3h_core.py', 'addon'),
    ('tools/cg3h_build.py',             'addon'),
    ('tools/cg3h_constants.py',         'addon'),
    ('tools/pkg_texture.py',            'addon'),
    # PyInstaller build artifacts (rebuild if --no-build is NOT passed):
    ('dist/addon/cg3h_exporter.exe',    'addon'),
    ('dist/addon/cg3h_importer.exe',    'addon'),
    ('dist/builder/cg3h_builder.exe',   'addon'),
]

PYINSTALLER_SPECS = [
    # (spec, distpath-relative-to-repo, workpath-relative-to-repo)
    ('cg3h_builder.spec',  'dist/builder', 'build/pyinstaller/cg3h_builder'),
    ('gr2_to_gltf.spec',   'dist/addon',   'build/pyinstaller/cg3h_exporter'),
    ('gltf_to_gr2.spec',   'dist/addon',   'build/pyinstaller/cg3h_importer'),
]


def _rebuild_exes():
    """Run PyInstaller for the 3 addon-bundled executables."""
    for spec, distpath, workpath in PYINSTALLER_SPECS:
        print(f"[build] PyInstaller -> {spec}")
        subprocess.check_call(
            [sys.executable, '-m', 'PyInstaller', spec,
             '--distpath', os.path.join(_repo, distpath),
             '--workpath', os.path.join(_repo, workpath),
             '--noconfirm'],
            cwd=_tools,
        )


def _assemble(build_dir):
    """Copy every bundle source into `build_dir/cg3h/`."""
    dst = os.path.join(build_dir, 'cg3h')
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    os.makedirs(dst)

    missing = []
    for src_rel, _ in BUNDLE_SOURCES:
        src = os.path.join(_repo, src_rel)
        if not os.path.isfile(src):
            missing.append(src_rel)
            continue
        shutil.copy2(src, os.path.join(dst, os.path.basename(src_rel)))
        print(f"  + {os.path.basename(src_rel)}")
    if missing:
        raise SystemExit(
            f"ERROR: missing bundle inputs — run without --no-build to regenerate:\n  "
            + "\n  ".join(missing)
        )
    return dst


def _zip_bundle(bundle_dir, zip_path):
    """ZIP bundle_dir's PARENT so paths inside the zip start with `cg3h/`."""
    build_root = os.path.dirname(bundle_dir)
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(bundle_dir):
            for f in files:
                full = os.path.join(root, f)
                arc = os.path.relpath(full, build_root)
                z.write(full, arc)
    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"[build] wrote {zip_path} ({size_mb:.1f} MB)")


def _install_into_blender(bundle_dir, blender_version):
    """Copy the assembled cg3h/ directly into Blender's addons folder.

    Matches what Blender's "Install from disk" does for a zip, minus
    the restart dance.  After install the user just needs to enable
    (or re-enable) the addon from Preferences.
    """
    appdata = os.environ.get('APPDATA')
    if not appdata:
        raise SystemExit("ERROR: %APPDATA% not set — can't locate Blender addons dir")
    addons = os.path.join(
        appdata, 'Blender Foundation', 'Blender',
        blender_version, 'scripts', 'addons',
    )
    if not os.path.isdir(addons):
        raise SystemExit(
            f"ERROR: Blender {blender_version} addons dir not found: {addons}"
        )
    target = os.path.join(addons, 'cg3h')
    if os.path.isdir(target):
        shutil.rmtree(target)
    shutil.copytree(bundle_dir, target)
    print(f"[install] Blender {blender_version} addon at: {target}")


def main():
    ap = argparse.ArgumentParser(description="Build the CG3H Blender addon ZIP")
    ap.add_argument('--no-build', action='store_true',
                    help='skip PyInstaller rebuild (reuse existing dist/* exes)')
    ap.add_argument('--install', action='store_true',
                    help='also copy into the local Blender 4.2 addons dir')
    ap.add_argument('--blender-version', default='4.2',
                    help='target Blender version for --install (default: 4.2)')
    args = ap.parse_args()

    if not args.no_build:
        _rebuild_exes()

    build_dir = os.path.join(_repo, 'build', 'blender_addon')
    if os.path.isdir(build_dir):
        shutil.rmtree(build_dir)
    bundle = _assemble(build_dir)

    dist_dir = os.path.join(_repo, 'dist')
    os.makedirs(dist_dir, exist_ok=True)
    zip_path = os.path.join(dist_dir, 'cg3h_blender_addon.zip')
    if os.path.isfile(zip_path):
        os.unlink(zip_path)
    _zip_bundle(bundle, zip_path)

    if args.install:
        _install_into_blender(bundle, args.blender_version)

    print("[build] done")


if __name__ == '__main__':
    main()
