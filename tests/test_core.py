"""
Test suite for CG3H core functionality.
Runs without the game DLL or .pkg files — uses synthetic data.

Usage:
    python -m pytest tests/test_core.py -v
    python tests/test_core.py              # standalone
"""
import sys, os, struct, json, tempfile, shutil
import numpy as np

# Add tools/ to path
_tools = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools')
sys.path.insert(0, _tools)


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

def test_normalize_strips_lod():
    """Legacy _LOD suffixes should be stripped."""
    from gltf_to_gr2 import _normalize_mesh_name
    assert _normalize_mesh_name('Melinoe_MeshShape_LOD1') == 'melinoe_meshshape'


def test_normalize_strips_numeric():
    """New _1, _2 suffixes should be stripped."""
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
        idx_path = save_texture_index(tmpdir, os.path.join(tmpdir, '_texture_index.json'))

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
    """Run all test functions and report results."""
    import traceback
    tests = [(name, obj) for name, obj in globals().items()
             if name.startswith('test_') and callable(obj)]
    passed = failed = 0
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


if __name__ == '__main__':
    sys.exit(_run_all())
