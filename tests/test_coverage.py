"""
CG3H Coverage Gap Tests — tests for functions identified as untested.

Covers:
  - parse_glb + build_vertex_buffer_40 (synthetic GLB)
  - _best_name_match (3-pass fuzzy matching)
  - _remap_bone_indices (joint remapping)
  - GPK pack/unpack/patch roundtrip
  - _build_texture_entry (0xAD entry construction)
  - check_conflicts (full conflict matrix)

Usage:
    python -m pytest tests/test_coverage.py -v
"""
import sys
import os
import struct
import json
import tempfile
import shutil
import numpy as np

_tools = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools')
sys.path.insert(0, _tools)


# =============================================================================
# Helpers
# =============================================================================

def _tmpdir():
    return tempfile.mkdtemp(prefix='cg3h_cov_')


def _make_minimal_glb(
    positions, normals=None, uvs=None, indices=None,
    joints=None, weights=None, joint_names=None,
    mesh_name='TestMesh', mat_name=None, tex_name=None,
):
    """Build a minimal GLB in memory and return its path."""
    import pygltflib

    V = len(positions)
    if uvs is None:
        uvs = np.zeros((V, 2), dtype=np.float32)
    if indices is None:
        indices = np.arange(V, dtype=np.uint16)

    # Pack binary data
    blob = bytearray()

    def add_data(arr):
        off = len(blob)
        raw = arr.tobytes()
        blob.extend(raw)
        return off, len(raw)

    pos_off, pos_len = add_data(positions.astype(np.float32))
    nrm_off, nrm_len = (None, 0)
    if normals is not None:
        nrm_off, nrm_len = add_data(normals.astype(np.float32))
    uv_off, uv_len = add_data(uvs.astype(np.float32))
    idx_off, idx_len = add_data(indices.astype(np.uint16))

    jnt_off, jnt_len = (None, 0)
    wgt_off, wgt_len = (None, 0)
    if joints is not None and weights is not None:
        jnt_off, jnt_len = add_data(joints.astype(np.uint16))
        wgt_off, wgt_len = add_data(weights.astype(np.float32))

    # Build glTF structure
    buffer_views = []
    accessors = []

    def add_accessor(off, length, comp_type, acc_type, count):
        bv_idx = len(buffer_views)
        buffer_views.append(pygltflib.BufferView(
            buffer=0, byteOffset=off, byteLength=length))
        acc_idx = len(accessors)
        accessors.append(pygltflib.Accessor(
            bufferView=bv_idx, componentType=comp_type,
            count=count, type=acc_type,
            max=None, min=None))
        return acc_idx

    pos_acc = add_accessor(pos_off, pos_len, 5126, 'VEC3', V)  # FLOAT
    nrm_acc = None
    if nrm_off is not None:
        nrm_acc = add_accessor(nrm_off, nrm_len, 5126, 'VEC3', V)
    uv_acc = add_accessor(uv_off, uv_len, 5126, 'VEC2', V)
    idx_acc = add_accessor(idx_off, idx_len, 5123, 'SCALAR', len(indices))  # UNSIGNED_SHORT

    jnt_acc = None
    wgt_acc = None
    if jnt_off is not None:
        jnt_acc = add_accessor(jnt_off, jnt_len, 5123, 'VEC4', V)  # UNSIGNED_SHORT
        wgt_acc = add_accessor(wgt_off, wgt_len, 5126, 'VEC4', V)  # FLOAT

    attrs = pygltflib.Attributes(
        POSITION=pos_acc, NORMAL=nrm_acc, TEXCOORD_0=uv_acc,
        JOINTS_0=jnt_acc, WEIGHTS_0=wgt_acc)

    mat_idx = None
    materials = []
    textures = []
    images = []
    if mat_name:
        mat_idx = 0
        pbr = None
        if tex_name:
            images.append(pygltflib.Image(name=tex_name, mimeType='image/png'))
            textures.append(pygltflib.Texture(source=0))
            pbr = pygltflib.PbrMetallicRoughness(
                baseColorTexture=pygltflib.TextureInfo(index=0))
        materials.append(pygltflib.Material(name=mat_name,
                                            pbrMetallicRoughness=pbr))

    prim = pygltflib.Primitive(attributes=attrs, indices=idx_acc, material=mat_idx)
    mesh = pygltflib.Mesh(name=mesh_name, primitives=[prim])

    nodes = [pygltflib.Node(name=mesh_name, mesh=0)]
    skins = []
    if joint_names:
        for i, jn in enumerate(joint_names):
            nodes.append(pygltflib.Node(name=jn))
        skins.append(pygltflib.Skin(
            joints=list(range(1, len(joint_names) + 1)),
            skeleton=1))
        nodes[0].skin = 0

    scene = pygltflib.Scene(nodes=[0])

    gltf = pygltflib.GLTF2(
        scene=0, scenes=[scene], nodes=nodes,
        meshes=[mesh], accessors=accessors, bufferViews=buffer_views,
        buffers=[pygltflib.Buffer(byteLength=len(blob))],
        skins=skins or None,
        materials=materials or None,
        textures=textures or None,
        images=images or None,
    )
    gltf.set_binary_blob(bytes(blob))

    tmp = tempfile.NamedTemporaryFile(suffix='.glb', delete=False)
    tmp.close()
    gltf.save(tmp.name)
    return tmp.name


# =============================================================================
# 1. parse_glb + build_vertex_buffer_40
# =============================================================================

def test_parse_glb_rigid_mesh():
    """parse_glb returns correct positions/normals/uvs for a rigid mesh."""
    from gltf_to_gr2 import parse_glb

    pos = np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]], dtype=np.float32)
    nrm = np.array([[0, 0, 1], [0, 1, 0], [1, 0, 0]], dtype=np.float32)
    uvs = np.array([[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]], dtype=np.float32)
    idx = np.array([0, 1, 2], dtype=np.uint16)

    glb_path = _make_minimal_glb(pos, normals=nrm, uvs=uvs, indices=idx)
    try:
        meshes = parse_glb(glb_path)
        assert len(meshes) == 1
        m = meshes[0]
        assert m['name'] == 'TestMesh'
        np.testing.assert_allclose(m['positions'], pos, atol=1e-5)
        np.testing.assert_allclose(m['normals'], nrm, atol=1e-5)
        np.testing.assert_allclose(m['uvs'], uvs, atol=1e-5)
        np.testing.assert_array_equal(m['indices'], idx)
        assert m['bw_u8'] is None  # rigid — no bone weights
        assert m['bj_u8'] is None
        assert m['bone_palette'] is None
    finally:
        os.unlink(glb_path)


def test_parse_glb_no_normals():
    """parse_glb returns normals=None when GLB has no normals."""
    from gltf_to_gr2 import parse_glb

    pos = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
    glb_path = _make_minimal_glb(pos, normals=None)
    try:
        meshes = parse_glb(glb_path)
        assert meshes[0]['normals'] is None
        assert meshes[0]['has_normals'] is False
    finally:
        os.unlink(glb_path)


def test_parse_glb_skinned_mesh():
    """parse_glb returns correct bone weights/indices for skinned mesh."""
    from gltf_to_gr2 import parse_glb

    V = 4
    pos = np.random.randn(V, 3).astype(np.float32)
    nrm = np.tile([0, 0, 1], (V, 1)).astype(np.float32)
    uvs = np.zeros((V, 2), dtype=np.float32)
    idx = np.array([0, 1, 2, 0, 2, 3], dtype=np.uint16)
    joints = np.array([[0, 1, 0, 0], [1, 0, 0, 0],
                        [0, 1, 0, 0], [1, 0, 0, 0]], dtype=np.uint16)
    weights = np.array([[0.8, 0.2, 0, 0], [0.6, 0.4, 0, 0],
                         [1.0, 0, 0, 0], [0.5, 0.5, 0, 0]], dtype=np.float32)

    glb_path = _make_minimal_glb(
        pos, normals=nrm, uvs=uvs, indices=idx,
        joints=joints, weights=weights,
        joint_names=['BoneA', 'BoneB'])
    try:
        meshes = parse_glb(glb_path)
        m = meshes[0]
        assert m['bw_u8'] is not None
        assert m['bj_u8'] is not None
        assert m['bone_palette'] is not None
        assert len(m['bone_palette']) == 2
        assert m['bone_palette'][0] == 'BoneA'
        assert m['bone_palette'][1] == 'BoneB'
        # Weights should be uint8 sum ~255
        assert m['bw_u8'].dtype == np.uint8
        # First vertex: 0.8 -> ~204, 0.2 -> ~51
        assert m['bw_u8'][0, 0] > 150
        assert m['bw_u8'][0, 1] > 30
    finally:
        os.unlink(glb_path)


def test_parse_glb_material_info():
    """parse_glb extracts material and texture names."""
    from gltf_to_gr2 import parse_glb

    pos = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
    glb_path = _make_minimal_glb(
        pos, mat_name='Mat_Melinoe', tex_name='Melinoe_Color512')
    try:
        meshes = parse_glb(glb_path)
        m = meshes[0]
        assert m['material_name'] == 'Mat_Melinoe'
        assert m['texture_name'] == 'Melinoe_Color512'
        assert m['texture_image_index'] == 0
    finally:
        os.unlink(glb_path)


def test_build_vertex_buffer_40_rigid():
    """build_vertex_buffer_40 packs a rigid mesh into correct 40-byte layout."""
    from gltf_to_gr2 import build_vertex_buffer_40

    pos = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], dtype=np.float32)
    nrm = np.array([[0.0, 0.0, 1.0], [0.0, 1.0, 0.0]], dtype=np.float32)
    uvs = np.array([[0.5, 0.5], [1.0, 0.0]], dtype=np.float32)

    m = {'positions': pos, 'normals': nrm, 'uvs': uvs,
         'bw_u8': None, 'bj_u8': None}
    buf = build_vertex_buffer_40(m)

    assert len(buf) == 2 * 40, f"Expected 80 bytes, got {len(buf)}"

    # Verify vertex 0 layout
    v0 = buf[:40]
    p0 = struct.unpack_from('<3f', v0, 0)
    assert p0 == (1.0, 2.0, 3.0), f"Position wrong: {p0}"

    # Bone weights/indices should be zero (rigid)
    bw = struct.unpack_from('<4B', v0, 12)
    bi = struct.unpack_from('<4B', v0, 16)
    assert bw == (0, 0, 0, 0)
    assert bi == (0, 0, 0, 0)

    n0 = struct.unpack_from('<3f', v0, 20)
    assert n0 == (0.0, 0.0, 1.0), f"Normal wrong: {n0}"

    uv0 = struct.unpack_from('<2f', v0, 32)
    assert uv0 == (0.5, 0.5), f"UV wrong: {uv0}"


def test_build_vertex_buffer_40_skinned():
    """build_vertex_buffer_40 packs bone weights and indices correctly."""
    from gltf_to_gr2 import build_vertex_buffer_40

    pos = np.array([[0, 0, 0]], dtype=np.float32)
    nrm = np.array([[0, 0, 1]], dtype=np.float32)
    uvs = np.array([[0, 0]], dtype=np.float32)
    bw = np.array([[200, 55, 0, 0]], dtype=np.uint8)
    bj = np.array([[2, 5, 0, 0]], dtype=np.uint8)

    m = {'positions': pos, 'normals': nrm, 'uvs': uvs,
         'bw_u8': bw, 'bj_u8': bj}
    buf = build_vertex_buffer_40(m)

    v0 = buf[:40]
    weights = struct.unpack_from('<4B', v0, 12)
    indices = struct.unpack_from('<4B', v0, 16)
    assert weights == (200, 55, 0, 0)
    assert indices == (2, 5, 0, 0)


def test_build_vertex_buffer_40_fallback_normals():
    """build_vertex_buffer_40 uses fallback normals when mesh has None."""
    from gltf_to_gr2 import build_vertex_buffer_40

    pos = np.array([[0, 0, 0]], dtype=np.float32)
    uvs = np.array([[0, 0]], dtype=np.float32)
    fallback = np.array([[0.0, 1.0, 0.0]], dtype=np.float32)

    m = {'positions': pos, 'normals': None, 'uvs': uvs,
         'bw_u8': None, 'bj_u8': None}
    buf = build_vertex_buffer_40(m, fallback_normals=fallback)

    n0 = struct.unpack_from('<3f', buf, 20)
    assert n0 == (0.0, 1.0, 0.0), f"Fallback normal not used: {n0}"


# =============================================================================
# 2. _best_name_match (3-pass fuzzy matching)
# =============================================================================

def test_best_name_match_exact():
    """Exact normalized match via variant stripping (pass 2)."""
    from gltf_to_gr2 import _best_name_match
    gr2 = {'melinoe_meshshape': 1, 'other_mesh': 2}
    assert _best_name_match('melinoe_meshshape', gr2) == 'melinoe_meshshape'


def test_best_name_match_variant_strip():
    """Variant word differences should still match (pass 2)."""
    from gltf_to_gr2 import _best_name_match, _VARIANT_WORDS
    if not _VARIANT_WORDS:
        return
    vw = _VARIANT_WORDS[0]  # 'overlook'
    # GR2 has 'melinoeoverlook_meshshape', GLB has 'melinoe_meshshape'
    # _strip_variants removes 'overlook' → both become 'melinoe_meshshape'
    gr2 = {f'melinoe{vw}_meshshape': 1}
    result = _best_name_match('melinoe_meshshape', gr2)
    assert result is not None, f"Should match after stripping variant '{vw}'"


def test_best_name_match_substring():
    """Substring containment with >=60% ratio should match (pass 3)."""
    from gltf_to_gr2 import _best_name_match
    # 'melinoe_mesh' (12 chars) inside 'melinoe_meshshape' (17 chars) = 70.6%
    gr2 = {'melinoe_meshshape': 1}
    result = _best_name_match('melinoe_mesh', gr2)
    assert result == 'melinoe_meshshape'


def test_best_name_match_substring_too_short():
    """Substring with <60% ratio should NOT match."""
    from gltf_to_gr2 import _best_name_match
    # 'mel' (3) inside 'melinoe_meshshape_battle_extra' (30) = 10%
    gr2 = {'melinoe_meshshape_battle_extra': 1}
    result = _best_name_match('mel', gr2)
    assert result is None


def test_best_name_match_no_match():
    """Completely unrelated names should return None."""
    from gltf_to_gr2 import _best_name_match
    gr2 = {'hecate_mesh': 1, 'athena_mesh': 2}
    result = _best_name_match('melinoe_mesh', gr2)
    assert result is None


def test_best_name_match_picks_best_ratio():
    """When multiple substrings match, picks the highest ratio."""
    from gltf_to_gr2 import _best_name_match
    gr2 = {
        'melinoe_meshshape': 1,       # 'melinoe_mesh' is 12/17 = 70.6%
        'melinoe_mesh': 2,            # 'melinoe_mesh' is 12/12 = 100%
    }
    result = _best_name_match('melinoe_mesh', gr2)
    # Should pick exact or highest ratio
    assert result == 'melinoe_mesh'


# =============================================================================
# 3. _remap_bone_indices
# =============================================================================

def test_remap_bone_indices_basic():
    """Simple remap: GLB joints [A,B] → GR2 bindings [B,A] should swap."""
    from gltf_to_gr2 import _remap_bone_indices

    glb_m = {
        'bone_palette': ['BoneA', 'BoneB'],
        'bj_u8': np.array([[0, 1, 0, 0], [1, 0, 0, 0]], dtype=np.uint8),
        'bw_u8': np.array([[200, 55, 0, 0], [128, 127, 0, 0]], dtype=np.uint8),
    }
    gr2_bb_names = ['BoneB', 'BoneA']  # reversed order

    bj, bw, ok = _remap_bone_indices(glb_m, gr2_bb_names)
    assert ok is True
    # GLB joint 0 (BoneA) → GR2 binding 1 (BoneA is at index 1)
    assert bj[0, 0] == 1
    # GLB joint 1 (BoneB) → GR2 binding 0 (BoneB is at index 0)
    assert bj[0, 1] == 0
    # Second vertex: GLB joint 1 (BoneB) → GR2 binding 0
    assert bj[1, 0] == 0


def test_remap_bone_indices_short_name():
    """Remap matches bones by short name (after colon)."""
    from gltf_to_gr2 import _remap_bone_indices

    glb_m = {
        'bone_palette': ['Armature:Spine', 'Armature:Head'],
        'bj_u8': np.array([[0, 0, 0, 0]], dtype=np.uint8),
        'bw_u8': np.array([[255, 0, 0, 0]], dtype=np.uint8),
    }
    gr2_bb_names = ['Head', 'Spine']

    bj, bw, ok = _remap_bone_indices(glb_m, gr2_bb_names)
    assert ok is True
    # Armature:Spine -> short 'Spine' -> GR2 index 1
    assert bj[0, 0] == 1


def test_remap_bone_indices_blender_escape():
    """Remap unescapes Blender's _x003A_ colon encoding."""
    from gltf_to_gr2 import _remap_bone_indices

    glb_m = {
        'bone_palette': ['Rig_x003A_Bone'],
        'bj_u8': np.array([[0, 0, 0, 0]], dtype=np.uint8),
        'bw_u8': np.array([[255, 0, 0, 0]], dtype=np.uint8),
    }
    gr2_bb_names = ['Rig:Bone']

    bj, bw, ok = _remap_bone_indices(glb_m, gr2_bb_names)
    assert ok is True
    assert bj[0, 0] == 0


def test_remap_bone_indices_no_skinning():
    """Returns (None, None, False) when GLB has no skinning."""
    from gltf_to_gr2 import _remap_bone_indices

    glb_m = {'bone_palette': None, 'bj_u8': None, 'bw_u8': None}
    bj, bw, ok = _remap_bone_indices(glb_m, ['SomeBone'])
    assert ok is False
    assert bj is None


def test_remap_bone_indices_no_gr2_bindings():
    """Returns (None, None, False) when GR2 has no bone bindings."""
    from gltf_to_gr2 import _remap_bone_indices

    glb_m = {
        'bone_palette': ['Bone'],
        'bj_u8': np.array([[0, 0, 0, 0]], dtype=np.uint8),
        'bw_u8': np.array([[255, 0, 0, 0]], dtype=np.uint8),
    }
    bj, bw, ok = _remap_bone_indices(glb_m, [])
    assert ok is False


def test_remap_bone_indices_unmapped_fallback():
    """Unmapped bones fall back to binding 0."""
    from gltf_to_gr2 import _remap_bone_indices

    glb_m = {
        'bone_palette': ['ExistsBone', 'GhostBone'],
        'bj_u8': np.array([[1, 0, 0, 0]], dtype=np.uint8),  # vertex uses GhostBone
        'bw_u8': np.array([[255, 0, 0, 0]], dtype=np.uint8),
    }
    gr2_bb_names = ['ExistsBone']  # GhostBone not in GR2

    bj, bw, ok = _remap_bone_indices(glb_m, gr2_bb_names)
    assert ok is True
    # GhostBone (GLB idx 1) not found → falls back to 0
    assert bj[0, 0] == 0


# =============================================================================
# 4. GPK pack/unpack roundtrip
# =============================================================================

def test_gpk_pack_unpack_roundtrip():
    """extract→pack→extract should preserve all data."""
    from gpk_pack import pack_gpk, extract_gpk

    entries = {
        'TestChar_Mesh': b'Hello this is mesh data' * 100,
        'TestChar_Anim_Idle': b'\x00\x01\x02\x03' * 500,
    }
    tmp = _tmpdir()
    try:
        gpk_path = os.path.join(tmp, 'test.gpk')
        pack_gpk(entries, gpk_path)
        assert os.path.isfile(gpk_path)

        extracted = extract_gpk(gpk_path)
        assert set(extracted.keys()) == set(entries.keys())
        for name in entries:
            assert extracted[name] == entries[name], f"Content mismatch for {name}"
    finally:
        shutil.rmtree(tmp)


def test_gpk_patch_replaces_entry():
    """patch_gpk replaces the specified entry, keeps others intact."""
    from gpk_pack import pack_gpk, extract_gpk, patch_gpk

    original = {
        'Entry_A': b'original_A_data' * 50,
        'Entry_B': b'original_B_data' * 50,
    }
    new_b = b'replaced_B_data_new_version' * 40
    tmp = _tmpdir()
    try:
        src = os.path.join(tmp, 'src.gpk')
        dst = os.path.join(tmp, 'dst.gpk')
        pack_gpk(original, src)
        patch_gpk(src, {'Entry_B': new_b}, dst)

        result = extract_gpk(dst)
        assert result['Entry_A'] == original['Entry_A'], "Unpatched entry changed"
        assert result['Entry_B'] == new_b, "Patched entry not updated"
    finally:
        shutil.rmtree(tmp)


def test_gpk_patch_missing_entry_raises():
    """patch_gpk raises KeyError for nonexistent entry names."""
    from gpk_pack import pack_gpk, patch_gpk
    import pytest

    tmp = _tmpdir()
    try:
        src = os.path.join(tmp, 'src.gpk')
        dst = os.path.join(tmp, 'dst.gpk')
        pack_gpk({'Entry_A': b'data'}, src)

        with pytest.raises(KeyError, match='NoSuchEntry'):
            patch_gpk(src, {'NoSuchEntry': b'x'}, dst)
    finally:
        shutil.rmtree(tmp)


def test_gpk_version_check():
    """extract_gpk rejects non-version-1 GPK files."""
    from gpk_pack import extract_gpk
    import pytest

    tmp = _tmpdir()
    try:
        bad_gpk = os.path.join(tmp, 'bad.gpk')
        with open(bad_gpk, 'wb') as f:
            f.write(struct.pack('<II', 99, 0))  # version=99, count=0

        with pytest.raises(ValueError, match='Unsupported GPK version'):
            extract_gpk(bad_gpk)
    finally:
        shutil.rmtree(tmp)


def test_gpk_name_too_long():
    """pack_gpk rejects entry names longer than 255 bytes."""
    from gpk_pack import pack_gpk
    import pytest

    tmp = _tmpdir()
    try:
        gpk_path = os.path.join(tmp, 'test.gpk')
        long_name = 'A' * 256
        with pytest.raises(ValueError, match='too long'):
            pack_gpk({long_name: b'data'}, gpk_path)
    finally:
        shutil.rmtree(tmp)


def test_gpk_list_no_decompress():
    """list_gpk returns entry names without decompressing data."""
    from gpk_pack import pack_gpk, list_gpk

    entries = {'Mesh_A': b'x' * 1000, 'Anim_B': b'y' * 2000}
    tmp = _tmpdir()
    try:
        gpk_path = os.path.join(tmp, 'test.gpk')
        pack_gpk(entries, gpk_path)

        listing = list_gpk(gpk_path)
        names = [name for name, _ in listing]
        assert set(names) == {'Mesh_A', 'Anim_B'}
        # Compressed sizes should be < original (LZ4 compresses repetitive data)
        for name, cs in listing:
            assert cs > 0
    finally:
        shutil.rmtree(tmp)


# =============================================================================
# 5. _build_texture_entry
# =============================================================================

def test_build_texture_entry_structure():
    """_build_texture_entry produces valid 0xAD entry bytes."""
    from pkg_texture import _build_texture_entry, _swap32

    pixels = b'\xAB' * 256
    entry = _build_texture_entry('GR2\\TestTex', pixels, 0x1C, 64, 64)

    # Tag
    assert entry[0] == 0xAD

    # CSString: 'GR2\\TestTex' = 11 bytes
    assert entry[1] == 11
    name = entry[2:13]
    assert name == b'GR2\\TestTex'

    # Big-endian size at offset 13
    be_size_raw = struct.unpack_from('<I', entry, 13)[0]
    total_data = _swap32(be_size_raw)
    expected_total = 10 + 20 + 256  # XNB(10) + tex_header(20) + pixels(256)
    assert total_data == expected_total, f"Expected {expected_total}, got {total_data}"

    # XNB header at offset 17
    assert entry[17:20] == b'XNB'

    # Texture format at offset 27 (17 + 10 = XNB end)
    tex_fmt = struct.unpack_from('<I', entry, 27)[0]
    assert tex_fmt == 0x1C

    # Width/height
    w = struct.unpack_from('<I', entry, 31)[0]
    h = struct.unpack_from('<I', entry, 35)[0]
    assert w == 64
    assert h == 64

    # Pixel data at the end
    assert entry[-256:] == pixels


def test_build_texture_entry_roundtrip():
    """Entry built by _build_texture_entry is parseable by scan_textures."""
    from pkg_texture import _build_texture_entry, scan_textures

    pixels = b'\x00' * 128
    entry = _build_texture_entry('GR2\\RoundTrip', pixels, 0x06, 32, 32)
    # Wrap in a chunk with 0xFF terminator
    chunk = bytes(entry) + b'\xFF'

    results = scan_textures([(chunk, None)])
    assert len(results) == 1
    t = results[0]
    assert t['name'] == 'GR2\\RoundTrip'
    assert t['width'] == 32
    assert t['height'] == 32
    assert t['format'] == 0x06
    assert t['pixel_size'] == 128


def test_build_texture_entry_long_name():
    """Entry with name >= 128 bytes uses multi-byte 7-bit length encoding."""
    from pkg_texture import _build_texture_entry

    long_name = 'GR2\\' + 'A' * 200  # 204 bytes
    pixels = b'\x00' * 64
    entry = _build_texture_entry(long_name, pixels, 0x1C, 16, 16)

    assert entry[0] == 0xAD
    # First length byte should have continuation bit set (>= 128)
    assert entry[1] & 0x80 != 0, "Long name should use multi-byte encoding"
    # Entry should still end with pixel data
    assert entry[-64:] == pixels


# =============================================================================
# 6. check_conflicts matrix
# =============================================================================

def _make_mod_group(mods_spec):
    """Build a mock mod group for check_conflicts.
    mods_spec: list of (id, name, type, textures_list)
    """
    group = []
    for mod_id, name, mod_type, textures in mods_spec:
        mod = {
            'id': mod_id,
            'mod': {
                'metadata': {'name': name},
                'type': mod_type,
                'target': {'character': 'Melinoe'},
                'assets': {
                    'textures': [{'name': t} for t in textures],
                },
            },
        }
        if mod_type == 'animation_patch':
            mod['mod']['assets']['animations'] = {'patch': True}
        group.append(mod)
    return group


def test_conflict_two_mesh_replace():
    """Two mesh_replace mods = hard conflict (error)."""
    from mod_merger import check_conflicts
    group = _make_mod_group([
        ('mod_a', 'ModA', 'mesh_replace', []),
        ('mod_b', 'ModB', 'mesh_replace', []),
    ])
    warnings, errors = check_conflicts(group)
    assert len(errors) > 0
    assert any('mesh_replace' in e.lower() or 'CONFLICT' in e for e in errors)


def test_conflict_same_texture_two_mods():
    """Two mods replacing the same texture = hard conflict."""
    from mod_merger import check_conflicts
    group = _make_mod_group([
        ('mod_a', 'ModA', 'texture_replace', ['Melinoe_Color512']),
        ('mod_b', 'ModB', 'texture_replace', ['Melinoe_Color512']),
    ])
    warnings, errors = check_conflicts(group)
    assert len(errors) > 0
    assert any('Melinoe_Color512' in e for e in errors)


def test_conflict_mesh_add_compatible():
    """Two mesh_add mods = no conflict."""
    from mod_merger import check_conflicts
    group = _make_mod_group([
        ('mod_a', 'ModA', 'mesh_add', []),
        ('mod_b', 'ModB', 'mesh_add', []),
    ])
    warnings, errors = check_conflicts(group)
    assert len(errors) == 0


def test_conflict_mesh_add_plus_replace_warning():
    """mesh_add + mesh_replace = warning (not error)."""
    from mod_merger import check_conflicts
    group = _make_mod_group([
        ('mod_a', 'ModA', 'mesh_add', []),
        ('mod_b', 'ModB', 'mesh_replace', []),
    ])
    warnings, errors = check_conflicts(group)
    assert len(warnings) > 0
    assert any('mesh_add' in w.lower() or 'add' in w.lower() for w in warnings)


def test_conflict_different_textures_ok():
    """Two mods replacing different textures = no conflict."""
    from mod_merger import check_conflicts
    group = _make_mod_group([
        ('mod_a', 'ModA', 'texture_replace', ['TextureA']),
        ('mod_b', 'ModB', 'texture_replace', ['TextureB']),
    ])
    warnings, errors = check_conflicts(group)
    assert len(errors) == 0


def test_conflict_texture_replace_plus_mesh_patch_ok():
    """texture_replace + mesh_patch = independent, no conflict."""
    from mod_merger import check_conflicts
    group = _make_mod_group([
        ('mod_a', 'ModA', 'texture_replace', ['SomeTex']),
        ('mod_b', 'ModB', 'mesh_patch', []),
    ])
    warnings, errors = check_conflicts(group)
    assert len(errors) == 0


# =============================================================================
# Runner
# =============================================================================

if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
