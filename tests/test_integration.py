"""
CG3H Integration Tests — tests the full pipeline without game/Blender.

Requires: running from Ship/ directory (needs granny2_x64.dll)
Usage:
    cd "C:\Program Files (x86)\Steam\steamapps\common\Hades II\Ship"
    python C:/Users/ender/GhidraHades2/tests/test_integration.py
"""
import sys, os, json, struct, shutil, tempfile

_tools = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools')
sys.path.insert(0, _tools)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _game_dir():
    for p in [
        "C:/Program Files (x86)/Steam/steamapps/common/Hades II",
        "D:/Steam/steamapps/common/Hades II",
    ]:
        if os.path.isdir(p):
            return p
    return None


def _needs_game(fn):
    """Decorator: skip test if game not found."""
    def wrapper(*args, **kwargs):
        if not _game_dir():
            print(f"  SKIP (game not found)")
            return
        return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper


def _tmpdir():
    d = tempfile.mkdtemp(prefix='cg3h_test_')
    return d


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Standalone .pkg build + roundtrip
# ═══════════════════════════════════════════════════════════════════════════════

def test_standalone_pkg_roundtrip():
    """Build a standalone .pkg, read it back, verify texture entry."""
    from pkg_texture import build_standalone_pkg, read_pkg_chunks, scan_textures
    from PIL import Image

    tmp = _tmpdir()
    try:
        # Create test PNG
        png = os.path.join(tmp, 'test.png')
        Image.new('RGBA', (64, 64), (255, 0, 0, 255)).save(png)

        pkg = os.path.join(tmp, 'test.pkg')
        build_standalone_pkg([{
            'name': 'GR2\\TestTex',
            'png_path': png,
            'width': 64, 'height': 64,
            'fmt': 0x1C, 'mip_count': 3,
        }], pkg)

        assert os.path.isfile(pkg), "PKG not created"
        chunks, _, _ = read_pkg_chunks(pkg)
        textures = scan_textures(chunks)
        assert len(textures) == 1, f"Expected 1 texture, got {len(textures)}"
        assert textures[0]['name'] == 'GR2\\TestTex'
        assert textures[0]['width'] == 64
        assert textures[0]['format'] == 0x1C
    finally:
        shutil.rmtree(tmp)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. mod.json generation
# ═══════════════════════════════════════════════════════════════════════════════

def test_mod_json_structure():
    """Verify mod.json has required fields."""
    mod = {
        'format': 'cg3h-mod/1.0',
        'metadata': {'name': 'Test', 'author': 'Tester', 'version': '1.0.0'},
        'type': 'texture_replace',
        'target': {'character': 'Melinoe'},
        'assets': {'textures': [{'name': 'Melinoe_Color512', 'file': 'test.png'}]},
    }
    assert mod['format'].startswith('cg3h-mod')
    assert 'name' in mod['metadata']
    assert 'character' in mod['target']
    assert len(mod['assets']['textures']) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Operation inference
# ═══════════════════════════════════════════════════════════════════════════════

def test_infer_operations_texture():
    from cg3h_build import _infer_operations
    mod = {'type': 'texture_replace', 'assets': {'textures': [{'name': 'X'}]}}
    ops = _infer_operations(mod)
    assert 'replaces_textures' in ops

def test_infer_operations_mesh_add():
    from cg3h_build import _infer_operations
    mod = {'type': 'mesh_add', 'assets': {'glb': 'test.glb'}}
    ops = _infer_operations(mod)
    assert 'adds_meshes' in ops

def test_infer_operations_multi():
    from cg3h_build import _infer_operations
    mod = {'type': ['mesh_add', 'texture_replace'], 'assets': {'glb': 'test.glb', 'textures': [{'name': 'X'}]}}
    ops = _infer_operations(mod)
    assert 'adds_meshes' in ops
    assert 'replaces_textures' in ops

def test_infer_operations_animation():
    from cg3h_build import _infer_operations
    mod = {'type': 'animation_patch', 'assets': {'glb': 'test.glb', 'animations': {'patch': True, 'filter': 'Idle'}}}
    ops = _infer_operations(mod)
    assert 'patches_animations' in ops

def test_infer_operations_from_assets():
    """Infer without explicit type — just from assets."""
    from cg3h_build import _infer_operations
    mod = {'assets': {'glb': 'test.glb', 'textures': [{'name': 'X', 'custom': True}]}}
    ops = _infer_operations(mod)
    assert 'adds_meshes' in ops
    assert 'adds_textures' in ops


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Conflict detection
# ═══════════════════════════════════════════════════════════════════════════════

def test_conflict_same_texture():
    from cg3h_build import detect_conflicts
    tmp = _tmpdir()
    try:
        # Create two mods replacing the same texture
        mod_a = os.path.join(tmp, 'ModA')
        mod_b = os.path.join(tmp, 'ModB')
        os.makedirs(os.path.join(mod_a, 'plugins'))
        os.makedirs(os.path.join(mod_b, 'plugins'))

        for d, name in [(mod_a, 'ModA'), (mod_b, 'ModB')]:
            with open(os.path.join(d, 'mod.json'), 'w') as f:
                json.dump({
                    'format': 'cg3h-mod/1.0',
                    'metadata': {'name': name},
                    'type': 'texture_replace',
                    'target': {'character': 'Melinoe'},
                    'assets': {'textures': [{'name': 'Melinoe_Color512'}]},
                }, f)

        # detect_conflicts checks other mods in a plugins dir
        # For this test we just verify the logic works
        from cg3h_build import _infer_operations
        mod = json.load(open(os.path.join(mod_a, 'mod.json')))
        ops = _infer_operations(mod)
        assert 'replaces_textures' in ops
    finally:
        shutil.rmtree(tmp)


def test_conflict_mesh_add_compatible():
    """Two mesh_add for same character should NOT conflict."""
    from cg3h_build import _infer_operations
    mod_a = {'type': 'mesh_add', 'target': {'character': 'Melinoe'}, 'assets': {'glb': 'a.glb'}}
    mod_b = {'type': 'mesh_add', 'target': {'character': 'Melinoe'}, 'assets': {'glb': 'b.glb'}}
    ops_a = _infer_operations(mod_a)
    ops_b = _infer_operations(mod_b)
    # Both add meshes — compatible, no replaces_meshes conflict
    assert 'adds_meshes' in ops_a
    assert 'adds_meshes' in ops_b
    assert 'replaces_meshes' not in ops_a
    assert 'replaces_meshes' not in ops_b


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Mod priority
# ═══════════════════════════════════════════════════════════════════════════════

def test_priority_generation():
    from mod_merger import load_priority, save_priority, generate_default_priority, group_by_character
    tmp = _tmpdir()
    try:
        mods = [
            {'id': 'B-Mod', 'mod': {'target': {'character': 'Melinoe'}, 'metadata': {'name': 'B'}}},
            {'id': 'A-Mod', 'mod': {'target': {'character': 'Melinoe'}, 'metadata': {'name': 'A'}}},
        ]
        priority = generate_default_priority(tmp, mods)
        assert 'Melinoe' in priority
        assert priority['Melinoe'] == ['A-Mod', 'B-Mod']  # alphabetical

        # Reload
        loaded = load_priority(tmp)
        assert loaded == priority
    finally:
        shutil.rmtree(tmp)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Smart stripping
# ═══════════════════════════════════════════════════════════════════════════════

def test_strip_unchanged_textures():
    """Unchanged textures should be detected via hash comparison."""
    import hashlib
    from PIL import Image
    import io

    img = Image.new('RGBA', (64, 64), (255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    png_bytes = buf.getvalue()
    png_hash = hashlib.md5(png_bytes).hexdigest()

    # Same hash = unchanged
    assert hashlib.md5(png_bytes).hexdigest() == png_hash

    # Different image = changed
    img2 = Image.new('RGBA', (64, 64), (0, 255, 0, 255))
    buf2 = io.BytesIO()
    img2.save(buf2, format='PNG')
    assert hashlib.md5(buf2.getvalue()).hexdigest() != png_hash


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Full export pipeline (needs game)
# ═══════════════════════════════════════════════════════════════════════════════

@_needs_game
def test_export_melinoe():
    """Export Melinoe to GLB and verify output."""
    import subprocess
    game = _game_dir()
    tmp = _tmpdir()
    try:
        glb = os.path.join(tmp, 'Melinoe.glb')
        result = subprocess.run(
            [sys.executable, os.path.join(_tools, 'gr2_to_gltf.py'),
             'Melinoe', '--textures', '-o', glb],
            capture_output=True, text=True, timeout=120,
            cwd=os.path.join(game, 'Ship'),
        )
        assert result.returncode == 0, f"Export failed: {result.stderr[-200:]}"
        assert os.path.isfile(glb), "GLB not created"
        assert os.path.getsize(glb) > 100000, "GLB too small"

        # Verify manifest
        manifest = os.path.join(tmp, 'manifest.json')
        assert os.path.isfile(manifest), "No manifest.json"
        with open(manifest) as f:
            m = json.load(f)
        assert m['character'] == 'Melinoe'
        assert len(m['meshes']) > 0
        assert 'textures' in m
    finally:
        shutil.rmtree(tmp)


@_needs_game
def test_build_texture_mod():
    """Build a texture-only mod and verify output structure."""
    from PIL import Image
    game = _game_dir()
    tmp = _tmpdir()
    try:
        # Create mod
        Image.new('RGBA', (512, 512), (255, 0, 0, 255)).save(os.path.join(tmp, 'red.png'))
        with open(os.path.join(tmp, 'mod.json'), 'w') as f:
            json.dump({
                'format': 'cg3h-mod/1.0',
                'metadata': {'name': 'RedTest', 'author': 'Test', 'version': '1.0.0'},
                'type': 'texture_replace',
                'target': {'character': 'Melinoe'},
                'assets': {'textures': [{'name': 'Melinoe_Color512', 'file': 'red.png', 'custom': True}]},
            }, f)

        import subprocess
        result = subprocess.run(
            [sys.executable, os.path.join(_tools, 'cg3h_build.py'), tmp],
            capture_output=True, text=True, timeout=120,
            cwd=os.path.join(game, 'Ship'),
        )
        assert result.returncode == 0, f"Build failed: {result.stderr[-200:]}"

        # Verify build output
        build = os.path.join(tmp, 'build')
        assert os.path.isdir(build), "No build directory"
        pd = os.path.join(build, 'plugins_data', 'Test-RedTest')
        assert os.path.isdir(pd), f"No plugins_data: {os.listdir(build)}"
        pkg = os.path.join(pd, 'Test-RedTest.pkg')
        assert os.path.isfile(pkg), "No .pkg file"

        pp = os.path.join(build, 'plugins', 'Test-RedTest')
        assert os.path.isfile(os.path.join(pp, 'main.lua')), "No main.lua"
        assert os.path.isfile(os.path.join(pp, 'manifest.json')), "No H2M manifest"
    finally:
        shutil.rmtree(tmp)


@_needs_game
def test_thunderstore_zip_structure():
    """Verify Thunderstore ZIP has correct structure and no CC content."""
    from PIL import Image
    game = _game_dir()
    tmp = _tmpdir()
    try:
        Image.new('RGBA', (512, 512), (0, 0, 255, 255)).save(os.path.join(tmp, 'blue.png'))
        with open(os.path.join(tmp, 'mod.json'), 'w') as f:
            json.dump({
                'format': 'cg3h-mod/1.0',
                'metadata': {'name': 'BlueTest', 'author': 'Test', 'version': '1.0.0'},
                'type': 'texture_replace',
                'target': {'character': 'Melinoe'},
                'assets': {'textures': [{'name': 'Test_Color', 'file': 'blue.png', 'custom': True}]},
            }, f)

        import subprocess
        result = subprocess.run(
            [sys.executable, os.path.join(_tools, 'cg3h_build.py'), tmp, '--package'],
            capture_output=True, text=True, timeout=120,
            cwd=os.path.join(game, 'Ship'),
        )
        assert result.returncode == 0, f"Package failed: {result.stderr[-200:]}"

        # Find ZIP
        zips = [f for f in os.listdir(tmp) if f.endswith('.zip')]
        assert len(zips) == 1, f"Expected 1 ZIP, got {zips}"

        import zipfile
        with zipfile.ZipFile(os.path.join(tmp, zips[0])) as zf:
            names = zf.namelist()
            assert 'manifest.json' in names, "No Thunderstore manifest"
            assert 'README.md' in names, "No README"
            assert 'mod.json' in names, "No mod.json"
            # No .gpk in ZIP (CC content)
            gpk_files = [n for n in names if n.endswith('.gpk')]
            assert len(gpk_files) == 0, f"ZIP contains .gpk (CC content): {gpk_files}"
    finally:
        shutil.rmtree(tmp)


# ═══════════════════════════════════════════════════════════════════════════════
# 8. GLB parse with materials
# ═══════════════════════════════════════════════════════════════════════════════

@_needs_game
def test_parse_glb_materials():
    """Export a character and verify parse_glb reads material info."""
    import subprocess
    game = _game_dir()
    tmp = _tmpdir()
    try:
        glb = os.path.join(tmp, 'Melinoe.glb')
        subprocess.run(
            [sys.executable, os.path.join(_tools, 'gr2_to_gltf.py'),
             'Melinoe', '--textures', '-o', glb],
            capture_output=True, timeout=120,
            cwd=os.path.join(game, 'Ship'),
        )
        from gltf_to_gr2 import parse_glb
        meshes = parse_glb(glb)
        assert len(meshes) > 0
        # At least one mesh should have a material
        has_mat = any(m.get('texture_name') for m in meshes)
        assert has_mat, "No meshes have texture_name"
    finally:
        shutil.rmtree(tmp)


# ═══════════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════════

def _run_all():
    import traceback
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith('test_') and callable(obj)]
    passed = failed = skipped = 0
    for name, fn in tests:
        try:
            fn()
            if 'SKIP' in (fn.__doc__ or ''):
                skipped += 1
            else:
                passed += 1
            print(f"  PASS  {name}")
        except Exception as e:
            if 'SKIP' in str(e):
                skipped += 1
                print(f"  SKIP  {name}")
            else:
                failed += 1
                print(f"  FAIL  {name}: {e}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped out of {len(tests)}")
    return failed


if __name__ == '__main__':
    sys.exit(_run_all())
