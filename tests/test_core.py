"""
Test suite for CG3H core functionality.
Runs without the game DLL or .pkg files — uses synthetic data.

Usage:
    python -m pytest tests/test_core.py -v
    python tests/test_core.py              # standalone
"""
import sys
import os
import struct
import json
import tempfile
import shutil
import numpy as np

# Add tools/ and blender_addon/cg3h/ to path so tests can import pure helpers
_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tools = os.path.join(_repo, 'tools')
_addon = os.path.join(_repo, 'blender_addon', 'cg3h')
sys.path.insert(0, _tools)
sys.path.insert(0, _addon)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Animation sanitization (build_gltf channel filtering)
# ═══════════════════════════════════════════════════════════════════════════════

def _make_bones(n=3):
    """Create minimal bone list for build_gltf."""
    bones = []
    for i in range(n):
        bones.append({
            'name': f'bone_{i}',
            'parent': i - 1 if i > 0 else -1,
            'translation': (0.0, 0.0, 0.0),
            'rotation': (0.0, 0.0, 0.0, 1.0),
            'inv_world': tuple([1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]),
        })
    return bones


def _make_mesh():
    """Create a minimal mesh for build_gltf."""
    positions = np.array([[0,0,0],[1,0,0],[0,1,0]], dtype=np.float32)
    normals = np.array([[0,0,1],[0,0,1],[0,0,1]], dtype=np.float32)
    uvs = np.array([[0,0],[1,0],[0,1]], dtype=np.float32)
    indices = np.array([0,1,2], dtype=np.uint16)
    return [(positions, normals, uvs, None, None, indices)]


def _make_anim(tracks):
    """Create a minimal animation dict."""
    return [{
        'name': 'TestAnim',
        'granny_name': 'TestAnim',
        'duration': 1.0,
        'tracks': tracks,
    }]


def _count_channels(gltf, path=None):
    """Count animation channels in the gltf, optionally filtered by path."""
    total = 0
    for anim in gltf.animations:
        for ch in anim.channels:
            if path is None or ch.target.path == path:
                total += 1
    return total


def test_scale_skip_shear_rotation():
    """Scale channels with significant off-diagonal (shear/rotation) should be skipped."""
    from gr2_to_gltf import build_gltf
    bones = _make_bones(1)
    # 3x3 matrix with large off-diagonal = rotation, not pure scale
    scale_matrix = np.array([[1, 0.5, 0, 0, 1, 0.5, 0, 0, 1]], dtype=np.float32)
    tracks = [{'name': 'bone_0', 'bone_index': 0,
               'orient': None, 'pos': None,
               'scale': (np.array([0.0], dtype=np.float32), scale_matrix, 0)}]
    gltf = build_gltf('test', _make_mesh(), ['mesh0'], bones, animations=_make_anim(tracks))
    assert _count_channels(gltf, 'scale') == 0, "Shear/rotation scale should be skipped"


def test_scale_skip_nan():
    """Scale channels with NaN values should be skipped."""
    from gr2_to_gltf import build_gltf
    bones = _make_bones(1)
    vals = np.array([[float('nan'), 1.0, 1.0]], dtype=np.float32)
    tracks = [{'name': 'bone_0', 'bone_index': 0,
               'orient': None, 'pos': None,
               'scale': (np.array([0.0], dtype=np.float32), vals, 0)}]
    gltf = build_gltf('test', _make_mesh(), ['mesh0'], bones, animations=_make_anim(tracks))
    assert _count_channels(gltf, 'scale') == 0, "NaN scale should be skipped"


def test_scale_skip_extreme():
    """Scale channels with extreme values (>100) should be skipped."""
    from gr2_to_gltf import build_gltf
    bones = _make_bones(1)
    vals = np.array([[200.0, 1.0, 1.0]], dtype=np.float32)
    tracks = [{'name': 'bone_0', 'bone_index': 0,
               'orient': None, 'pos': None,
               'scale': (np.array([0.0], dtype=np.float32), vals, 0)}]
    gltf = build_gltf('test', _make_mesh(), ['mesh0'], bones, animations=_make_anim(tracks))
    assert _count_channels(gltf, 'scale') == 0, "Extreme scale should be skipped"


def test_scale_pure_scale_passes():
    """Pure scale (identity off-diagonal) should NOT be skipped."""
    from gr2_to_gltf import build_gltf
    bones = _make_bones(1)
    # 3x3 identity-like: diagonal = 1.5, off-diagonal = 0
    vals = np.array([[1.5, 0, 0, 0, 1.5, 0, 0, 0, 1.5]], dtype=np.float32)
    tracks = [{'name': 'bone_0', 'bone_index': 0,
               'orient': None, 'pos': None,
               'scale': (np.array([0.0], dtype=np.float32), vals, 0)}]
    gltf = build_gltf('test', _make_mesh(), ['mesh0'], bones, animations=_make_anim(tracks))
    assert _count_channels(gltf, 'scale') == 1, "Pure scale should pass"


def test_rotation_normalized():
    """Rotation quaternions should be normalized to unit length."""
    from gr2_to_gltf import build_gltf
    bones = _make_bones(1)
    # Non-unit quaternion (length ~2)
    vals = np.array([[0, 0, 0, 2.0]], dtype=np.float32)
    tracks = [{'name': 'bone_0', 'bone_index': 0,
               'orient': (np.array([0.0], dtype=np.float32), vals.copy(), 0),
               'pos': None, 'scale': None}]
    gltf = build_gltf('test', _make_mesh(), ['mesh0'], bones, animations=_make_anim(tracks))
    assert _count_channels(gltf, 'rotation') == 1
    # Find the rotation sampler's output accessor
    rot_ch = [ch for ch in gltf.animations[0].channels if ch.target.path == 'rotation'][0]
    sampler = gltf.animations[0].samplers[rot_ch.sampler]
    acc = gltf.accessors[sampler.output]
    bv = gltf.bufferViews[acc.bufferView]
    blob = gltf.binary_blob()
    quat = np.frombuffer(blob[bv.byteOffset:bv.byteOffset + bv.byteLength], dtype=np.float32)
    length = np.linalg.norm(quat)
    assert abs(length - 1.0) < 0.001, f"Quaternion should be unit length, got {length}"


def test_translation_skip_extreme():
    """Translation channels with extreme values (>1000) should be skipped."""
    from gr2_to_gltf import build_gltf
    bones = _make_bones(1)
    vals = np.array([[5000.0, 0.0, 0.0]], dtype=np.float32)
    tracks = [{'name': 'bone_0', 'bone_index': 0,
               'orient': None,
               'pos': (np.array([0.0], dtype=np.float32), vals, 0),
               'scale': None}]
    gltf = build_gltf('test', _make_mesh(), ['mesh0'], bones, animations=_make_anim(tracks))
    assert _count_channels(gltf, 'translation') == 0, "Extreme translation should be skipped"


def test_translation_normal_passes():
    """Normal translation values should pass."""
    from gr2_to_gltf import build_gltf
    bones = _make_bones(1)
    vals = np.array([[1.0, 2.0, 3.0]], dtype=np.float32)
    tracks = [{'name': 'bone_0', 'bone_index': 0,
               'orient': None,
               'pos': (np.array([0.0], dtype=np.float32), vals, 0),
               'scale': None}]
    gltf = build_gltf('test', _make_mesh(), ['mesh0'], bones, animations=_make_anim(tracks))
    assert _count_channels(gltf, 'translation') == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Animation bone name matching (_static suffix, short name)
# ═══════════════════════════════════════════════════════════════════════════════

def test_bone_match_exact():
    """Exact bone name match should work."""
    from gr2_to_gltf import build_gltf
    bones = _make_bones(1)
    tracks = [{'name': 'bone_0', 'bone_index': 0,
               'orient': (np.array([0.0], dtype=np.float32),
                          np.array([[0,0,0,1]], dtype=np.float32), 0),
               'pos': None, 'scale': None}]
    gltf = build_gltf('test', _make_mesh(), ['mesh0'], bones, animations=_make_anim(tracks))
    assert _count_channels(gltf, 'rotation') == 1


def test_bone_match_short_name():
    """Track with rig prefix should match bone via short name (after colon)."""
    from gr2_to_gltf import build_gltf
    bones = _make_bones(1)
    tracks = [{'name': 'SomeRig:bone_0', 'bone_index': 0,
               'orient': (np.array([0.0], dtype=np.float32),
                          np.array([[0,0,0,1]], dtype=np.float32), 0),
               'pos': None, 'scale': None}]
    gltf = build_gltf('test', _make_mesh(), ['mesh0'], bones, animations=_make_anim(tracks))
    assert _count_channels(gltf, 'rotation') == 1


def test_bone_match_static_suffix():
    """Track with _static suffix should match bone without it."""
    from gr2_to_gltf import build_gltf
    bones = _make_bones(1)
    tracks = [{'name': 'bone_0_static', 'bone_index': 0,
               'orient': (np.array([0.0], dtype=np.float32),
                          np.array([[0,0,0,1]], dtype=np.float32), 0),
               'pos': None, 'scale': None}]
    gltf = build_gltf('test', _make_mesh(), ['mesh0'], bones, animations=_make_anim(tracks))
    assert _count_channels(gltf, 'rotation') == 1


def test_bone_match_rig_prefix_and_static():
    """Track with both rig prefix and _static suffix should match."""
    from gr2_to_gltf import build_gltf
    bones = _make_bones(1)
    tracks = [{'name': 'MyRig:bone_0_static', 'bone_index': 0,
               'orient': (np.array([0.0], dtype=np.float32),
                          np.array([[0,0,0,1]], dtype=np.float32), 0),
               'pos': None, 'scale': None}]
    gltf = build_gltf('test', _make_mesh(), ['mesh0'], bones, animations=_make_anim(tracks))
    assert _count_channels(gltf, 'rotation') == 1


def test_bone_no_match():
    """Track with completely wrong name should be skipped."""
    from gr2_to_gltf import build_gltf
    bones = _make_bones(1)
    tracks = [{'name': 'nonexistent_bone', 'bone_index': 0,
               'orient': (np.array([0.0], dtype=np.float32),
                          np.array([[0,0,0,1]], dtype=np.float32), 0),
               'pos': None, 'scale': None}]
    gltf = build_gltf('test', _make_mesh(), ['mesh0'], bones, animations=_make_anim(tracks))
    assert _count_channels(gltf) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Mesh naming (duplicate names get _2, _3 — not _LOD)
# ═══════════════════════════════════════════════════════════════════════════════

def test_duplicate_mesh_names():
    """Duplicate mesh names should get _1, _2 suffixes, not _LOD."""
    from gr2_to_gltf import build_gltf
    bones = _make_bones(1)
    mesh_data = _make_mesh() * 3  # three identical meshes
    mesh_names = ['MyMesh', 'MyMesh_1', 'MyMesh_2']
    gltf = build_gltf('test', mesh_data, mesh_names, bones)
    names = [m.name for m in gltf.meshes]
    assert 'MyMesh' in names
    assert 'MyMesh_1' in names
    assert 'MyMesh_2' in names
    assert not any('LOD' in n for n in names), f"No LOD in names: {names}"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Importer mesh name normalization
# ═══════════════════════════════════════════════════════════════════════════════

def test_normalize_strips_numeric():
    """_1, _2 part-split suffixes should be stripped."""
    from gltf_to_gr2 import _normalize_mesh_name
    assert _normalize_mesh_name('Melinoe_MeshShape_2') == 'melinoe_meshshape'


def test_normalize_strips_blender_suffix():
    """Blender .001 suffixes should be stripped."""
    from gltf_to_gr2 import _normalize_mesh_name
    assert _normalize_mesh_name('Melinoe_MeshShape.001') == 'melinoe_meshshape'


def test_normalize_strips_rig_prefix():
    """Rig: prefix should be stripped."""
    from gltf_to_gr2 import _normalize_mesh_name
    assert _normalize_mesh_name('Melinoe_Rig:MelinoeOverlook_MeshShape_2.001') == \
           'melinoeoverlook_meshshape'


# ═══════════════════════════════════════════════════════════════════════════════
# 5. PKG 0xAA big-endian size parsing
# ═══════════════════════════════════════════════════════════════════════════════

def _build_fake_pkg_chunk(*entries):
    """Build a fake decompressed chunk with Tex2D/Tex3D entries.
    Each entry: (tag, name_bytes, width, height, fmt, pixel_data)
    """
    chunk = bytearray()
    for tag, name, w, h, fmt, pixels in entries:
        chunk.append(tag)
        # CSString: 7-bit length + raw bytes
        name_bytes = name.encode('utf-8')
        chunk.append(len(name_bytes))
        chunk.extend(name_bytes)
        # Big-endian total size (XNB header 10 + tex header 20 + pixels)
        from pkg_texture import _swap32
        total = 10 + 20 + len(pixels)
        chunk.extend(struct.pack('<I', _swap32(total)))
        # XNB header
        chunk.extend(b'XNBw\x06\x00')
        chunk.extend(struct.pack('<I', total))
        # Texture header: fmt, w, h, depth=1, pixel_size
        chunk.extend(struct.pack('<IIIII', fmt, w, h, 1, len(pixels)))
        chunk.extend(pixels)
    chunk.append(0xFF)  # end of chunk
    return bytes(chunk)


def test_scan_textures_0xAD():
    """Tex2D (0xAD) entries should be parsed correctly."""
    from pkg_texture import scan_textures
    pixels = b'\x00' * 256
    chunk = _build_fake_pkg_chunk((0xAD, 'GR2\\Test_Color', 16, 16, 0x1C, pixels))
    results = scan_textures([(chunk, None)])
    assert len(results) == 1
    assert results[0]['name'] == 'GR2\\Test_Color'
    assert results[0]['width'] == 16
    assert results[0]['format'] == 0x1C


def test_scan_textures_0xAA():
    """Tex3D (0xAA) entries should be parsed with big-endian size (same as 0xAD)."""
    from pkg_texture import scan_textures
    pixels = b'\x00' * 256
    chunk = _build_fake_pkg_chunk((0xAA, 'GR2\\Test3D_Color', 16, 16, 0x1C, pixels))
    results = scan_textures([(chunk, None)])
    assert len(results) == 1
    assert results[0]['name'] == 'GR2\\Test3D_Color'
    assert results[0]['width'] == 16


def test_scan_textures_mixed():
    """0xAD followed by 0xAA should both be parsed (0xAA size doesn't skip rest)."""
    from pkg_texture import scan_textures
    p1 = b'\x00' * 256
    p2 = b'\xff' * 128
    chunk = _build_fake_pkg_chunk(
        (0xAA, 'First3D', 16, 16, 0x1C, p1),
        (0xAD, 'Second2D', 8, 8, 0x1C, p2),
    )
    results = scan_textures([(chunk, None)])
    assert len(results) == 2, f"Expected 2 textures, got {len(results)}"
    names = {r['name'] for r in results}
    assert 'First3D' in names
    assert 'Second2D' in names


# ═══════════════════════════════════════════════════════════════════════════════
# 6. PNG -> DDS compression
# ═══════════════════════════════════════════════════════════════════════════════

def test_png_to_dds_bc7():
    """PNG -> BC7 DDS with mipmaps should produce correct size."""
    from pkg_texture import png_to_dds
    from PIL import Image

    tmpdir = tempfile.mkdtemp()
    try:
        png_path = os.path.join(tmpdir, 'test.png')
        Image.new('RGBA', (64, 64), (255, 0, 0, 255)).save(png_path)
        dds = png_to_dds(png_path, 0x1C, 64, 64, 3)  # 3 mips: 64, 32, 16
    finally:
        shutil.rmtree(tmpdir)

    # Verify header
    assert dds[:4] == b'DDS '
    w = struct.unpack_from('<I', dds, 16)[0]
    h = struct.unpack_from('<I', dds, 12)[0]
    assert w == 64 and h == 64

    # Verify pixel data size (BC7: 16 bytes per 4x4 block)
    expected = (16*16 + 8*8 + 4*4) * 16  # blocks per mip * 16 bytes
    header_size = 128 + 20  # DDS + DX10
    actual = len(dds) - header_size
    assert actual == expected, f"Expected {expected}, got {actual}"


def test_png_to_dds_bc3():
    """PNG -> BC3 DDS should work."""
    from pkg_texture import png_to_dds
    from PIL import Image

    tmpdir = tempfile.mkdtemp()
    try:
        png_path = os.path.join(tmpdir, 'test.png')
        Image.new('RGBA', (16, 16), (0, 255, 0, 255)).save(png_path)
        dds = png_to_dds(png_path, 0x06, 16, 16, 1)  # 1 mip
    finally:
        shutil.rmtree(tmpdir)

    assert dds[:4] == b'DDS '
    # BC3: 16 bytes per 4x4 block, 16x16 = 16 blocks
    expected_pixels = 16 * 16
    header_size = 128
    actual = len(dds) - header_size
    assert actual == expected_pixels, f"Expected {expected_pixels}, got {actual}"


def test_png_to_dds_resize():
    """PNG with wrong dimensions should be resized to target."""
    from pkg_texture import png_to_dds
    from PIL import Image

    tmpdir = tempfile.mkdtemp()
    try:
        png_path = os.path.join(tmpdir, 'test.png')
        Image.new('RGBA', (100, 100), (0, 0, 255, 255)).save(png_path)
        dds = png_to_dds(png_path, 0x1C, 64, 64, 1)
    finally:
        shutil.rmtree(tmpdir)

    w = struct.unpack_from('<I', dds, 16)[0]
    assert w == 64


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Texture index
# ═══════════════════════════════════════════════════════════════════════════════

def test_texture_index_roundtrip():
    """build_texture_index + save + load should roundtrip."""
    from pkg_texture import save_texture_index, load_texture_index
    tmpdir = tempfile.mkdtemp()
    try:
        # Create a fake .pkg with one texture
        pixels = b'\x00' * 256
        chunk = _build_fake_pkg_chunk((0xAD, 'GR2\\MyChar_Color', 16, 16, 0x1C, pixels))
        # Write a minimal .pkg file (header + 1 compressed chunk)
        import lz4.block
        from pkg_texture import _swap32
        comp = lz4.block.compress(chunk, store_size=False)
        # Header: version 7 + compressed flag (0x20000000 | 7 = 0x20000007)
        pkg_data = struct.pack('<I', _swap32(0x20000007))
        pkg_data += b'\x01'  # flag: compressed
        pkg_data += struct.pack('<I', _swap32(len(comp)))
        pkg_data += comp

        pkg_path = os.path.join(tmpdir, 'Test.pkg')
        with open(pkg_path, 'wb') as f:
            f.write(pkg_data)

        # Build and save index
        save_texture_index(tmpdir, os.path.join(tmpdir, '_texture_index.json'))

        # Load it back
        index = load_texture_index(tmpdir)
        assert index is not None
        assert 'mychar_color' in index
        assert index['mychar_color']['pkg'] == 'Test.pkg'
        assert index['mychar_color']['width'] == 16
    finally:
        shutil.rmtree(tmpdir)


# ═══════════════════════════════════════════════════════════════════════════════
# 8. _swap32
# ═══════════════════════════════════════════════════════════════════════════════

def test_swap32():
    """_swap32 should byte-reverse a 32-bit integer."""
    from pkg_texture import _swap32
    assert _swap32(0x01020304) == 0x04030201
    assert _swap32(0) == 0
    assert _swap32(_swap32(0xDEADBEEF)) == 0xDEADBEEF  # roundtrip


# ═══════════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════════

def _run_all():
    """Run all test functions across test_core, test_coverage, and test_integration."""
    import importlib
    import traceback

    # Always discover this module's own tests first
    suites = [(__name__, globals())]

    # Pull in sibling test modules so a single invocation runs the full suite
    for sibling in ('test_coverage',):
        try:
            mod = importlib.import_module(sibling)
            suites.append((sibling, vars(mod)))
        except Exception as e:
            print(f"  (skip {sibling}: {e})")

    passed = failed = 0
    for module_name, ns in suites:
        tests = [(name, obj) for name, obj in ns.items()
                 if name.startswith('test_') and callable(obj)]
        for name, fn in sorted(tests):
            try:
                fn()
                passed += 1
                print(f"  PASS  {name}")
            except Exception as e:
                failed += 1
                print(f"  FAIL  {name}: {e}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed out of {passed + failed}")
    return failed


# ── v3.1 Multi-entry tests ──────────────────────────────────────────────────

def test_skeleton_merge_unique_bones():
    """Skeleton merge should add bones not in the base skeleton."""
    # Simulate the merge logic from gr2_to_gltf.py
    base_bones = [
        {'name': 'root', 'parent': -1, 'translation': (0,0,0), 'rotation': (0,0,0,1), 'inv_world': tuple(range(16))},
        {'name': 'spine', 'parent': 0, 'translation': (0,1,0), 'rotation': (0,0,0,1), 'inv_world': tuple(range(16))},
        {'name': 'head', 'parent': 1, 'translation': (0,2,0), 'rotation': (0,0,0,1), 'inv_world': tuple(range(16))},
    ]
    base_name_to_idx = {b['name']: i for i, b in enumerate(base_bones)}

    entry2_bones = [
        {'name': 'root', 'parent': -1, 'translation': (0,0,0), 'rotation': (0,0,0,1), 'inv_world': tuple(range(16))},
        {'name': 'hat_bone', 'parent': 0, 'translation': (0,3,0), 'rotation': (0,0,0,1), 'inv_world': tuple(range(16))},
    ]

    # Merge (same logic as gr2_to_gltf.py)
    bones = list(base_bones)
    bone_name_to_idx = dict(base_name_to_idx)
    for bone in entry2_bones:
        if bone['name'] not in bone_name_to_idx:
            new_parent = -1
            if bone['parent'] >= 0 and bone['parent'] < len(entry2_bones):
                parent_name = entry2_bones[bone['parent']]['name']
                new_parent = bone_name_to_idx.get(parent_name, -1)
            new_bone = dict(bone, parent=new_parent)
            bone_name_to_idx[bone['name']] = len(bones)
            bones.append(new_bone)

    assert len(bones) == 4  # root, spine, head, hat_bone
    assert bone_name_to_idx['hat_bone'] == 3
    assert bones[3]['name'] == 'hat_bone'
    assert bones[3]['parent'] == 0  # root remapped to index 0


def test_skeleton_merge_no_duplicates():
    """Skeleton merge should not duplicate existing bones."""
    base_bones = [
        {'name': 'root', 'parent': -1, 'translation': (0,0,0), 'rotation': (0,0,0,1), 'inv_world': tuple(range(16))},
        {'name': 'spine', 'parent': 0, 'translation': (0,1,0), 'rotation': (0,0,0,1), 'inv_world': tuple(range(16))},
    ]
    base_name_to_idx = {b['name']: i for i, b in enumerate(base_bones)}

    # Same bones in second entry
    entry2_bones = [
        {'name': 'root', 'parent': -1, 'translation': (0,0,0), 'rotation': (0,0,0,1), 'inv_world': tuple(range(16))},
        {'name': 'spine', 'parent': 0, 'translation': (0,1,0), 'rotation': (0,0,0,1), 'inv_world': tuple(range(16))},
    ]

    bones = list(base_bones)
    bone_name_to_idx = dict(base_name_to_idx)
    for bone in entry2_bones:
        if bone['name'] not in bone_name_to_idx:
            bone_name_to_idx[bone['name']] = len(bones)
            bones.append(bone)

    assert len(bones) == 2  # No duplicates added


def test_skeleton_merge_parent_remap():
    """Skeleton merge should remap parent indices to the merged list."""
    base_bones = [
        {'name': 'root', 'parent': -1, 'translation': (0,0,0), 'rotation': (0,0,0,1), 'inv_world': tuple(range(16))},
    ]
    base_name_to_idx = {'root': 0}

    # Entry2 has a chain: root -> arm -> hand (but root already in base)
    entry2_bones = [
        {'name': 'root', 'parent': -1, 'translation': (0,0,0), 'rotation': (0,0,0,1), 'inv_world': tuple(range(16))},
        {'name': 'arm', 'parent': 0, 'translation': (1,0,0), 'rotation': (0,0,0,1), 'inv_world': tuple(range(16))},
        {'name': 'hand', 'parent': 1, 'translation': (2,0,0), 'rotation': (0,0,0,1), 'inv_world': tuple(range(16))},
    ]

    bones = list(base_bones)
    bone_name_to_idx = dict(base_name_to_idx)
    for bone in entry2_bones:
        if bone['name'] not in bone_name_to_idx:
            new_parent = -1
            if bone['parent'] >= 0 and bone['parent'] < len(entry2_bones):
                parent_name = entry2_bones[bone['parent']]['name']
                new_parent = bone_name_to_idx.get(parent_name, -1)
            new_bone = dict(bone, parent=new_parent)
            bone_name_to_idx[bone['name']] = len(bones)
            bones.append(new_bone)

    assert len(bones) == 3  # root, arm, hand
    assert bones[1]['name'] == 'arm'
    assert bones[1]['parent'] == 0  # root is at index 0 in merged
    assert bones[2]['name'] == 'hand'
    assert bones[2]['parent'] == 1  # arm is at index 1 in merged


def test_routing_with_manifest():
    """Manifest routes meshes to their source entries."""
    from gltf_to_gr2 import _build_entry_routing

    manifest = {
        'meshes': [
            {'name': 'BattleMesh', 'entry': 'Battle_Mesh'},
            {'name': 'HubMesh', 'entry': 'Hub_Mesh'},
        ]
    }
    glb_meshes = [
        {'name': 'BattleMesh'},
        {'name': 'HubMesh'},
    ]
    entries = ['Battle_Mesh', 'Hub_Mesh']
    routing = _build_entry_routing(manifest, glb_meshes, entries)

    assert len(routing['Battle_Mesh']) == 1
    assert routing['Battle_Mesh'][0]['name'] == 'BattleMesh'
    assert len(routing['Hub_Mesh']) == 1
    assert routing['Hub_Mesh'][0]['name'] == 'HubMesh'


def test_routing_without_manifest():
    """Without manifest, all meshes go to first entry."""
    from gltf_to_gr2 import _build_entry_routing

    glb_meshes = [{'name': 'A'}, {'name': 'B'}]
    entries = ['First_Mesh', 'Second_Mesh']
    routing = _build_entry_routing(None, glb_meshes, entries)

    assert len(routing['First_Mesh']) == 2
    assert len(routing['Second_Mesh']) == 0


def test_routing_new_mesh_all_entries():
    """New meshes (not in manifest) go to all entries by default."""
    from gltf_to_gr2 import _build_entry_routing

    manifest = {
        'meshes': [
            {'name': 'Existing', 'entry': 'Battle_Mesh'},
        ]
    }
    glb_meshes = [
        {'name': 'Existing'},
        {'name': 'NewGlasses'},
    ]
    entries = ['Battle_Mesh', 'Hub_Mesh']
    routing = _build_entry_routing(manifest, glb_meshes, entries)

    assert len(routing['Battle_Mesh']) == 2  # Existing + NewGlasses
    assert len(routing['Hub_Mesh']) == 1     # NewGlasses only
    assert routing['Hub_Mesh'][0]['name'] == 'NewGlasses'


def test_routing_new_mesh_targeted():
    """New meshes with routing go to specified entries only."""
    from gltf_to_gr2 import _build_entry_routing

    manifest = {'meshes': []}
    glb_meshes = [{'name': 'Glasses'}]
    entries = ['Battle_Mesh', 'Hub_Mesh']
    routing = _build_entry_routing(manifest, glb_meshes, entries,
                                   new_mesh_routing={'Glasses': ['Battle_Mesh']})

    assert len(routing['Battle_Mesh']) == 1
    assert len(routing['Hub_Mesh']) == 0


def test_merge_manifests_union():
    """Merged manifest contains union of entries and meshes."""
    from cg3h_builder_entry import _merge_manifests

    tmpdir = tempfile.mkdtemp()
    try:
        m1_path = os.path.join(tmpdir, 'mod1', 'manifest.json')
        m2_path = os.path.join(tmpdir, 'mod2', 'manifest.json')
        os.makedirs(os.path.dirname(m1_path))
        os.makedirs(os.path.dirname(m2_path))

        with open(m1_path, 'w') as f:
            json.dump({'mesh_entries': ['Battle_Mesh'], 'meshes': [
                {'name': 'BattleMesh', 'entry': 'Battle_Mesh', 'gr2_index': 0}
            ]}, f)
        with open(m2_path, 'w') as f:
            json.dump({'mesh_entries': ['Hub_Mesh'], 'meshes': [
                {'name': 'HubMesh', 'entry': 'Hub_Mesh', 'gr2_index': 0}
            ]}, f)

        char_mods = [
            {'manifest_path': m1_path},
            {'manifest_path': m2_path},
        ]
        merged = _merge_manifests(char_mods)

        assert merged is not None
        assert set(merged['mesh_entries']) == {'Battle_Mesh', 'Hub_Mesh'}
        assert len(merged['meshes']) == 2
    finally:
        shutil.rmtree(tmpdir)


def test_merge_manifests_no_duplicates():
    """Same mesh from two manifests appears only once."""
    from cg3h_builder_entry import _merge_manifests

    tmpdir = tempfile.mkdtemp()
    try:
        m1_path = os.path.join(tmpdir, 'mod1', 'manifest.json')
        m2_path = os.path.join(tmpdir, 'mod2', 'manifest.json')
        os.makedirs(os.path.dirname(m1_path))
        os.makedirs(os.path.dirname(m2_path))

        mesh = {'name': 'SharedMesh', 'entry': 'Body_Mesh', 'gr2_index': 0}
        with open(m1_path, 'w') as f:
            json.dump({'mesh_entries': ['Body_Mesh'], 'meshes': [mesh]}, f)
        with open(m2_path, 'w') as f:
            json.dump({'mesh_entries': ['Body_Mesh'], 'meshes': [mesh]}, f)

        merged = _merge_manifests([
            {'manifest_path': m1_path},
            {'manifest_path': m2_path},
        ])

        assert len(merged['meshes']) == 1
        assert len(merged['mesh_entries']) == 1
    finally:
        shutil.rmtree(tmpdir)


# ═══════════════════════════════════════════════════════════════════════════════
# 8. GLB merge helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_glb(mesh_names, bone_names=None, animations=None, mod_id='TestMod'):
    """Build a minimal GLB with named meshes, optional skeleton, and animations.

    mesh_names: list of str
    bone_names: list of str (skeleton nodes)
    animations: list of (anim_name, [(bone_name, path, times, values), ...])
    Returns path to temp GLB file.
    """
    import pygltflib

    blob = bytearray()
    nodes = []
    meshes = []
    accessors = []
    buffer_views = []
    scene_nodes = []

    # Create mesh nodes with minimal geometry (a single triangle)
    tri_pos = struct.pack('<9f', 0,0,0, 1,0,0, 0,1,0)
    tri_idx = struct.pack('<3H', 0, 1, 2)

    for i, mname in enumerate(mesh_names):
        # Position accessor
        pos_bv_offset = len(blob)
        blob.extend(tri_pos)
        bv_pos = pygltflib.BufferView(buffer=0, byteOffset=pos_bv_offset, byteLength=len(tri_pos))
        bv_pos_idx = len(buffer_views)
        buffer_views.append(bv_pos)

        acc_pos = pygltflib.Accessor(
            bufferView=bv_pos_idx, byteOffset=0,
            componentType=pygltflib.FLOAT, count=3, type='VEC3',
            max=[1,1,0], min=[0,0,0])
        acc_pos_idx = len(accessors)
        accessors.append(acc_pos)

        # Index accessor
        idx_bv_offset = len(blob)
        blob.extend(tri_idx)
        bv_idx = pygltflib.BufferView(buffer=0, byteOffset=idx_bv_offset, byteLength=len(tri_idx))
        bv_idx_idx = len(buffer_views)
        buffer_views.append(bv_idx)

        acc_idx = pygltflib.Accessor(
            bufferView=bv_idx_idx, byteOffset=0,
            componentType=pygltflib.UNSIGNED_SHORT, count=3, type='SCALAR')
        acc_idx_idx = len(accessors)
        accessors.append(acc_idx)

        prim = pygltflib.Primitive(
            attributes=pygltflib.Attributes(POSITION=acc_pos_idx),
            indices=acc_idx_idx)
        mesh = pygltflib.Mesh(name=mname, primitives=[prim])
        mesh_idx = len(meshes)
        meshes.append(mesh)

        node = pygltflib.Node(name=mname, mesh=mesh_idx)
        node_idx = len(nodes)
        nodes.append(node)
        scene_nodes.append(node_idx)

    # Create bone nodes (skeleton)
    bone_node_indices = {}
    if bone_names:
        for bname in bone_names:
            node = pygltflib.Node(name=bname)
            idx = len(nodes)
            nodes.append(node)
            bone_node_indices[bname] = idx

    # Create animations
    gltf_animations = []
    if animations and bone_node_indices:
        for anim_name, tracks in animations:
            channels = []
            samplers = []
            for bone_name, path, times, values in tracks:
                if bone_name not in bone_node_indices:
                    continue
                target_node = bone_node_indices[bone_name]

                # Time accessor
                time_bytes = struct.pack(f'<{len(times)}f', *times)
                t_bv_off = len(blob)
                blob.extend(time_bytes)
                bv_t = pygltflib.BufferView(buffer=0, byteOffset=t_bv_off, byteLength=len(time_bytes))
                bv_t_idx = len(buffer_views)
                buffer_views.append(bv_t)
                acc_t = pygltflib.Accessor(
                    bufferView=bv_t_idx, byteOffset=0,
                    componentType=pygltflib.FLOAT, count=len(times), type='SCALAR',
                    max=[max(times)], min=[min(times)])
                acc_t_idx = len(accessors)
                accessors.append(acc_t)

                # Value accessor
                val_bytes = struct.pack(f'<{len(values)}f', *values)
                v_bv_off = len(blob)
                blob.extend(val_bytes)
                bv_v = pygltflib.BufferView(buffer=0, byteOffset=v_bv_off, byteLength=len(val_bytes))
                bv_v_idx = len(buffer_views)
                buffer_views.append(bv_v)

                n_values = len(values)
                if path == 'rotation':
                    vec_type, count = 'VEC4', n_values // 4
                elif path in ('translation', 'scale'):
                    vec_type, count = 'VEC3', n_values // 3
                else:
                    vec_type, count = 'SCALAR', n_values

                acc_v = pygltflib.Accessor(
                    bufferView=bv_v_idx, byteOffset=0,
                    componentType=pygltflib.FLOAT, count=count, type=vec_type)
                acc_v_idx = len(accessors)
                accessors.append(acc_v)

                sampler_idx = len(samplers)
                samplers.append(pygltflib.AnimationSampler(
                    input=acc_t_idx, output=acc_v_idx, interpolation='LINEAR'))
                channels.append(pygltflib.AnimationChannel(
                    sampler=sampler_idx,
                    target=pygltflib.AnimationChannelTarget(
                        node=target_node, path=path)))

            if channels:
                gltf_animations.append(pygltflib.Animation(
                    name=anim_name, channels=channels, samplers=samplers))

    # Build skin if we have bones
    skins = []
    if bone_names:
        joints = [bone_node_indices[b] for b in bone_names]
        skins = [pygltflib.Skin(joints=joints)]
        # Assign skin to mesh nodes
        for ni in scene_nodes:
            nodes[ni].skin = 0

    gltf = pygltflib.GLTF2(
        scene=0,
        scenes=[pygltflib.Scene(nodes=scene_nodes)],
        nodes=nodes,
        meshes=meshes,
        accessors=accessors,
        bufferViews=buffer_views,
        buffers=[pygltflib.Buffer(byteLength=len(blob))],
        skins=skins or None,
        animations=gltf_animations or None,
    )
    gltf.set_binary_blob(bytes(blob))

    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, f'{mod_id}.glb')
    gltf.save(path)
    return path, tmpdir


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Animation filter union tests
# ═══════════════════════════════════════════════════════════════════════════════

def test_anim_filter_union():
    """Union of multiple filter patterns matches entries matching ANY pattern."""
    # Simulate the filter logic from patch_animation_entries
    entries = {
        'Idle_01': 'data1',
        'Run_01': 'data2',
        'Attack_01': 'data3',
        'Idle_02': 'data4',
    }
    filters = ['idle', 'run']
    patterns = [p.lower() for p in filters]
    result = {k: v for k, v in entries.items()
              if any(p in k.lower() for p in patterns)}
    assert len(result) == 3, f"Expected 3 matches, got {len(result)}"
    assert 'Idle_01' in result
    assert 'Run_01' in result
    assert 'Idle_02' in result
    assert 'Attack_01' not in result


def test_anim_filter_single_string_compat():
    """Single string filter works the same as before (backward compat)."""
    entries = {
        'Idle_01': 'data1',
        'Run_01': 'data2',
        'Idle_02': 'data3',
    }
    filt = 'idle'
    # Same logic as updated patch_animation_entries
    if isinstance(filt, str):
        patterns = [filt.lower()]
    else:
        patterns = [p.lower() for p in filt]
    result = {k: v for k, v in entries.items()
              if any(p in k.lower() for p in patterns)}
    assert len(result) == 2
    assert 'Run_01' not in result


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Name deduplication tests
# ═══════════════════════════════════════════════════════════════════════════════

def test_merge_glbs_no_collision():
    """Unique mesh names across mods stay unchanged."""
    from cg3h_builder_entry import _merge_glbs
    import pygltflib

    glb1, tmp1 = _make_glb(['MeshA'], bone_names=['bone0'], mod_id='ModA')
    glb2, tmp2 = _make_glb(['MeshB'], bone_names=['bone0'], mod_id='ModB')
    out = tempfile.mkdtemp()
    try:
        char_mods = [
            {'id': 'ModA', 'glb_path': glb1},
            {'id': 'ModB', 'glb_path': glb2},
        ]
        merged_path, collisions = _merge_glbs(char_mods, out, 'TestChar')
        gltf = pygltflib.GLTF2().load(merged_path)
        names = [m.name for m in gltf.meshes]
        assert 'MeshA' in names, f"MeshA missing: {names}"
        assert 'MeshB' in names, f"MeshB missing: {names}"
        assert len(collisions) == 0, f"No collisions expected: {collisions}"
    finally:
        shutil.rmtree(tmp1)
        shutil.rmtree(tmp2)
        shutil.rmtree(out)


def test_merge_glbs_name_collision():
    """Duplicate mesh names get prefixed with mod id."""
    from cg3h_builder_entry import _merge_glbs
    import pygltflib

    glb1, tmp1 = _make_glb(['Crown'], bone_names=['bone0'], mod_id='ModA')
    glb2, tmp2 = _make_glb(['Crown'], bone_names=['bone0'], mod_id='ModB')
    out = tempfile.mkdtemp()
    try:
        char_mods = [
            {'id': 'ModA', 'glb_path': glb1},
            {'id': 'ModB', 'glb_path': glb2},
        ]
        merged_path, collisions = _merge_glbs(char_mods, out, 'TestChar')
        gltf = pygltflib.GLTF2().load(merged_path)
        names = [m.name for m in gltf.meshes]
        assert 'ModA_Crown' in names, f"ModA_Crown missing: {names}"
        assert 'ModB_Crown' in names, f"ModB_Crown missing: {names}"
        assert len(names) == 2, f"Expected 2 meshes: {names}"
        assert 'Crown' in collisions, f"Crown should be in collisions: {collisions}"
    finally:
        shutil.rmtree(tmp1)
        shutil.rmtree(tmp2)
        shutil.rmtree(out)


def test_merge_glbs_single_mod():
    """Single mod has no renames (backward compat)."""
    from cg3h_builder_entry import _merge_glbs
    import pygltflib

    glb1, tmp1 = _make_glb(['Crown', 'Belt'], bone_names=['bone0'], mod_id='ModA')
    out = tempfile.mkdtemp()
    try:
        char_mods = [{'id': 'ModA', 'glb_path': glb1}]
        merged_path, collisions = _merge_glbs(char_mods, out, 'TestChar')
        gltf = pygltflib.GLTF2().load(merged_path)
        names = [m.name for m in gltf.meshes]
        assert 'Crown' in names
        assert 'Belt' in names
        assert len(collisions) == 0
    finally:
        shutil.rmtree(tmp1)
        shutil.rmtree(out)


def test_merge_manifests_with_collisions():
    """Manifest mesh names prefixed per-mod when collisions exist."""
    from cg3h_builder_entry import _merge_manifests

    tmpdir = tempfile.mkdtemp()
    try:
        m1_path = os.path.join(tmpdir, 'mod1', 'manifest.json')
        m2_path = os.path.join(tmpdir, 'mod2', 'manifest.json')
        os.makedirs(os.path.dirname(m1_path))
        os.makedirs(os.path.dirname(m2_path))

        with open(m1_path, 'w') as f:
            json.dump({'mesh_entries': ['Body_Mesh'], 'meshes': [
                {'name': 'Crown', 'entry': 'Body_Mesh', 'gr2_index': 0}
            ]}, f)
        with open(m2_path, 'w') as f:
            json.dump({'mesh_entries': ['Body_Mesh'], 'meshes': [
                {'name': 'Crown', 'entry': 'Body_Mesh', 'gr2_index': 1}
            ]}, f)

        collisions = {'Crown'}
        merged = _merge_manifests([
            {'id': 'ModA', 'manifest_path': m1_path},
            {'id': 'ModB', 'manifest_path': m2_path},
        ], collisions=collisions)

        mesh_names = [m['name'] for m in merged['meshes']]
        assert 'ModA_Crown' in mesh_names, f"ModA_Crown missing: {mesh_names}"
        assert 'ModB_Crown' in mesh_names, f"ModB_Crown missing: {mesh_names}"
        assert len(merged['meshes']) == 2, f"Expected 2 meshes: {mesh_names}"
    finally:
        shutil.rmtree(tmpdir)


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Animation merge tests
# ═══════════════════════════════════════════════════════════════════════════════

def test_merge_glbs_animation_copy():
    """Animations from second GLB are copied into merged GLB."""
    from cg3h_builder_entry import _merge_glbs
    import pygltflib

    bones = ['hip', 'spine']
    anim1 = ('IdleAnim', [
        ('hip', 'translation', [0.0, 1.0], [0,0,0, 1,1,1]),
    ])
    anim2 = ('RunAnim', [
        ('spine', 'translation', [0.0, 1.0], [0,0,0, 2,2,2]),
    ])

    glb1, tmp1 = _make_glb(['MeshA'], bone_names=bones, animations=[anim1], mod_id='ModA')
    glb2, tmp2 = _make_glb(['MeshB'], bone_names=bones, animations=[anim2], mod_id='ModB')
    out = tempfile.mkdtemp()
    try:
        char_mods = [
            {'id': 'ModA', 'glb_path': glb1},
            {'id': 'ModB', 'glb_path': glb2},
        ]
        merged_path, _collisions = _merge_glbs(char_mods, out, 'TestChar')
        gltf = pygltflib.GLTF2().load(merged_path)
        anim_names = [a.name for a in gltf.animations]
        assert 'IdleAnim' in anim_names, f"IdleAnim missing: {anim_names}"
        assert 'RunAnim' in anim_names, f"RunAnim missing: {anim_names}"
        assert len(gltf.animations) == 2
    finally:
        shutil.rmtree(tmp1)
        shutil.rmtree(tmp2)
        shutil.rmtree(out)


def test_merge_glbs_animation_node_remap():
    """Animation channels remap to correct bone nodes in merged GLB."""
    from cg3h_builder_entry import _merge_glbs
    import pygltflib

    # Mod A: MeshA node at 0, then bones hip(1), spine(2)
    # Mod B: MeshB node at 0, then bones hip(1), spine(2)
    # Merged: MeshA(0), MeshB(1), hip(2), spine(3)
    # Animation from ModB targeting hip should point to node 2 in merged
    bones = ['hip', 'spine']
    anim_b = ('WalkAnim', [
        ('hip', 'translation', [0.0, 0.5], [0,0,0, 1,0,0]),
    ])

    glb1, tmp1 = _make_glb(['MeshA'], bone_names=bones, mod_id='ModA')
    glb2, tmp2 = _make_glb(['MeshB'], bone_names=bones, animations=[anim_b], mod_id='ModB')
    out = tempfile.mkdtemp()
    try:
        char_mods = [
            {'id': 'ModA', 'glb_path': glb1},
            {'id': 'ModB', 'glb_path': glb2},
        ]
        merged_path, _collisions = _merge_glbs(char_mods, out, 'TestChar')
        gltf = pygltflib.GLTF2().load(merged_path)

        # Find the hip node index in merged GLB
        hip_idx = None
        for i, node in enumerate(gltf.nodes):
            if node.name == 'hip':
                hip_idx = i
                break
        assert hip_idx is not None, "hip node not found in merged GLB"

        # The animation channel should point to hip_idx
        walk_anim = [a for a in gltf.animations if a.name == 'WalkAnim'][0]
        assert walk_anim.channels[0].target.node == hip_idx, \
            f"Expected node {hip_idx}, got {walk_anim.channels[0].target.node}"
    finally:
        shutil.rmtree(tmp1)
        shutil.rmtree(tmp2)
        shutil.rmtree(out)


def test_merge_glbs_animation_dedup():
    """Same animation name in two mods: last wins."""
    from cg3h_builder_entry import _merge_glbs
    import pygltflib

    bones = ['hip']
    anim1 = ('SharedAnim', [
        ('hip', 'translation', [0.0, 1.0], [0,0,0, 1,1,1]),
    ])
    anim2 = ('SharedAnim', [
        ('hip', 'translation', [0.0, 1.0], [0,0,0, 9,9,9]),
    ])

    glb1, tmp1 = _make_glb(['MeshA'], bone_names=bones, animations=[anim1], mod_id='ModA')
    glb2, tmp2 = _make_glb(['MeshB'], bone_names=bones, animations=[anim2], mod_id='ModB')
    out = tempfile.mkdtemp()
    try:
        char_mods = [
            {'id': 'ModA', 'glb_path': glb1},
            {'id': 'ModB', 'glb_path': glb2},
        ]
        merged_path, _collisions = _merge_glbs(char_mods, out, 'TestChar')
        gltf = pygltflib.GLTF2().load(merged_path)
        shared = [a for a in gltf.animations if a.name == 'SharedAnim']
        assert len(shared) == 1, f"Expected 1 SharedAnim, got {len(shared)}"
        # Verify it's the last mod's version (values 9,9,9)
        ch = shared[0].channels[0]
        sampler = shared[0].samplers[ch.sampler]
        acc = gltf.accessors[sampler.output]
        bv = gltf.bufferViews[acc.bufferView]
        blob = gltf.binary_blob()
        data = struct.unpack_from(f'<{acc.count * 3}f', blob, bv.byteOffset + acc.byteOffset)
        # Last 3 values should be 9,9,9
        assert data[-3:] == (9.0, 9.0, 9.0), f"Expected (9,9,9), got {data[-3:]}"
    finally:
        shutil.rmtree(tmp1)
        shutil.rmtree(tmp2)
        shutil.rmtree(out)


# ═══════════════════════════════════════════════════════════════════════════════
# 12. Mesh change detection tests
# ═══════════════════════════════════════════════════════════════════════════════

def _make_glb_with_uvs(mesh_name, positions, uvs, indices, normals=None):
    """Build a GLB with one mesh that has positions, UVs, optional normals.

    positions: list of (x,y,z) tuples
    uvs: list of (u,v) tuples
    indices: list of int (triangle indices)
    normals: list of (nx,ny,nz) tuples or None
    Returns (glb_path, tmpdir).
    """
    import pygltflib

    blob = bytearray()
    buffer_views = []
    accessors = []
    vc = len(positions)

    # Positions
    pos_bytes = struct.pack(f'<{vc*3}f', *[c for p in positions for c in p])
    pos_off = len(blob)
    blob.extend(pos_bytes)
    bv_pos = pygltflib.BufferView(buffer=0, byteOffset=pos_off, byteLength=len(pos_bytes))
    buffer_views.append(bv_pos)
    acc_pos = pygltflib.Accessor(
        bufferView=len(buffer_views)-1, byteOffset=0,
        componentType=pygltflib.FLOAT, count=vc, type='VEC3')
    accessors.append(acc_pos)
    pos_acc_idx = len(accessors) - 1

    # UVs
    uv_bytes = struct.pack(f'<{vc*2}f', *[c for u in uvs for c in u])
    uv_off = len(blob)
    blob.extend(uv_bytes)
    bv_uv = pygltflib.BufferView(buffer=0, byteOffset=uv_off, byteLength=len(uv_bytes))
    buffer_views.append(bv_uv)
    acc_uv = pygltflib.Accessor(
        bufferView=len(buffer_views)-1, byteOffset=0,
        componentType=pygltflib.FLOAT, count=vc, type='VEC2')
    accessors.append(acc_uv)
    uv_acc_idx = len(accessors) - 1

    # Normals (optional)
    nrm_acc_idx = None
    if normals:
        nrm_bytes = struct.pack(f'<{vc*3}f', *[c for n in normals for c in n])
        nrm_off = len(blob)
        blob.extend(nrm_bytes)
        bv_nrm = pygltflib.BufferView(buffer=0, byteOffset=nrm_off, byteLength=len(nrm_bytes))
        buffer_views.append(bv_nrm)
        acc_nrm = pygltflib.Accessor(
            bufferView=len(buffer_views)-1, byteOffset=0,
            componentType=pygltflib.FLOAT, count=vc, type='VEC3')
        accessors.append(acc_nrm)
        nrm_acc_idx = len(accessors) - 1

    # Indices
    ic = len(indices)
    idx_bytes = struct.pack(f'<{ic}H', *indices)
    idx_off = len(blob)
    blob.extend(idx_bytes)
    bv_idx = pygltflib.BufferView(buffer=0, byteOffset=idx_off, byteLength=len(idx_bytes))
    buffer_views.append(bv_idx)
    acc_idx = pygltflib.Accessor(
        bufferView=len(buffer_views)-1, byteOffset=0,
        componentType=pygltflib.UNSIGNED_SHORT, count=ic, type='SCALAR')
    accessors.append(acc_idx)
    idx_acc_idx = len(accessors) - 1

    attrs = pygltflib.Attributes(POSITION=pos_acc_idx, TEXCOORD_0=uv_acc_idx)
    if nrm_acc_idx is not None:
        attrs.NORMAL = nrm_acc_idx
    prim = pygltflib.Primitive(attributes=attrs, indices=idx_acc_idx)
    mesh = pygltflib.Mesh(name=mesh_name, primitives=[prim])
    node = pygltflib.Node(name=mesh_name, mesh=0)

    gltf = pygltflib.GLTF2(
        scene=0,
        scenes=[pygltflib.Scene(nodes=[0])],
        nodes=[node],
        meshes=[mesh],
        accessors=accessors,
        bufferViews=buffer_views,
        buffers=[pygltflib.Buffer(byteLength=len(blob))],
    )
    gltf.set_binary_blob(bytes(blob))

    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, 'test.glb')
    gltf.save(path)
    return path, tmpdir


# Base triangle data used by all change detection tests
_CD_POSITIONS = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
_CD_UVS = [(0, 0), (1, 0), (0, 1)]
_CD_NORMALS = [(0, 0, 1), (0, 0, 1), (0, 0, 1)]
_CD_INDICES = [0, 1, 2]


def _make_manifest_and_baseline(mesh_name, positions, tmpdir):
    """Create manifest.json and .baseline_positions.npz for change detection tests."""
    manifest = {
        'meshes': [{
            'name': mesh_name,
            'entry': 'Body_Mesh',
            'gr2_index': 0,
            'vertex_count': len(positions),
            'index_count': len(_CD_INDICES),
            'position_hash': 'unused',
        }]
    }
    with open(os.path.join(tmpdir, 'manifest.json'), 'w') as f:
        json.dump(manifest, f)

    baseline = {mesh_name: np.array(positions, dtype=np.float32)}
    np.savez_compressed(os.path.join(tmpdir, '.baseline_positions.npz'), **baseline)
    return manifest


def test_change_detect_unchanged_same_count():
    """Same vertex count, same positions (within noise) → unchanged."""
    from cg3h_build import _is_mesh_changed
    import pygltflib

    # Simulate Blender noise: add ~1e-5 to positions
    noisy_pos = [(p[0] + 1e-5, p[1] - 1e-5, p[2] + 2e-5) for p in _CD_POSITIONS]
    glb_path, tmpdir = _make_glb_with_uvs('TestMesh', noisy_pos, _CD_UVS, _CD_INDICES)
    try:
        manifest = _make_manifest_and_baseline('TestMesh', _CD_POSITIONS, tmpdir)
        baseline = dict(np.load(os.path.join(tmpdir, '.baseline_positions.npz')))

        gltf = pygltflib.GLTF2().load(glb_path)
        blob = gltf.binary_blob()
        orig = manifest['meshes'][0]
        result = _is_mesh_changed(gltf, gltf.meshes[0], blob, orig, baseline.get('TestMesh'))
        assert result is False, "Noise-only change should be undetected"
    finally:
        shutil.rmtree(tmpdir)


def test_change_detect_position_edit():
    """Same vertex count, position moved by 0.01 → changed."""
    from cg3h_build import _is_mesh_changed
    import pygltflib

    edited_pos = [(0.01, 0, 0), (1, 0, 0), (0, 1, 0)]  # first vertex moved
    glb_path, tmpdir = _make_glb_with_uvs('TestMesh', edited_pos, _CD_UVS, _CD_INDICES)
    try:
        manifest = _make_manifest_and_baseline('TestMesh', _CD_POSITIONS, tmpdir)
        baseline = dict(np.load(os.path.join(tmpdir, '.baseline_positions.npz')))

        gltf = pygltflib.GLTF2().load(glb_path)
        blob = gltf.binary_blob()
        result = _is_mesh_changed(gltf, gltf.meshes[0], blob,
                                  manifest['meshes'][0], baseline.get('TestMesh'))
        assert result is True, "Position edit should be detected"
    finally:
        shutil.rmtree(tmpdir)


def test_change_detect_normal_split_unchanged():
    """More vertices from normal splits but same unique (pos, UV) → unchanged."""
    from cg3h_build import _is_mesh_changed
    import pygltflib

    # Simulate Blender normal split: duplicate vertex 0 with different normal
    split_pos = list(_CD_POSITIONS) + [_CD_POSITIONS[0]]  # 4 verts, dup of v0
    split_uvs = list(_CD_UVS) + [_CD_UVS[0]]              # same UV
    split_idx = [0, 1, 2, 3, 1, 2]                         # extra tri using dup
    split_nrm = list(_CD_NORMALS) + [(0, 1, 0)]            # different normal

    glb_path, tmpdir = _make_glb_with_uvs('TestMesh', split_pos, split_uvs,
                                          split_idx, normals=split_nrm)
    try:
        manifest = _make_manifest_and_baseline('TestMesh', _CD_POSITIONS, tmpdir)
        baseline = dict(np.load(os.path.join(tmpdir, '.baseline_positions.npz')))

        gltf = pygltflib.GLTF2().load(glb_path)
        blob = gltf.binary_blob()
        result = _is_mesh_changed(gltf, gltf.meshes[0], blob,
                                  manifest['meshes'][0], baseline.get('TestMesh'))
        assert result is False, "Normal-split inflation should be undetected"
    finally:
        shutil.rmtree(tmpdir)


def test_change_detect_genuine_new_vertex():
    """More vertices with genuinely new positions → changed."""
    from cg3h_build import _is_mesh_changed
    import pygltflib

    new_pos = list(_CD_POSITIONS) + [(0.5, 0.5, 0)]  # genuinely new vertex
    new_uvs = list(_CD_UVS) + [(0.5, 0.5)]            # new UV
    new_idx = [0, 1, 2, 0, 3, 2]

    glb_path, tmpdir = _make_glb_with_uvs('TestMesh', new_pos, new_uvs, new_idx)
    try:
        manifest = _make_manifest_and_baseline('TestMesh', _CD_POSITIONS, tmpdir)
        baseline = dict(np.load(os.path.join(tmpdir, '.baseline_positions.npz')))

        gltf = pygltflib.GLTF2().load(glb_path)
        blob = gltf.binary_blob()
        result = _is_mesh_changed(gltf, gltf.meshes[0], blob,
                                  manifest['meshes'][0], baseline.get('TestMesh'))
        assert result is True, "Genuine new vertex should be detected"
    finally:
        shutil.rmtree(tmpdir)


def test_change_detect_fewer_vertices():
    """Fewer vertices than original → changed."""
    from cg3h_build import _is_mesh_changed
    import pygltflib

    fewer_pos = [_CD_POSITIONS[0], _CD_POSITIONS[1]]  # only 2 verts
    fewer_uvs = [_CD_UVS[0], _CD_UVS[1]]
    fewer_idx = [0, 1, 0]

    glb_path, tmpdir = _make_glb_with_uvs('TestMesh', fewer_pos, fewer_uvs, fewer_idx)
    try:
        manifest = _make_manifest_and_baseline('TestMesh', _CD_POSITIONS, tmpdir)
        baseline = dict(np.load(os.path.join(tmpdir, '.baseline_positions.npz')))

        gltf = pygltflib.GLTF2().load(glb_path)
        blob = gltf.binary_blob()
        result = _is_mesh_changed(gltf, gltf.meshes[0], blob,
                                  manifest['meshes'][0], baseline.get('TestMesh'))
        assert result is True, "Fewer vertices should be detected as changed"
    finally:
        shutil.rmtree(tmpdir)


def test_change_detect_no_baseline_fallback():
    """Without baseline file, same vertex count → unchanged (graceful fallback)."""
    from cg3h_build import _is_mesh_changed
    import pygltflib

    glb_path, tmpdir = _make_glb_with_uvs('TestMesh', _CD_POSITIONS, _CD_UVS, _CD_INDICES)
    try:
        manifest = _make_manifest_and_baseline('TestMesh', _CD_POSITIONS, tmpdir)

        gltf = pygltflib.GLTF2().load(glb_path)
        blob = gltf.binary_blob()
        # No baseline passed — should fall back to count-based check
        result = _is_mesh_changed(gltf, gltf.meshes[0], blob,
                                  manifest['meshes'][0], None)
        assert result is False, "No baseline + same count should be unchanged"
    finally:
        shutil.rmtree(tmpdir)


def test_strip_keeps_new_mesh_strips_original():
    """_strip_unchanged_data keeps new meshes, strips unchanged originals."""
    from cg3h_build import _strip_unchanged_data
    import pygltflib

    tmpdir = tempfile.mkdtemp()
    try:
        # Create manifest with one original mesh
        manifest = {
            'meshes': [{
                'name': 'OriginalMesh',
                'entry': 'Body_Mesh',
                'gr2_index': 0,
                'vertex_count': 3,
                'index_count': 3,
                'position_hash': 'unused',
            }]
        }
        with open(os.path.join(tmpdir, 'manifest.json'), 'w') as f:
            json.dump(manifest, f)

        # Save baseline for original mesh
        baseline = {'OriginalMesh': np.array(_CD_POSITIONS, dtype=np.float32)}
        np.savez_compressed(os.path.join(tmpdir, '.baseline_positions.npz'), **baseline)

        # Build GLB with original mesh (unchanged) + new mesh
        import pygltflib as pgl
        blob = bytearray()
        buffer_views = []
        accessors = []
        meshes_list = []
        nodes_list = []

        for mi, (name, positions) in enumerate([
            ('OriginalMesh', _CD_POSITIONS),
            ('NewMesh', [(2, 0, 0), (3, 0, 0), (2, 1, 0)]),
        ]):
            vc = len(positions)
            pos_bytes = struct.pack(f'<{vc*3}f', *[c for p in positions for c in p])
            pos_off = len(blob)
            blob.extend(pos_bytes)
            buffer_views.append(pgl.BufferView(buffer=0, byteOffset=pos_off,
                                               byteLength=len(pos_bytes)))
            accessors.append(pgl.Accessor(
                bufferView=len(buffer_views)-1, byteOffset=0,
                componentType=pgl.FLOAT, count=vc, type='VEC3'))
            pos_ai = len(accessors) - 1

            uv_bytes = struct.pack(f'<{vc*2}f', *[c for u in _CD_UVS for c in u])
            uv_off = len(blob)
            blob.extend(uv_bytes)
            buffer_views.append(pgl.BufferView(buffer=0, byteOffset=uv_off,
                                               byteLength=len(uv_bytes)))
            accessors.append(pgl.Accessor(
                bufferView=len(buffer_views)-1, byteOffset=0,
                componentType=pgl.FLOAT, count=vc, type='VEC2'))
            uv_ai = len(accessors) - 1

            idx_bytes = struct.pack('<3H', 0, 1, 2)
            idx_off = len(blob)
            blob.extend(idx_bytes)
            buffer_views.append(pgl.BufferView(buffer=0, byteOffset=idx_off,
                                               byteLength=len(idx_bytes)))
            accessors.append(pgl.Accessor(
                bufferView=len(buffer_views)-1, byteOffset=0,
                componentType=pgl.UNSIGNED_SHORT, count=3, type='SCALAR'))
            idx_ai = len(accessors) - 1

            prim = pgl.Primitive(
                attributes=pgl.Attributes(POSITION=pos_ai, TEXCOORD_0=uv_ai),
                indices=idx_ai)
            meshes_list.append(pgl.Mesh(name=name, primitives=[prim]))
            nodes_list.append(pgl.Node(name=name, mesh=mi))

        gltf = pgl.GLTF2(
            scene=0,
            scenes=[pgl.Scene(nodes=list(range(len(nodes_list))))],
            nodes=nodes_list,
            meshes=meshes_list,
            accessors=accessors,
            bufferViews=buffer_views,
            buffers=[pgl.Buffer(byteLength=len(blob))],
        )
        gltf.set_binary_blob(bytes(blob))
        glb_path = os.path.join(tmpdir, 'test.glb')
        gltf.save(glb_path)

        # Run strip
        result = _strip_unchanged_data(glb_path, tmpdir)
        assert result is not None, "Should produce stripped GLB"

        # Load stripped GLB and verify only NewMesh remains
        stripped_path = os.path.join(tmpdir, 'stripped.glb')
        with open(stripped_path, 'wb') as f:
            f.write(result)
        stripped = pgl.GLTF2().load(stripped_path)
        mesh_names = [m.name for m in stripped.meshes]
        assert 'NewMesh' in mesh_names, f"NewMesh should be kept: {mesh_names}"
        assert 'OriginalMesh' not in mesh_names, f"OriginalMesh should be stripped: {mesh_names}"
    finally:
        shutil.rmtree(tmpdir)


def test_strip_keeps_edited_original():
    """_strip_unchanged_data keeps an original mesh that was edited."""
    from cg3h_build import _strip_unchanged_data
    import pygltflib as pgl

    tmpdir = tempfile.mkdtemp()
    try:
        manifest = {
            'meshes': [{
                'name': 'EditedMesh',
                'entry': 'Body_Mesh',
                'gr2_index': 0,
                'vertex_count': 3,
                'index_count': 3,
                'position_hash': 'unused',
            }]
        }
        with open(os.path.join(tmpdir, 'manifest.json'), 'w') as f:
            json.dump(manifest, f)

        baseline = {'EditedMesh': np.array(_CD_POSITIONS, dtype=np.float32)}
        np.savez_compressed(os.path.join(tmpdir, '.baseline_positions.npz'), **baseline)

        # Build GLB with edited positions (moved vertex by 0.05)
        edited_pos = [(0.05, 0, 0), (1, 0, 0), (0, 1, 0)]
        glb_path, glb_tmpdir = _make_glb_with_uvs('EditedMesh', edited_pos,
                                                   _CD_UVS, _CD_INDICES)
        # Copy GLB to mod dir
        import shutil as sh
        sh.copy2(glb_path, os.path.join(tmpdir, 'test.glb'))
        sh.rmtree(glb_tmpdir)

        result = _strip_unchanged_data(os.path.join(tmpdir, 'test.glb'), tmpdir)
        assert result is not None, "Edited mesh should produce output"

        stripped_path = os.path.join(tmpdir, 'stripped.glb')
        with open(stripped_path, 'wb') as f:
            f.write(result)
        stripped = pgl.GLTF2().load(stripped_path)
        mesh_names = [m.name for m in stripped.meshes]
        assert 'EditedMesh' in mesh_names, f"Edited mesh should be kept: {mesh_names}"
    finally:
        shutil.rmtree(tmpdir)


# ═══════════════════════════════════════════════════════════════════════════════
# 13. Manifest builder tests (Phase 0 of v3.4)
# ═══════════════════════════════════════════════════════════════════════════════

def test_manifest_has_bb_names():
    """build_manifest writes bb_names per mesh."""
    from gr2_to_gltf import build_manifest

    pos = np.array([[0,0,0],[1,0,0],[0,1,0]], dtype=np.float32)
    normals = np.array([[0,0,1]]*3, dtype=np.float32)
    uvs = np.array([[0,0],[1,0],[0,1]], dtype=np.float32)
    indices = np.array([0,1,2], dtype=np.uint16)
    md = (pos, normals, uvs, None, None, indices)

    manifest = build_manifest(
        character='TestChar',
        out_basename='TestChar.glb',
        mesh_entries=['Body_Mesh'],
        mesh_names=['Mesh1'],
        exported_gr2_indices=[{'entry': 'Body_Mesh', 'gr2_index': 0}],
        mesh_data_list=[md],
        mesh_bb_names_list=[['BoneA', 'BoneB', 'BoneC']],
    )

    assert manifest['character'] == 'TestChar'
    assert len(manifest['meshes']) == 1
    m = manifest['meshes'][0]
    assert m['name'] == 'Mesh1'
    assert m['vertex_count'] == 3
    assert m['index_count'] == 3
    assert m['bb_names'] == ['BoneA', 'BoneB', 'BoneC']
    assert 'position_hash' in m


def test_manifest_textures_optional():
    """build_manifest only includes textures field when provided."""
    from gr2_to_gltf import build_manifest

    pos = np.array([[0,0,0]], dtype=np.float32)
    md = (pos, None, None, None, None, np.array([0], dtype=np.uint16))

    m1 = build_manifest('C', 'C.glb', [], [], [], [], [])
    assert 'textures' not in m1
    assert 'animations' not in m1

    m2 = build_manifest('C', 'C.glb', ['E'], ['M'],
                        [{'entry': 'E', 'gr2_index': 0}], [md], [['B']],
                        manifest_textures={'tex1': {'png_hash': 'abc'}},
                        anim_data=[{'name': 'Idle'}])
    assert m2['textures'] == {'tex1': {'png_hash': 'abc'}}
    assert m2['animations'] == {'count': 1, 'names': ['Idle']}


# ═══════════════════════════════════════════════════════════════════════════════
# 14. cg3h_core helpers (Phase 1 of v3.4)
# ═══════════════════════════════════════════════════════════════════════════════

def _make_test_manifest():
    return {
        'character': 'Test',
        'mesh_entries': ['Battle_Mesh', 'Hub_Mesh'],
        'meshes': [
            {'name': 'BattleBody', 'entry': 'Battle_Mesh', 'gr2_index': 0,
             'bb_names': ['Hip', 'Spine', 'Head']},
            {'name': 'BattleOutline', 'entry': 'Battle_Mesh', 'gr2_index': 1,
             'bb_names': ['Hip', 'Spine']},
            {'name': 'HubBody', 'entry': 'Hub_Mesh', 'gr2_index': 0,
             'bb_names': ['Hip', 'Spine', 'Crown']},
            {'name': 'HubShadowMesh', 'entry': 'Hub_Mesh', 'gr2_index': 1,
             'bb_names': ['Hip']},
        ],
    }


def test_select_template_exact_overlap():
    """Template with most overlapping bones is picked."""
    from cg3h_core import select_template

    m = _make_test_manifest()
    # Active bones include Crown, only HubBody has it
    tpl = select_template(m, {'Hip', 'Spine', 'Crown'})
    assert tpl is not None
    assert tpl['name'] == 'HubBody'


def test_select_template_skips_outline_shadow():
    """Outline and Shadow meshes are never templates."""
    from cg3h_core import select_template

    m = _make_test_manifest()
    tpl = select_template(m, {'Hip'})
    assert tpl is not None
    assert 'Outline' not in tpl['name']
    assert 'Shadow' not in tpl['name']


def test_select_template_restrict_entries():
    """restrict_entries limits candidates."""
    from cg3h_core import select_template

    m = _make_test_manifest()
    tpl = select_template(m, {'Hip', 'Spine', 'Crown'},
                          restrict_entries={'Battle_Mesh'})
    assert tpl is not None
    assert tpl['entry'] == 'Battle_Mesh'
    assert tpl['name'] == 'BattleBody'


def test_select_template_empty_manifest():
    """No manifest returns None."""
    from cg3h_core import select_template
    assert select_template(None, {'Hip'}) is None
    assert select_template({'meshes': []}, {'Hip'}) is None


def test_visible_bones_per_entry():
    """ALL respects routing; entry/mesh presets return their bb_names."""
    # Replicates the logic of _compute_visible_bones from __init__.py
    # without importing bpy.
    m = _make_test_manifest()

    def _compute_visible(manifest, preset, target_entries=None):
        if not manifest or preset == "WHOLE":
            return None
        if preset == "ALL":
            if not target_entries:
                return None  # no routing context — show literally everything
            v = set()
            for mesh in manifest.get('meshes', []):
                if mesh.get('entry') in target_entries:
                    v.update(mesh.get('bb_names', []))
            return v
        if preset.startswith("E:"):
            entry = preset[2:]
            v = set()
            for mesh in manifest.get('meshes', []):
                if mesh.get('entry') == entry:
                    v.update(mesh.get('bb_names', []))
            return v
        if preset.startswith("M:"):
            for mesh in manifest.get('meshes', []):
                if mesh.get('name') == preset[2:]:
                    return set(mesh.get('bb_names', []))
            return set()
        return None

    # WHOLE → None (literal everything regardless of routing)
    assert _compute_visible(m, "WHOLE", target_entries={'Battle_Mesh'}) is None
    # ALL with no routing context → None (show literally everything)
    assert _compute_visible(m, "ALL") is None
    # ALL with Battle routing → union of all Battle meshes' bb_names
    assert _compute_visible(m, "ALL", target_entries={'Battle_Mesh'}) == {'Hip', 'Spine', 'Head'}
    # ALL with both entries routed → union of all bb_names
    assert _compute_visible(m, "ALL", target_entries={'Battle_Mesh', 'Hub_Mesh'}) == {
        'Hip', 'Spine', 'Head', 'Crown'}
    # Explicit entry preset → union of that entry's meshes
    assert _compute_visible(m, "E:Battle_Mesh") == {'Hip', 'Spine', 'Head'}
    # Hub entry → HubBody {Hip,Spine,Crown} + HubShadowMesh {Hip}
    assert _compute_visible(m, "E:Hub_Mesh") == {'Hip', 'Spine', 'Crown'}
    # Specific mesh
    assert _compute_visible(m, "M:HubBody") == {'Hip', 'Spine', 'Crown'}
    # Unknown mesh → empty set
    assert _compute_visible(m, "M:Nonexistent") == set()


def test_find_weight_violations_new_mesh():
    """Weight on a bone not in the template's bindings is flagged."""
    from cg3h_core import find_weight_violations

    data = [{'name': 'Cube', 'is_original': False,
             'groups': {'Hip': 12, 'Tail': 3}}]
    lookup = {'Cube': {'Hip', 'Spine'}}
    v = find_weight_violations(data, lookup)
    assert len(v) == 1
    assert v[0]['mesh'] == 'Cube'
    assert v[0]['bone'] == 'Tail'
    assert v[0]['vertex_count'] == 3
    assert v[0]['is_original'] is False


def test_find_weight_violations_original_flagged_too():
    """Existing meshes are flagged with same severity."""
    from cg3h_core import find_weight_violations

    data = [{'name': 'BattleBody', 'is_original': True,
             'groups': {'Hip': 5, 'AlienBone': 1}}]
    lookup = {'BattleBody': {'Hip'}}
    v = find_weight_violations(data, lookup)
    assert len(v) == 1
    assert v[0]['is_original'] is True


def test_find_weight_violations_unknown_mesh_skipped():
    """Mesh not in lookup is silently skipped."""
    from cg3h_core import find_weight_violations

    data = [{'name': 'Mystery', 'is_original': False,
             'groups': {'Hip': 5}}]
    v = find_weight_violations(data, {})
    assert v == []


def test_find_weight_violations_zero_count_ignored():
    """A group with zero vertex count is not a violation."""
    from cg3h_core import find_weight_violations

    data = [{'name': 'Cube', 'is_original': False,
             'groups': {'Tail': 0}}]
    v = find_weight_violations(data, {'Cube': {'Hip'}})
    assert v == []


# ═══════════════════════════════════════════════════════════════════════════════
# 15. mod_info helpers (extracted from old mod_merger in v3.5.1)
# ═══════════════════════════════════════════════════════════════════════════════

def _make_mod(mod_id, character, mod_type='mesh_replace', textures=None,
              extras=None):
    """Build a minimal mod info dict matching mod_info.scan_cg3h_mods output."""
    mod = {
        'format': 'cg3h-mod/1.0',
        'metadata': {'name': mod_id, 'author': 'tester', 'version': '1.0.0'},
        'type': mod_type,
        'target': {'character': character},
        'assets': {'glb': f'{character}.glb'},
    }
    if textures:
        mod['assets']['textures'] = textures
    if extras:
        mod.update(extras)
    return {
        'id': mod_id,
        'mod_json_path': f'/fake/{mod_id}/mod.json',
        'plugin_path': f'/fake/{mod_id}',
        'data_path': f'/fake/{mod_id}',
        'mod': mod,
    }


def test_mod_info_group_by_character():
    """group_by_character clusters mods by their target.character field."""
    from mod_info import group_by_character

    mods = [
        _make_mod('A', 'Melinoe'),
        _make_mod('B', 'Melinoe'),
        _make_mod('C', 'Hecate'),
        _make_mod('D', 'Melinoe'),
    ]
    groups = group_by_character(mods)
    assert set(groups.keys()) == {'Melinoe', 'Hecate'}
    assert len(groups['Melinoe']) == 3
    assert len(groups['Hecate']) == 1
    assert {m['id'] for m in groups['Melinoe']} == {'A', 'B', 'D'}


def test_mod_info_group_skips_no_character():
    """Mods without target.character are skipped."""
    from mod_info import group_by_character

    mods = [
        _make_mod('A', 'Melinoe'),
        {'id': 'no_char', 'mod': {'target': {}}},
    ]
    groups = group_by_character(mods)
    assert list(groups.keys()) == ['Melinoe']
    assert len(groups['Melinoe']) == 1


def test_mod_info_check_conflicts_two_mesh_replace():
    """Two mesh_replace mods on the same character → ERROR."""
    from mod_info import check_conflicts

    group = [
        _make_mod('ModA', 'Hecate', mod_type='mesh_replace'),
        _make_mod('ModB', 'Hecate', mod_type='mesh_replace'),
    ]
    warnings, errors = check_conflicts(group)
    assert any('mesh_replace' in e or 'CONFLICT' in e for e in errors)


def test_mod_info_check_conflicts_mesh_add_only():
    """Two mesh_add mods on the same character → no error (compatible)."""
    from mod_info import check_conflicts

    group = [
        _make_mod('ModA', 'Hecate', mod_type='mesh_add'),
        _make_mod('ModB', 'Hecate', mod_type='mesh_add'),
    ]
    warnings, errors = check_conflicts(group)
    assert errors == []


def test_mod_info_check_conflicts_same_texture():
    """Two mods modifying the same texture name → ERROR."""
    from mod_info import check_conflicts

    group = [
        _make_mod('ModA', 'Hecate', mod_type='texture_replace',
                  textures=[{'name': 'Hecate_Color'}]),
        _make_mod('ModB', 'Hecate', mod_type='texture_replace',
                  textures=[{'name': 'Hecate_Color'}]),
    ]
    warnings, errors = check_conflicts(group)
    assert any('Hecate_Color' in e for e in errors)


def test_mod_info_check_conflicts_different_textures():
    """Two mods with different texture names → no error."""
    from mod_info import check_conflicts

    group = [
        _make_mod('ModA', 'Hecate', mod_type='texture_replace',
                  textures=[{'name': 'Hecate_Color'}]),
        _make_mod('ModB', 'Hecate', mod_type='texture_replace',
                  textures=[{'name': 'Hecate_Normal'}]),
    ]
    warnings, errors = check_conflicts(group)
    assert errors == []


def test_mod_info_check_conflicts_mixed_add_replace():
    """mesh_add + mesh_replace → warning (not error)."""
    from mod_info import check_conflicts

    group = [
        _make_mod('ModA', 'Hecate', mod_type='mesh_add'),
        _make_mod('ModB', 'Hecate', mod_type='mesh_replace'),
    ]
    warnings, errors = check_conflicts(group)
    assert errors == []
    assert any('mesh_add' in w and 'mesh_replace' in w for w in warnings)


def test_mod_info_check_conflicts_custom_texture_shared():
    """v3.6: two mods shipping a custom texture with the same name → INFO
    warning, no error (auto-prefixed at build time)."""
    from mod_info import check_conflicts

    group = [
        _make_mod('ModA', 'Hecate', mod_type='mesh_add',
                  textures=[{'name': 'HecateColor', 'custom': True}]),
        _make_mod('ModB', 'Hecate', mod_type='mesh_add',
                  textures=[{'name': 'HecateColor', 'custom': True}]),
    ]
    warnings, errors = check_conflicts(group)
    assert errors == []
    assert any('HecateColor' in w and 'auto-prefixed' in w for w in warnings)


def test_mod_info_check_conflicts_custom_vs_replace_isolated():
    """v3.6: a custom texture and a texture_replace target sharing a name
    do not interfere — the custom texture is auto-prefixed at build, so the
    replace target stands alone (no error, no warning for that pair)."""
    from mod_info import check_conflicts

    group = [
        _make_mod('ModA', 'Hecate', mod_type='mesh_add',
                  textures=[{'name': 'Hecate_Color', 'custom': True}]),
        _make_mod('ModB', 'Hecate', mod_type='texture_replace',
                  textures=[{'name': 'Hecate_Color'}]),
    ]
    warnings, errors = check_conflicts(group)
    assert errors == []


def test_version_consistency():
    """cg3h_constants.CG3H_VERSION must match the Thunderstore manifest's
    version_number.  Bumping the release version in only one place is a
    common mistake — this test catches the drift before release."""
    from cg3h_constants import CG3H_VERSION

    manifest_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        '.github', 'thunderstore', 'manifest.json')
    with open(manifest_path) as f:
        manifest = json.load(f)
    assert manifest['version_number'] == CG3H_VERSION, (
        f"CG3H_VERSION ({CG3H_VERSION}) and thunderstore/manifest.json "
        f"version_number ({manifest['version_number']}) disagree")


def test_mod_info_priority_roundtrip():
    """load_priority → save_priority → load_priority returns the same data."""
    from mod_info import load_priority, save_priority

    tmpdir = tempfile.mkdtemp()
    try:
        priority = {'Hecate': ['ModA', 'ModB'], 'Melinoe': ['ModC']}
        save_priority(tmpdir, priority)
        loaded = load_priority(tmpdir)
        assert loaded == priority
    finally:
        shutil.rmtree(tmpdir)


def test_mod_info_load_priority_missing_file():
    """load_priority returns an empty dict when the file doesn't exist."""
    from mod_info import load_priority

    tmpdir = tempfile.mkdtemp()
    try:
        assert load_priority(tmpdir) == {}
    finally:
        shutil.rmtree(tmpdir)


def test_mod_info_generate_default_priority():
    """generate_default_priority creates an alphabetical entry per multi-mod character."""
    from mod_info import generate_default_priority

    tmpdir = tempfile.mkdtemp()
    try:
        mods = [
            _make_mod('zeta', 'Hecate'),
            _make_mod('alpha', 'Hecate'),
            _make_mod('beta', 'Hecate'),
            _make_mod('solo', 'Melinoe'),  # only one mod → no entry
        ]
        priority = generate_default_priority(tmpdir, mods)
        assert 'Hecate' in priority
        assert priority['Hecate'] == ['alpha', 'beta', 'zeta']
        assert 'Melinoe' not in priority  # single mod = no priority entry
    finally:
        shutil.rmtree(tmpdir)


def test_mod_info_scan_cg3h_mods_finds_format():
    """scan_cg3h_mods picks up mod.json files with format starting with cg3h-mod."""
    from mod_info import scan_cg3h_mods

    tmpdir = tempfile.mkdtemp()
    try:
        plugins = os.path.join(tmpdir, 'plugins')
        plugins_data = os.path.join(tmpdir, 'plugins_data')
        for mid, fmt in [('GoodMod', 'cg3h-mod/1.0'),
                         ('OtherMod', 'something-else'),
                         ('NoFormat', None)]:
            os.makedirs(os.path.join(plugins, mid))
            os.makedirs(os.path.join(plugins_data, mid))
            mod_json = {'metadata': {'name': mid}}
            if fmt:
                mod_json['format'] = fmt
            with open(os.path.join(plugins_data, mid, 'mod.json'), 'w') as f:
                json.dump(mod_json, f)

        mods = scan_cg3h_mods(tmpdir)
        ids = [m['id'] for m in mods]
        assert 'GoodMod' in ids
        assert 'OtherMod' not in ids
        assert 'NoFormat' not in ids
    finally:
        shutil.rmtree(tmpdir)


def test_mod_info_scan_cg3h_mods_empty_dir():
    """Scanning a directory without plugins/ returns an empty list."""
    from mod_info import scan_cg3h_mods

    tmpdir = tempfile.mkdtemp()
    try:
        assert scan_cg3h_mods(tmpdir) == []
    finally:
        shutil.rmtree(tmpdir)


def test_classify_mod_pure_variant():
    """Pure mesh_replace with target.mesh_entries → is_variant=True."""
    from cg3h_builder_entry import _classify_mod
    mod = {
        "type": "mesh_replace",
        "target": {"character": "Melinoe", "mesh_entries": ["Melinoe_Mesh"]},
    }
    assert _classify_mod(mod) == (True, False, False)


def test_classify_mod_pure_accessory():
    """mesh_add → is_accessory=True regardless of mesh_entries."""
    from cg3h_builder_entry import _classify_mod
    mod = {"type": "mesh_add", "target": {"character": "Melinoe"}}
    assert _classify_mod(mod) == (False, True, False)


def test_classify_mod_mixed_add_and_replace():
    """mesh_add + mesh_replace → accessory wins (additive)."""
    from cg3h_builder_entry import _classify_mod
    mod = {
        "type": ["mesh_add", "mesh_replace"],
        "target": {"character": "Melinoe", "mesh_entries": ["Melinoe_Mesh"]},
    }
    assert _classify_mod(mod) == (False, True, False)


def test_classify_mod_animation_only():
    """animation_patch with no mesh ops → is_animation_only=True."""
    from cg3h_builder_entry import _classify_mod
    mod = {
        "type": "animation_patch",
        "target": {"character": "Melinoe"},
    }
    assert _classify_mod(mod) == (False, False, True)


def test_classify_mod_animation_with_mesh_replace():
    """animation_patch + mesh_replace → variant, NOT animation_only.

    The animation patch still applies (during convert), but the mod
    routes through the mesh-replace path because the mesh is the
    dominant signal.
    """
    from cg3h_builder_entry import _classify_mod
    mod = {
        "type": ["mesh_replace", "animation_patch"],
        "target": {"character": "Melinoe", "mesh_entries": ["Melinoe_Mesh"]},
    }
    assert _classify_mod(mod) == (True, False, False)


def test_classify_mod_animation_with_mesh_add():
    """animation_patch + mesh_add → accessory, NOT animation_only."""
    from cg3h_builder_entry import _classify_mod
    mod = {
        "type": ["mesh_add", "animation_patch"],
        "target": {"character": "Melinoe"},
    }
    assert _classify_mod(mod) == (False, True, False)


def test_classify_mod_pure_texture():
    """texture_replace alone → all three bools False (separate flow)."""
    from cg3h_builder_entry import _classify_mod
    mod = {"type": "texture_replace", "target": {"character": "Melinoe"}}
    assert _classify_mod(mod) == (False, False, False)


def test_classify_mod_mesh_replace_no_entries():
    """mesh_replace without target.mesh_entries → not a variant."""
    from cg3h_builder_entry import _classify_mod
    mod = {"type": "mesh_replace", "target": {"character": "Melinoe"}}
    assert _classify_mod(mod) == (False, False, False)


def _anim_mod(mod_id, character, animations):
    """animation_patch mod fixture with target.animations populated."""
    mi = _make_mod(mod_id, character, mod_type='animation_patch')
    mi['mod']['target']['animations'] = list(animations)
    return mi


def test_check_conflicts_animation_overlap():
    """Two animation_patch mods touching the same animation entry → warning."""
    from mod_info import check_conflicts

    group = [
        _anim_mod('ModA', 'Melinoe', ['Melinoe_Idle', 'Melinoe_Run']),
        _anim_mod('ModB', 'Melinoe', ['Melinoe_Idle', 'Melinoe_Dash']),
    ]
    warnings, errors = check_conflicts(group)
    assert errors == []
    overlap_warnings = [w for w in warnings if 'animation' in w and 'Melinoe_Idle' in w]
    assert len(overlap_warnings) == 1, f"expected one overlap warning, got: {warnings}"
    assert 'ModA' in overlap_warnings[0] and 'ModB' in overlap_warnings[0]


def test_check_conflicts_animation_no_overlap():
    """Two animation_patch mods on disjoint animations → no conflict."""
    from mod_info import check_conflicts

    group = [
        _anim_mod('ModA', 'Melinoe', ['Melinoe_Idle']),
        _anim_mod('ModB', 'Melinoe', ['Melinoe_Run']),
    ]
    warnings, errors = check_conflicts(group)
    assert errors == []
    assert not any('animation' in w and ('Melinoe_Idle' in w or 'Melinoe_Run' in w)
                   for w in warnings)


def test_check_conflicts_animation_missing_field():
    """animation_patch mod without target.animations → softer warning that it
    can't precisely detect overlap.  Surfaces only when 2+ patcher mods are
    present (single mod can't conflict with itself)."""
    from mod_info import check_conflicts

    a = _make_mod('ModA', 'Melinoe', mod_type='animation_patch')
    b = _anim_mod('ModB', 'Melinoe', ['Melinoe_Idle'])
    warnings, errors = check_conflicts([a, b])
    assert errors == []
    assert any("don't declare target.animations" in w for w in warnings)


def test_check_conflicts_animation_three_way_overlap():
    """Three mods, one shared animation across all three → still one warning
    that names all three mods."""
    from mod_info import check_conflicts

    group = [
        _anim_mod('ModA', 'Melinoe', ['Idle']),
        _anim_mod('ModB', 'Melinoe', ['Idle']),
        _anim_mod('ModC', 'Melinoe', ['Idle']),
    ]
    warnings, errors = check_conflicts(group)
    overlap_warnings = [w for w in warnings if 'animation' in w and 'Idle' in w]
    assert len(overlap_warnings) == 1
    for label in ('ModA', 'ModB', 'ModC'):
        assert label in overlap_warnings[0]


if __name__ == '__main__':
    sys.exit(_run_all())
