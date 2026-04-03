"""
Hades II GR2 to glTF exporter.
Reads a .gpk + .sdb pair using the game's granny2_x64.dll and exports
a fully-skinned glTF 2.0 file with meshes, skeleton, and weights.

Usage:
    python gr2_to_gltf.py <name>
    e.g. python gr2_to_gltf.py Melinoe

Expects:
    - granny2_x64.dll in cwd (run from the Ship/ directory)
    - ../Content/GR2/_Optimized/<name>.gpk and .sdb
"""
import struct, ctypes, argparse, os, sys
import numpy as np
import lz4.block
import pygltflib
from pygltflib import (GLTF2, Scene, Node, Mesh, Primitive, Skin, Accessor, BufferView, Buffer,
                       Animation, AnimationSampler, AnimationChannel, AnimationChannelTarget)
from pygltflib import FLOAT, UNSIGNED_SHORT, UNSIGNED_BYTE, VEC3, VEC4, SCALAR, MAT4, VEC2
from pygltflib import ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER

# granny_types must be on sys.path — works when run from tools/ or when tools/ is in path
_tools_dir = os.path.dirname(os.path.abspath(__file__))
if _tools_dir not in sys.path:
    sys.path.insert(0, _tools_dir)
from granny_types import (
    build_type_map, get_struct_stride, get_vertex_layout,
    get_transform_field_offsets, setup_dll_types,
    MTYPE_UINT8, MTYPE_NORMAL_U8, MTYPE_REAL32, MTYPE_REAL16,
)

# Module-level state populated by setup_granny().
_TYPES: dict = {}
_BONE_STRIDE: int = 0
_BB_STRIDE: int = 0
_TRANSFORM_OFFS: dict = {}   # offsets within granny_transform: flags/translation/orientation

# ─────────────────────────── GPK extraction ──────────────────────────────────

def extract_all_from_gpk(gpk_path):
    """Returns dict of {name: decompressed_bytes}."""
    with open(gpk_path, 'rb') as f:
        raw = f.read()
    version = struct.unpack_from('<I', raw, 0)[0]
    if version != 1:
        raise ValueError(f"Unsupported GPK version {version}")
    count = struct.unpack_from('<I', raw, 4)[0]
    off = 8
    result = {}
    for _ in range(count):
        nl = raw[off]; name = raw[off+1:off+1+nl].decode('utf-8')
        off += 1 + nl
        cs = struct.unpack_from('<I', raw, off)[0]; off += 4
        result[name] = lz4.block.decompress(raw[off:off+cs], uncompressed_size=32*1024*1024)
        off += cs
    return result

# ─────────────────────────── Granny DLL helpers ──────────────────────────────

def setup_granny(dll_path):
    global _TYPES, _BONE_STRIDE, _BB_STRIDE, _TRANSFORM_OFFS

    dll = ctypes.CDLL(dll_path)

    # Core file I/O
    dll.GrannyReadEntireFileFromMemory.restype  = ctypes.c_void_p
    dll.GrannyReadEntireFileFromMemory.argtypes = [ctypes.c_int, ctypes.c_void_p]
    dll.GrannyGetStringDatabase.restype  = ctypes.c_void_p
    dll.GrannyGetStringDatabase.argtypes = [ctypes.c_void_p]
    dll.GrannyRemapFileStrings.restype  = ctypes.c_bool
    dll.GrannyRemapFileStrings.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    dll.GrannyGetFileInfo.restype  = ctypes.c_void_p
    dll.GrannyGetFileInfo.argtypes = [ctypes.c_void_p]
    dll.GrannyFreeFile.restype  = None
    dll.GrannyFreeFile.argtypes = [ctypes.c_void_p]

    # Vertex stride query
    dll.GrannyGetTotalObjectSize.restype  = ctypes.c_int
    dll.GrannyGetTotalObjectSize.argtypes = [ctypes.c_void_p]

    # Index extraction — safe multi-format copy (16-bit or 32-bit source → requested size)
    # GrannyCopyMeshIndices(mesh*, bytes_per_index, dest_buf*) → void
    dll.GrannyGetMeshTriangleCount.restype  = ctypes.c_int
    dll.GrannyGetMeshTriangleCount.argtypes = [ctypes.c_void_p]
    dll.GrannyCopyMeshIndices.restype  = None
    dll.GrannyCopyMeshIndices.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]

    # Curve conversion for animation export
    dll.GrannyGetResultingDaK32fC32fCurveSize.restype  = ctypes.c_int
    dll.GrannyGetResultingDaK32fC32fCurveSize.argtypes = [ctypes.c_void_p]
    dll.GrannyCurveConvertToDaK32fC32f.restype  = ctypes.c_void_p
    dll.GrannyCurveConvertToDaK32fC32f.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    dll.GrannyFreeCurve.restype  = None
    dll.GrannyFreeCurve.argtypes = [ctypes.c_void_p]
    dll.GrannyCurveIsTypeDaK32fC32f.restype  = ctypes.c_bool
    dll.GrannyCurveIsTypeDaK32fC32f.argtypes = [ctypes.c_void_p]

    # granny_types needs these too
    setup_dll_types(dll)

    # ── Build dynamic type map (replaces all hardcoded struct offsets) ──
    print("  Building Granny type map …")
    _TYPES          = build_type_map(dll)          # raises RuntimeError if validation fails
    _BONE_STRIDE    = get_struct_stride(dll, 'GrannyBoneType')
    _BB_STRIDE      = get_struct_stride(dll, 'GrannyBoneBindingType')
    _TRANSFORM_OFFS = get_transform_field_offsets(dll)
    print(f"  Type map OK  (bone={_BONE_STRIDE}B, binding={_BB_STRIDE}B, "
          f"transform.translation=+0x{_TRANSFORM_OFFS['translation']:02X})")

    return dll


def load_gr2(dll, gr2_bytes, sdb_bytes):
    """Load and remap a GR2 file using the game's DLL. Returns (gr2_file, sdb_file, file_info, keep_alive)."""
    sdb_buf = ctypes.create_string_buffer(sdb_bytes)
    gr2_buf = ctypes.create_string_buffer(gr2_bytes)
    sdb_file = dll.GrannyReadEntireFileFromMemory(len(sdb_bytes), sdb_buf)
    if not sdb_file:
        raise RuntimeError("Failed to load SDB")
    str_db = dll.GrannyGetStringDatabase(sdb_file)
    if not str_db:
        raise RuntimeError("Failed to get string database")
    platform_tag = struct.unpack_from('<I', gr2_bytes, 0x14)[0]
    hdr_size     = struct.unpack_from('<I', gr2_bytes, 0x10)[0]
    print(f"  [gr2 header] magic={gr2_bytes[:4].hex()}  hdr_size={hdr_size}  platform_tag=0x{platform_tag:08X}")
    gr2_file = dll.GrannyReadEntireFileFromMemory(len(gr2_bytes), gr2_buf)
    if not gr2_file:
        raise RuntimeError("Failed to load GR2")
    if not dll.GrannyRemapFileStrings(gr2_file, str_db):
        # A re-serialized GR2 (Golden Path output) has strings embedded inline;
        # GrannyRemapFileStrings returns false because there are no CRC stubs to
        # resolve.  This is not an error — the fi is still valid and readable.
        print("  WARNING: GrannyRemapFileStrings returned false "
              "(file may have embedded strings — continuing)")
    fi = dll.GrannyGetFileInfo(gr2_file)
    if not fi:
        raise RuntimeError("GrannyGetFileInfo returned null")
    return gr2_file, sdb_file, fi, (sdb_buf, gr2_buf)

# ─────────────────────────── Struct reading helpers ──────────────────────────

_kernel32 = ctypes.windll.kernel32

def _valid_ptr(p):
    return isinstance(p, int) and 0x10000 <= p <= 0x7FFFFFFFFFFF

def _readable(ptr, size):
    if not _valid_ptr(ptr):
        return False
    return not _kernel32.IsBadReadPtr(ctypes.c_void_p(ptr), ctypes.c_size_t(size))

def rq(addr, off):
    return struct.unpack_from('<Q', (ctypes.c_uint8*8).from_address(addr+off), 0)[0]

def ri(addr, off):
    return struct.unpack_from('<i', (ctypes.c_uint8*4).from_address(addr+off), 0)[0]

def safe_bytes(addr, n):
    return bytes((ctypes.c_uint8*n).from_address(addr))

def read_cstr(ptr):
    if not _valid_ptr(ptr): return ''
    m = (ctypes.c_uint8*256).from_address(ptr)
    b = bytes(m)
    return b[:b.index(0)].decode('utf-8', 'replace') if 0 in b else ''

def _t(struct_name, field_name) -> int:
    """Look up a field's byte offset from the dynamic type map. Raises KeyError on miss."""
    try:
        return _TYPES[struct_name][field_name]
    except KeyError:
        raise KeyError(f"Field '{struct_name}.{field_name}' not found in type map. "
                       f"Known fields: {list(_TYPES.get(struct_name, {}).keys())}")

# ─────────────────────────── Skeleton reading ────────────────────────────────

def read_skeleton(fi):
    """Returns (bones_list, name_to_index_dict)."""
    # 'Skeletons' is ArrayOfReferencesMember: {count[4], ptr[8]} at _skels offset.
    _skels = _t('granny_file_info', 'Skeletons')
    skel_count = ri(fi, _skels)
    if skel_count == 0:
        return [], {}
    skels_arr = rq(fi, _skels + 4)
    skel_ptr  = rq(skels_arr, 0)

    # 'Bones' is ReferenceToArrayMember: {count[4], ptr[8]} at _bones offset.
    _bones = _t('granny_skeleton', 'Bones')
    bone_count = ri(skel_ptr, _bones)
    bones_arr  = rq(skel_ptr, _bones + 4)

    off_name      = _t('granny_bone', 'Name')
    off_parent    = _t('granny_bone', 'ParentIndex')
    # 'Transform' is TransformMember (inline granny_transform, 68 bytes).
    # Internal offsets come from GrannyTransformType walk — zero hardcoding.
    off_lt        = _t('granny_bone', 'Transform')
    off_trans     = off_lt + _TRANSFORM_OFFS['translation']
    off_rot       = off_lt + _TRANSFORM_OFFS['orientation']
    off_inv_world = _t('granny_bone', 'InverseWorldTransform')

    bones = []
    name_to_idx = {}
    for bi in range(bone_count):
        base = bones_arr + bi * _BONE_STRIDE
        name   = read_cstr(rq(base, off_name))
        parent = ri(base, off_parent)
        tx, ty, tz          = struct.unpack_from('<3f', safe_bytes(base + off_trans, 12), 0)
        rx, ry, rz, rw      = struct.unpack_from('<4f', safe_bytes(base + off_rot, 16), 0)
        # InverseWorld 4x4: Granny row-major and glTF column-major have identical memory
        # layout for TRS matrices — use raw bytes directly, no transpose needed.
        inv_world = struct.unpack_from('<16f', safe_bytes(base + off_inv_world, 64), 0)
        bones.append({
            'name':        name,
            'parent':      parent,
            'translation': (tx, ty, tz),
            'rotation':    (rx, ry, rz, rw),
            'inv_world':   inv_world,
        })
        name_to_idx[name] = bi
    return bones, name_to_idx

# ─────────────────────────── Mesh reading ────────────────────────────────────

def _read_skinning_weights(vp, vc, src_stride, vtx_layout):
    """
    Extract per-vertex blend weights and indices from the raw vertex buffer.

    Uses the vertex type definition (vtx_layout) to locate the fields
    by TYPE SIGNATURE — mtype == MTYPE_UINT8 and element_count == 4 —
    rather than by field name.  This is fully DLL-driven: no hardcoded
    names, no assumptions about naming conventions.

    In Granny's skinned vertex types, exactly two uint8×4 fields appear:
      [first]   bone weights  (sum ≈ 255 per vertex)
      [second]  bone indices  (local bone palette indices)

    Returns (bw_float32 [vc,4], bj_intp [vc,4]).
    Returns (None, None) if fewer than two uint8×4 fields exist (rigid mesh).
    """
    # Each entry: (name, byte_offset, total_bytes, mtype, element_count)
    # BoneWeights may be NormalUInt8 (14) or UInt8 (12); BoneIndices are always UInt8.
    # Accept both types — order in the layout is always weights-first, indices-second.
    u8x4 = [(off, name) for name, off, total, mtype, count in vtx_layout
             if mtype in (MTYPE_UINT8, MTYPE_NORMAL_U8) and count == 4]

    if len(u8x4) < 2:
        return None, None   # rigid mesh — no per-vertex skinning data

    bw_off = u8x4[0][0]   # first uint8×4  = bone weights
    bj_off = u8x4[1][0]   # second uint8×4 = bone indices

    raw = np.frombuffer(safe_bytes(vp, vc * src_stride), dtype=np.uint8).reshape(vc, src_stride)
    bw = raw[:, bw_off:bw_off + 4].astype(np.float32)
    bj = raw[:, bj_off:bj_off + 4].astype(np.intp)
    return bw, bj


def read_mesh_data(mesh_ptr, bone_name_to_idx, dll, debug=False):
    """
    Read vertex and index data from a granny_mesh pointer.

    Returns (mesh_name, positions, normals, uvs, weights, joints, indices)
    or None if the mesh should be skipped.

    weights/joints are None for rigid (non-skinned) meshes.
    """
    off_name = _t('granny_mesh', 'Name')
    # PrimaryVertexData is ReferenceMember (pointer, 8 bytes).
    off_vd   = _t('granny_mesh', 'PrimaryVertexData')
    # BoneBindings is ReferenceToArrayMember: {count[4], ptr[8]}.
    _bb      = _t('granny_mesh', 'BoneBindings')

    mesh_name = read_cstr(rq(mesh_ptr, off_name))
    if 'ShadowMesh' in mesh_name:
        print(f"  Mesh '{mesh_name}': skipped (shadow mesh)")
        return None

    vd = rq(mesh_ptr, off_vd)
    if not _valid_ptr(vd):
        print(f"  Mesh '{mesh_name}': skipped (null PrimaryVertexData)")
        return None

    # 'Vertices' is ReferenceToVariantArrayMember: {type*[8], count[4], ptr[8]}.
    _verts          = _t('granny_vertex_data', 'Vertices')
    vertex_type_ptr = rq(vd, _verts)
    vc              = ri(vd, _verts + 8)
    vp              = rq(vd, _verts + 12)

    if not _valid_ptr(vp) or vc <= 0:
        print(f"  Mesh '{mesh_name}': skipped (no vertex data: vc={vc}, vp=0x{vp:X})")
        return None

    # ── Vertex type introspection ─────────────────────────────────────────────
    vtx_layout = get_vertex_layout(dll, vertex_type_ptr)
    if vtx_layout is None:
        print(f"  WARNING: '{mesh_name}' has no readable vertex type definition — skipping")
        return None

    src_stride = dll.GrannyGetTotalObjectSize(ctypes.c_void_p(vertex_type_ptr))

    if src_stride <= 0:
        print(f"  WARNING: '{mesh_name}' reported vertex stride {src_stride} — skipping")
        return None

    # ── Bone palette ──────────────────────────────────────────────────────────
    bb_count = ri(mesh_ptr, _bb)         # count is at _bb
    bb_ptr   = rq(mesh_ptr, _bb + 4)     # ptr  is at _bb + 4

    off_bb_name = _t('granny_bone_binding', 'BoneName')

    palette = []
    debug_palette = []
    for bi in range(bb_count):
        entry    = bb_ptr + bi * _BB_STRIDE
        name_ptr = rq(entry, off_bb_name) if _readable(entry, 8) else 0
        bb_name  = read_cstr(name_ptr) if _readable(name_ptr, 4) else ''
        global_idx = bone_name_to_idx.get(bb_name, 0)
        palette.append(global_idx)
        debug_palette.append((bb_name, bone_name_to_idx.get(bb_name)))
    if not palette:
        palette = [0]

    if debug:
        print(f"  [debug] '{mesh_name}'  stride={src_stride}  bb_count={bb_count}")
        vfield_summary = ', '.join(f"{n}@+{o}" for n, o, *_ in vtx_layout)
        print(f"    vertex fields: {vfield_summary}")
        for bi, (bname, resolved) in enumerate(debug_palette):
            status = f"-> bone[{resolved}]" if resolved is not None else "NOT IN SKELETON (fallback 0)"
            print(f"    BB[{bi}] '{bname}' {status}")

    # ── Geometry extraction — read directly from raw vertex buffer ───────────
    # GrannyConvertVertexLayouts does field-name matching; if SDB string remapping
    # doesn't reach vertex type field names the names are null/wrong and all
    # converted fields come out as zero.  Reading by TYPE SIGNATURE avoids that.
    raw_verts = np.frombuffer(safe_bytes(vp, vc * src_stride), dtype=np.uint8).reshape(vc, src_stride)

    # Collect geometry fields by type, ordered by byte offset (vtx_layout is already ordered).
    # FieldEntry = (name, byte_offset, total_bytes, mtype, element_count)
    real32_3 = [e for e in vtx_layout if e[3] == MTYPE_REAL32 and e[4] == 3]
    real32_2 = [e for e in vtx_layout if e[3] == MTYPE_REAL32 and e[4] == 2]
    real16_2 = [e for e in vtx_layout if e[3] == MTYPE_REAL16 and e[4] == 2]

    if not real32_3:
        print(f"  WARNING: '{mesh_name}' has no float32x3 fields — cannot extract geometry, skipping")
        return None

    # --- ENGINE ARCHITECTURE: 40-BYTE PHYSICAL GPU STRIDE ---
    # The Hades II engine forces ALL vertices to a uniform 40-byte physical layout
    # regardless of what Granny's metadata reports.
    #
    # Rigid mesh (Granny metadata = 32 bytes):   Pos(12) | Padding(8) | Normal(12) | UV(8) = 40
    # Skinned mesh (Granny metadata = 40 bytes): Pos(12) | Weights(4) | Indices(4) | Normal(12) | UV(8) = 40
    #
    # Standard tools like LSLib read using the 32-byte Granny stride, desync after the first
    # vertex, and output corrupted "spiky" geometry on rigid meshes.
    is_stripped_mesh = (src_stride < 40)
    physical_stride = 40 if is_stripped_mesh else src_stride
    
    raw_verts = np.frombuffer(safe_bytes(vp, vc * physical_stride), dtype=np.uint8).reshape(vc, physical_stride)
    
    if is_stripped_mesh:
        # Force the hardcoded Forge Renderer physical offsets
        positions = np.frombuffer(raw_verts[:, 0:12].tobytes(), dtype='<f4').reshape(vc, 3).copy()
        normals   = np.frombuffer(raw_verts[:, 20:32].tobytes(), dtype='<f4').reshape(vc, 3).copy()
        uvs       = np.frombuffer(raw_verts[:, 32:40].tobytes(), dtype='<f4').reshape(vc, 2).copy()
    else:
        # Fully compliant Granny mesh (e.g. 40+ byte skinned meshes where Granny is honest)
        pos_off = real32_3[0][1]
        positions = np.frombuffer(raw_verts[:, pos_off:pos_off+12].tobytes(), dtype='<f4').reshape(vc, 3).copy()
        
        if len(real32_3) >= 2:
            nrm_off = real32_3[1][1]
            normals = np.frombuffer(raw_verts[:, nrm_off:nrm_off+12].tobytes(), dtype='<f4').reshape(vc, 3).copy()
        else:
            normals = np.zeros((vc, 3), dtype=np.float32)
            
        if real32_2:
            uv_off = real32_2[0][1]
            uvs = np.frombuffer(raw_verts[:, uv_off:uv_off+8].tobytes(), dtype='<f4').reshape(vc, 2).copy()
        elif real16_2:
            uv_off = real16_2[0][1]
            uvs = np.frombuffer(raw_verts[:, uv_off:uv_off+4].tobytes(), dtype='<f2').astype(np.float32).reshape(vc, 2).copy()
        else:
            uvs = np.zeros((vc, 2), dtype=np.float32)

    # Bulletproof sanitization: Neutralize any padding garbage so glTF doesn't crash on NaNs
    np.nan_to_num(positions, copy=False)
    np.nan_to_num(normals, copy=False)
    np.nan_to_num(uvs, copy=False)

    # ── Bone weights (skinned meshes only) ────────────────────────────────────
    raw_bw, raw_bj = _read_skinning_weights(vp, vc, src_stride, vtx_layout)

    if raw_bw is None:
        # Rigid mesh — no blend data in vertex format.
        # Positions are already in character root space; export without skinning.
        joints  = None
        weights = None
    else:
        # Remap local bone palette indices → global skeleton indices.
        # Unused bone influence slots (raw_bw == 0) may contain arbitrary/sentinel values
        # that exceed the palette size.  Clamp before the lookup so those slots don't crash;
        # they are zeroed out afterwards anyway.
        palette_np               = np.array(palette, dtype=np.uint16)
        raw_bj_safe              = np.clip(raw_bj, 0, len(palette) - 1)
        joints_global            = palette_np[raw_bj_safe]
        joints_global[raw_bw==0] = 0
        w_sum        = raw_bw.sum(axis=1, keepdims=True)
        weights_norm = (raw_bw / np.where(w_sum > 0, w_sum, 1.0)).astype(np.float32)
        joints  = joints_global.astype(np.uint16)
        weights = weights_norm

    # ── Indices — GrannyCopyMeshIndices with bytes_per_index=2 ────────────────
    # The engine always normalises to 16-bit indices for GPU uniformity.
    # GrannyCopyMeshIndices handles both 16-bit and 32-bit source index buffers,
    # matching the engine's own ForgeRenderer pipeline exactly.
    # Reading raw Indices16 memory would silently skip large meshes whose .gr2
    # stores 32-bit indices; this DLL call converts them safely.
    tri_count = dll.GrannyGetMeshTriangleCount(ctypes.c_void_p(mesh_ptr))
    if tri_count <= 0:
        print(f"  WARNING: '{mesh_name}' has no triangles — skipping")
        return None
    idx_buf = ctypes.create_string_buffer(tri_count * 3 * 2)  # 3 indices × 2 bytes per triangle
    dll.GrannyCopyMeshIndices(ctypes.c_void_p(mesh_ptr), 2, idx_buf)
    indices = np.frombuffer(bytes(idx_buf), dtype=np.uint16).copy()

    if len(indices) > 0:
        max_idx = np.max(indices)
        if max_idx < vc - 1:
            positions = positions[:max_idx + 1]
            normals = normals[:max_idx + 1]
            uvs = uvs[:max_idx + 1]
            if weights is not None:
                weights = weights[:max_idx + 1]
                joints = joints[:max_idx + 1]

    return mesh_name, positions, normals, uvs, weights, joints, indices

# ─────────────────────────── Animation extraction ───────────────────────────

TRACK_STRIDE = 60  # granny_transform_track: Name(8)+Flags(4)+3×Curve2(16) = 60


def _decode_curve_dak32f(data_ptr, expected_dim):
    """Decode a DaK32fC32f curve (format=1): float32 knots + float32 controls."""
    raw = safe_bytes(data_ptr, 28)
    deg = raw[1]
    kc = struct.unpack_from('<I', raw, 4)[0]
    kp = struct.unpack_from('<Q', raw, 8)[0]
    cc = struct.unpack_from('<I', raw, 16)[0]
    cp = struct.unpack_from('<Q', raw, 20)[0]
    if not _valid_ptr(kp) or not _valid_ptr(cp) or kc == 0:
        return None
    knots = np.frombuffer(safe_bytes(kp, kc * 4), dtype=np.float32).copy()
    ctrls = np.frombuffer(safe_bytes(cp, cc * 4), dtype=np.float32).copy()
    dim = cc // kc if kc > 0 else expected_dim
    values = ctrls.reshape(kc, dim) if dim > 0 else ctrls
    return knots, values, deg


def _decode_curve(dll, curve2_addr, data_ptr, expected_dim):
    """
    Decode a Granny curve. Simple formats are read directly in Python.
    Quantized formats are converted via GrannyCurveConvertToDaK32fC32f.
    Returns (knots, values, degree) or None if identity/empty.
    """
    if not _valid_ptr(data_ptr):
        return None
    fmt = safe_bytes(data_ptr, 1)[0]

    if fmt == 2:  # DaIdentity — no animation
        return None

    if fmt == 4:  # D3Constant32f
        vals = np.frombuffer(safe_bytes(data_ptr + 4, 12), dtype=np.float32).copy()
        return np.array([0.0], dtype=np.float32), vals.reshape(1, 3), 0

    if fmt == 5:  # D4Constant32f
        vals = np.frombuffer(safe_bytes(data_ptr + 4, 16), dtype=np.float32).copy()
        return np.array([0.0], dtype=np.float32), vals.reshape(1, 4), 0

    if fmt == 3:  # DaConstant32f (9 floats = 3x3 matrix)
        vals = np.frombuffer(safe_bytes(data_ptr + 4, 36), dtype=np.float32).copy()
        return np.array([0.0], dtype=np.float32), vals.reshape(1, 9), 0

    if fmt == 0:  # DaKeyframes32f — implicit integer knots
        dim = struct.unpack_from('<H', safe_bytes(data_ptr + 2, 2))[0]
        cc = struct.unpack_from('<I', safe_bytes(data_ptr + 4, 4))[0]
        cp = struct.unpack_from('<Q', safe_bytes(data_ptr + 8, 8))[0]
        if not _valid_ptr(cp) or cc == 0 or dim == 0:
            return None
        ctrls = np.frombuffer(safe_bytes(cp, cc * 4), dtype=np.float32).copy()
        n_knots = cc // dim
        knots = np.arange(n_knots, dtype=np.float32)
        return knots, ctrls.reshape(n_knots, dim), 0

    if fmt == 1:  # DaK32fC32f — already float32
        return _decode_curve_dak32f(data_ptr, expected_dim)

    # All other formats: convert via DLL
    try:
        sz = dll.GrannyGetResultingDaK32fC32fCurveSize(ctypes.c_void_p(curve2_addr))
        if sz <= 0:
            return None
        buf = (ctypes.c_uint8 * sz)()
        ret = dll.GrannyCurveConvertToDaK32fC32f(
            ctypes.c_void_p(curve2_addr), ctypes.cast(buf, ctypes.c_void_p))
        if not ret:
            return None
        conv_data = rq(ret, 8)
        result = _decode_curve_dak32f(conv_data, expected_dim) if _valid_ptr(conv_data) else None
        dll.GrannyFreeCurve(ctypes.c_void_p(ret))
        return result
    except Exception:
        return None


def _fixup_quaternion_signs(values):
    """
    Ensure consecutive quaternions have positive dot product so slerp/lerp
    takes the short path. Flips quaternions that would cause long-way-around
    interpolation. Modifies values in-place.
    """
    if values.shape[-1] != 4 or len(values) < 2:
        return
    flip = 1.0
    for i in range(len(values) - 1):
        q0 = values[i]
        q1 = values[i + 1] * flip
        dot = np.dot(q0, q1)
        if dot < 0.0:
            flip = -flip
        values[i + 1] *= flip


def _postprocess_track(track):
    """
    Apply LSLib-style post-processing to an animation track:
    1. Quaternion sign fixup (prevents slerp long-path jitter)
    2. Merge adjacent keyframes within 4ms
    3. Remove trivial keyframes that lie on the interpolation path
    """
    # Quaternion sign fixup on orientation curves
    if track['orient'] is not None:
        knots, values, deg = track['orient']
        if values.shape[-1] == 4 and len(knots) > 1:
            _fixup_quaternion_signs(values)

    # Merge adjacent frames (within 4ms) for each curve type
    for key in ('orient', 'pos', 'scale'):
        curve = track[key]
        if curve is None:
            continue
        knots, values, deg = curve
        if len(knots) < 2:
            continue
        # Find indices to keep (merge duplicates within 0.004s)
        keep = [0]
        for i in range(1, len(knots)):
            if knots[i] - knots[keep[-1]] >= 0.004:
                keep.append(i)
        if len(keep) < len(knots):
            track[key] = (knots[keep], values[keep], deg)

    # Remove trivial translation keyframes (on the linear interpolation path)
    if track['pos'] is not None:
        knots, values, deg = track['pos']
        if len(knots) > 2 and values.shape[-1] == 3:
            keep = [0]
            for i in range(1, len(knots) - 1):
                t0, t1, t2 = knots[keep[-1]], knots[i], knots[min(i+1, len(knots)-1)]
                if t2 == t0:
                    keep.append(i)
                    continue
                alpha = (t1 - t0) / (t2 - t0)
                lerped = values[keep[-1]] * (1 - alpha) + values[min(i+1, len(values)-1)] * alpha
                if np.linalg.norm(values[i] - lerped) >= 0.001:
                    keep.append(i)
            keep.append(len(knots) - 1)
            if len(keep) < len(knots):
                track['pos'] = (knots[keep], values[keep], deg)

    # Remove trivial rotation keyframes (on the slerp path)
    if track['orient'] is not None:
        knots, values, deg = track['orient']
        if len(knots) > 2 and values.shape[-1] == 4:
            all_trivial = True
            for i in range(1, len(knots) - 1):
                t0, t1, t2 = knots[0], knots[i], knots[-1]
                alpha = (t1 - t0) / (t2 - t0) if t2 != t0 else 0
                # Simple lerp approximation of slerp for small angles
                lerped = values[0] * (1 - alpha) + values[-1] * alpha
                lerped /= max(np.linalg.norm(lerped), 1e-10)
                if np.linalg.norm(values[i] - lerped) >= 0.001:
                    all_trivial = False
                    break
            if all_trivial and np.linalg.norm(values[0] - values[-1]) < 0.0001:
                # All intermediate are trivial and endpoints match — reduce to single
                track['orient'] = (knots[:1], values[:1], deg)


def extract_animations(dll, gpk_entries, sdb_bytes, anim_filter=None):
    """
    Extract animation data from all non-mesh GPK entries.
    Returns list of dicts with name, duration, and decoded track data.
    """
    animations = []
    sdb_buf = ctypes.create_string_buffer(sdb_bytes)
    sdb_file = dll.GrannyReadEntireFileFromMemory(len(sdb_bytes), sdb_buf)
    if not sdb_file:
        return animations
    str_db = dll.GrannyGetStringDatabase(sdb_file)

    anim_entries = {k: v for k, v in gpk_entries.items() if not k.endswith('_Mesh')}
    if anim_filter:
        pattern = anim_filter.lower()
        anim_entries = {k: v for k, v in anim_entries.items() if pattern in k.lower()}

    total = len(anim_entries)
    print(f"  Processing {total} animation entries" +
          (f" (filtered by '{anim_filter}')" if anim_filter else ""), flush=True)

    for idx, (entry_name, gr2_bytes) in enumerate(anim_entries.items()):
        gr2_buf = ctypes.create_string_buffer(gr2_bytes)
        gr2_file = dll.GrannyReadEntireFileFromMemory(len(gr2_bytes), gr2_buf)
        if not gr2_file:
            continue
        dll.GrannyRemapFileStrings(gr2_file, str_db)
        fi = dll.GrannyGetFileInfo(gr2_file)
        if not fi:
            dll.GrannyFreeFile(gr2_file)
            continue

        anim_count = ri(fi, 0x78)
        if anim_count == 0:
            dll.GrannyFreeFile(gr2_file)
            continue

        anim0 = rq(rq(fi, 0x7C), 0)
        anim_name = read_cstr(rq(anim0, 0))
        duration = struct.unpack_from('<f', safe_bytes(anim0 + 8, 4))[0]
        if duration <= 0:
            dll.GrannyFreeFile(gr2_file)
            continue

        tg_count = ri(fi, 0x6C)
        if tg_count == 0:
            dll.GrannyFreeFile(gr2_file)
            continue
        tg0 = rq(rq(fi, 0x70), 0)
        tt_count = ri(tg0, 0x14)
        tt_ptr = rq(tg0, 0x18)
        if tt_count == 0 or not _valid_ptr(tt_ptr):
            dll.GrannyFreeFile(gr2_file)
            continue

        tracks = []
        for ti in range(tt_count):
            t_base = tt_ptr + ti * TRACK_STRIDE
            t_name = read_cstr(rq(t_base, 0))

            orient = _decode_curve(dll, t_base + 0x0C, rq(t_base, 0x14), 4)
            pos    = _decode_curve(dll, t_base + 0x1C, rq(t_base, 0x24), 3)
            scale  = _decode_curve(dll, t_base + 0x2C, rq(t_base, 0x34), 9)

            if orient is None and pos is None and scale is None:
                continue
            track = {'name': t_name, 'orient': orient, 'pos': pos, 'scale': scale}
            _postprocess_track(track)
            # Skip if post-processing removed all data
            if track['orient'] is None and track['pos'] is None and track['scale'] is None:
                continue
            tracks.append(track)

        if tracks:
            animations.append({
                'name': entry_name,          # GPK entry name (stable, used for roundtrip matching)
                'granny_name': anim_name,    # internal Granny name (for reference)
                'duration': duration,
                'tracks': tracks,
            })

        dll.GrannyFreeFile(gr2_file)
        if (idx + 1) % 25 == 0 or idx + 1 == total:
            print(f"    {idx+1}/{total} processed ({len(animations)} animations)", flush=True)

    dll.GrannyFreeFile(sdb_file)
    print(f"  Extracted {len(animations)} animations")
    return animations


# ─────────────────────────── glTF export ─────────────────────────────────────

def build_gltf(character_name, mesh_data_list, mesh_names, bones, animations=None,
               texture_png_bytes=None):
    gltf = GLTF2()
    gltf.scene = 0
    gltf.scenes = [Scene(nodes=[])]
    gltf.asset = pygltflib.Asset(version="2.0", generator="Hades2 GR2 Exporter")

    all_buffers = []

    def add_accessor(data_np, component_type, element_type, target=None):
        raw = data_np.tobytes()
        bv_offset = sum(len(b) for b in all_buffers)
        pad = (4 - (len(raw) % 4)) % 4
        all_buffers.append(raw + b'\x00' * pad)
        bv_idx = len(gltf.bufferViews)
        gltf.bufferViews.append(BufferView(
            buffer=0, byteOffset=bv_offset, byteLength=len(raw), target=target,
        ))
        acc_idx = len(gltf.accessors)
        if element_type == MAT4:
            acc_min = acc_max = None
        elif data_np.ndim > 1:
            acc_min = np.nanmin(data_np, axis=0).tolist()
            acc_max = np.nanmax(data_np, axis=0).tolist()
        else:
            acc_min = [float(np.nanmin(data_np))]
            acc_max = [float(np.nanmax(data_np))]
        gltf.accessors.append(Accessor(
            bufferView=bv_idx, componentType=component_type,
            count=len(data_np), type=element_type, min=acc_min, max=acc_max,
        ))
        return acc_idx

    # ── Bones -> Nodes ──
    bone_node_indices = []
    root_nodes = []
    for bone in bones:
        tx, ty, tz = bone['translation']
        rx, ry, rz, rw = bone['rotation']
        node = Node(
            name=bone['name'],
            translation=[float(tx), float(ty), float(tz)],
            rotation=[float(rx), float(ry), float(rz), float(rw)],
            children=[],
        )
        gltf.nodes.append(node)
        bone_node_indices.append(len(gltf.nodes) - 1)

    for i, bone in enumerate(bones):
        if bone['parent'] >= 0:
            parent_idx = bone_node_indices[bone['parent']]
            if gltf.nodes[parent_idx].children is None:
                gltf.nodes[parent_idx].children = []
            gltf.nodes[parent_idx].children.append(bone_node_indices[i])
        else:
            root_nodes.append(bone_node_indices[i])

    inv_bind_np = np.array([b['inv_world'] for b in bones], dtype=np.float32).reshape(-1, 16)
    ibm_accessor = add_accessor(inv_bind_np, FLOAT, MAT4)

    skin = Skin(
        name=f"{character_name}_Skin",
        joints=bone_node_indices,
        inverseBindMatrices=ibm_accessor,
        skeleton=bone_node_indices[0] if bone_node_indices else None,
    )
    gltf.skins.append(skin)
    skin_idx = 0

    # ── Texture + Material ──
    mat_idx = None
    if texture_png_bytes is not None:
        # Embed PNG as a buffer view
        png_bv_offset = sum(len(b) for b in all_buffers)
        pad = (4 - (len(texture_png_bytes) % 4)) % 4
        all_buffers.append(texture_png_bytes + b'\x00' * pad)
        png_bv_idx = len(gltf.bufferViews)
        gltf.bufferViews.append(BufferView(
            buffer=0, byteOffset=png_bv_offset, byteLength=len(texture_png_bytes),
        ))

        img_idx = len(gltf.images) if hasattr(gltf, 'images') and gltf.images else 0
        gltf.images.append(pygltflib.Image(
            bufferView=png_bv_idx, mimeType="image/png", name=f"{character_name}_Color",
        ))

        tex_idx = len(gltf.textures)
        gltf.textures.append(pygltflib.Texture(source=img_idx))

        mat_idx = len(gltf.materials)
        gltf.materials.append(pygltflib.Material(
            name=f"Mat_{character_name}",
            pbrMetallicRoughness=pygltflib.PbrMetallicRoughness(
                baseColorTexture=pygltflib.TextureInfo(index=tex_idx),
                metallicFactor=0.0,
                roughnessFactor=1.0,
            ),
            doubleSided=True,
        ))

    # ── Mesh nodes ──
    for mesh_data, mesh_name in zip(mesh_data_list, mesh_names):
        positions, normals, uvs, weights, joints, indices = mesh_data
        skinned = weights is not None

        pos_acc = add_accessor(positions.astype(np.float32), FLOAT, VEC3, ARRAY_BUFFER)
        nrm_acc = add_accessor(normals.astype(np.float32), FLOAT, VEC3, ARRAY_BUFFER) if normals is not None else None
        uv_acc  = add_accessor(uvs.astype(np.float32), FLOAT, VEC2, ARRAY_BUFFER)
        idx_acc = add_accessor(indices.astype(np.uint16), UNSIGNED_SHORT, SCALAR, ELEMENT_ARRAY_BUFFER)

        attrs = pygltflib.Attributes(POSITION=pos_acc, NORMAL=nrm_acc, TEXCOORD_0=uv_acc)
        if skinned:
            attrs.JOINTS_0  = add_accessor(joints.astype(np.uint16), UNSIGNED_SHORT, VEC4, ARRAY_BUFFER)
            attrs.WEIGHTS_0 = add_accessor(weights.astype(np.float32), FLOAT, VEC4, ARRAY_BUFFER)

        # Assign material to non-outline, non-shadow meshes
        prim_mat = None
        if mat_idx is not None and 'Outline' not in mesh_name and 'Shadow' not in mesh_name:
            prim_mat = mat_idx

        gltf.meshes.append(Mesh(
            name=mesh_name,
            primitives=[Primitive(attributes=attrs, indices=idx_acc, material=prim_mat)],
        ))
        # Rigid meshes: positions are in character root space — no skin needed.
        # Skinned meshes: positions are in bone-local space — apply shared skin.
        gltf.nodes.append(Node(
            name=mesh_name,
            mesh=len(gltf.meshes) - 1,
            skin=skin_idx if skinned else None,
        ))
        gltf.scenes[0].nodes.append(len(gltf.nodes) - 1)

    for rn in root_nodes:
        gltf.scenes[0].nodes.append(rn)

    # ── Animations ──
    if animations:
        # Build bone name -> node index mapping for animation channel targeting
        bone_name_to_node = {}
        for i, bone in enumerate(bones):
            bone_name_to_node[bone['name']] = bone_node_indices[i]
            # Also map short name (after last colon)
            short = bone['name'].split(':')[-1]
            if short not in bone_name_to_node:
                bone_name_to_node[short] = bone_node_indices[i]

        total_anims = len(animations)
        print(f"  Building {total_anims} animation(s) into glTF...", flush=True)
        for anim_idx, anim_data in enumerate(animations):
            # Compute content hash from all keyframe data for diff-on-import
            import hashlib
            h = hashlib.md5()
            for t in anim_data['tracks']:
                for key in ('orient', 'pos', 'scale'):
                    if t[key] is not None:
                        knots, values, _ = t[key]
                        h.update(knots.tobytes())
                        h.update(values.tobytes())
            content_hash = h.hexdigest()

            anim = Animation(
                name=anim_data['name'], channels=[], samplers=[],
                extras={
                    'granny_name': anim_data.get('granny_name', ''),
                    'content_hash': content_hash,
                },
            )

            for track in anim_data['tracks']:
                node_idx = bone_name_to_node.get(track['name'])
                if node_idx is None:
                    short = track['name'].split(':')[-1]
                    node_idx = bone_name_to_node.get(short)
                if node_idx is None:
                    continue

                # Translation channel
                if track['pos'] is not None:
                    knots, values, _ = track['pos']
                    if len(knots) > 0 and values.shape[-1] == 3:
                        time_acc = add_accessor(knots.astype(np.float32), FLOAT, SCALAR)
                        val_acc = add_accessor(values.astype(np.float32), FLOAT, VEC3)
                        si = len(anim.samplers)
                        anim.samplers.append(AnimationSampler(
                            input=time_acc, output=val_acc, interpolation="LINEAR"))
                        anim.channels.append(AnimationChannel(
                            sampler=si,
                            target=AnimationChannelTarget(node=node_idx, path="translation")))

                # Rotation channel (quaternion XYZW — same order as glTF)
                if track['orient'] is not None:
                    knots, values, _ = track['orient']
                    if len(knots) > 0 and values.shape[-1] == 4:
                        time_acc = add_accessor(knots.astype(np.float32), FLOAT, SCALAR)
                        val_acc = add_accessor(values.astype(np.float32), FLOAT, VEC4)
                        si = len(anim.samplers)
                        anim.samplers.append(AnimationSampler(
                            input=time_acc, output=val_acc, interpolation="LINEAR"))
                        anim.channels.append(AnimationChannel(
                            sampler=si,
                            target=AnimationChannelTarget(node=node_idx, path="rotation")))

                # Scale channel (extract diagonal from 3x3 matrix, or use 3D directly)
                if track['scale'] is not None:
                    knots, values, _ = track['scale']
                    if len(knots) > 0:
                        if values.shape[-1] == 9:
                            # 3x3 matrix → extract diagonal as scale
                            scale_vals = values[:, [0, 4, 8]]  # M00, M11, M22
                        elif values.shape[-1] == 3:
                            scale_vals = values
                        else:
                            continue
                        time_acc = add_accessor(knots.astype(np.float32), FLOAT, SCALAR)
                        val_acc = add_accessor(scale_vals.astype(np.float32), FLOAT, VEC3)
                        si = len(anim.samplers)
                        anim.samplers.append(AnimationSampler(
                            input=time_acc, output=val_acc, interpolation="LINEAR"))
                        anim.channels.append(AnimationChannel(
                            sampler=si,
                            target=AnimationChannelTarget(node=node_idx, path="scale")))

            if anim.channels:
                gltf.animations.append(anim)

            if (anim_idx + 1) % 100 == 0 or anim_idx + 1 == total_anims:
                print(f"    {anim_idx+1}/{total_anims} animations built", flush=True)

    print("  Assembling binary buffer...", flush=True)
    combined = b''.join(all_buffers)
    gltf.buffers = [Buffer(byteLength=len(combined))]
    gltf.set_binary_blob(combined)
    return gltf

# ─────────────────────────── Texture extraction ─────────────────────────────

def _extract_model_texture(pkg_dir, character_name):
    """
    Extract the 3D model texture from .pkg files for the given character.
    Searches all .pkg files in pkg_dir for matching texture entries.
    Returns (png_bytes, dds_bytes) or (None, None) if not found.
    """
    import io
    try:
        import texture2ddecoder
        from PIL import Image
    except ImportError:
        print("  WARNING: texture2ddecoder or Pillow not installed. "
              "Run: pip install texture2ddecoder Pillow")
        return None, None

    from pkg_texture import read_pkg_chunks, _swap32, _read_7bit_int, _read_csstring, build_dds_header

    # Search patterns for the character's model texture
    cn = character_name.lower()
    search = [
        f"{cn}transform_color",
        f"{cn}_color512",
        f"{cn}_color",
    ]

    # Search all .pkg files, starting with Fx.pkg (most common) then others
    pkg_files = []
    fx = os.path.join(pkg_dir, "Fx.pkg")
    if os.path.isfile(fx):
        pkg_files.append(fx)
    for f in sorted(os.listdir(pkg_dir)):
        full = os.path.join(pkg_dir, f)
        if f.endswith('.pkg') and f != 'Fx.pkg' and os.path.isfile(full):
            pkg_files.append(full)

    for pkg_path in pkg_files:
        try:
            chunks, _, _ = read_pkg_chunks(pkg_path)
        except Exception:
            continue

        for ci, (chunk, _) in enumerate(chunks):
            doff = 0
            while doff < len(chunk) - 5:
                tag = chunk[doff]; doff += 1
                if tag in (0xFF, 0xBE):
                    break
                if tag == 0xAD:  # Tex2D
                    name, doff = _read_csstring(chunk, doff)
                    total_sz = _swap32(struct.unpack_from('<I', chunk, doff)[0])
                    doff += 4
                    data_start = doff

                    filename_lower = name.lower().replace('\\', '/').split('/')[-1]
                    if any(filename_lower.startswith(s) or filename_lower == s for s in search):
                        if chunk[doff:doff+3] == b'XNB':
                            doff += 10
                            fmt = struct.unpack_from('<I', chunk, doff)[0]
                            tw = struct.unpack_from('<I', chunk, doff+4)[0]
                            th = struct.unpack_from('<I', chunk, doff+8)[0]
                            tps = struct.unpack_from('<I', chunk, doff+16)[0]
                            pixel_data = chunk[doff+20:doff+20+tps]

                            dds_header = build_dds_header(tw, th, fmt, tps)
                            dds_bytes = dds_header + pixel_data

                            if fmt == 0x1C:  # BC7
                                blocks = max(tw//4, 1) * max(th//4, 1)
                                mip0_size = blocks * 16
                                bgra = texture2ddecoder.decode_bc7(pixel_data[:mip0_size], tw, th)
                                img = Image.frombytes('RGBA', (tw, th), bgra)
                                r, g, b, a = img.split()
                                img = Image.merge('RGBA', (b, g, r, a))
                            elif fmt in (0x00, 0x0E):
                                mip0_size = tw * th * 4
                                img = Image.frombytes('RGBA', (tw, th), pixel_data[:mip0_size])
                            elif fmt == 0x06:  # BC3/DXT5
                                blocks = max(tw//4, 1) * max(th//4, 1)
                                mip0_size = blocks * 16
                                bgra = texture2ddecoder.decode_bc3(pixel_data[:mip0_size], tw, th)
                                img = Image.frombytes('RGBA', (tw, th), bgra)
                                r, g, b, a = img.split()
                                img = Image.merge('RGBA', (b, g, r, a))
                            else:
                                print(f"  Unsupported texture format 0x{fmt:X}")
                                continue

                            png_buf = io.BytesIO()
                            img.save(png_buf, format='PNG')
                            png_bytes = png_buf.getvalue()

                            pkg_name = os.path.basename(pkg_path)
                            print(f"  Found: {name} ({tw}x{th} fmt=0x{fmt:X} "
                                  f"{tps:,} bytes) in {pkg_name}")
                            return png_bytes, dds_bytes

                    doff = data_start + total_sz
                elif tag == 0xDE:
                    sz = struct.unpack_from('<i', chunk, doff)[0]; doff += 4 + sz
                elif tag == 0xAA:
                    nl, doff = _read_7bit_int(chunk, doff); doff += nl
                    sz = struct.unpack_from('<i', chunk, doff)[0]; doff += 4 + max(sz, 0)
                else:
                    break

    return None, None


# ─────────────────────────── Main ────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Export Hades II character to glTF")
    parser.add_argument("name", help="Character name, e.g. Melinoe")
    parser.add_argument("--gpk-dir", default="../Content/GR2/_Optimized",
                        help="Directory containing .gpk and .sdb files")
    parser.add_argument("--dll", default="./granny2_x64.dll",
                        help="Path to granny2_x64.dll")
    parser.add_argument("--mesh-index", type=int, default=None,
                        help="Export only this mesh index; default: all")
    parser.add_argument("--all-lods", action="store_true",
                        help="(deprecated, all meshes now exported by default)")
    parser.add_argument("-o", "--output", default=None, help="Output .glb path")
    parser.add_argument("--debug", action="store_true",
                        help="Print per-mesh vertex layout and bone binding info")
    parser.add_argument("--animations", action="store_true",
                        help="Include animation data from the GPK")
    parser.add_argument("--anim-filter", default=None,
                        help="Only export animations matching this pattern (e.g. 'Idle')")
    parser.add_argument("--textures", action="store_true",
                        help="Embed 3D model texture in GLB + save original DDS alongside")
    parser.add_argument("--pkg-dir", default=None,
                        help="Directory containing .pkg texture packages "
                             "(default: auto-detect from gpk-dir)")
    args = parser.parse_args()

    gpk_path = os.path.join(args.gpk_dir, f"{args.name}.gpk")
    sdb_path = os.path.join(args.gpk_dir, f"{args.name}.sdb")
    out_path = args.output or f"{args.name}.glb"

    print(f"[1/5] Extracting GPK: {gpk_path}")
    entries = extract_all_from_gpk(gpk_path)
    mesh_key = f"{args.name}_Mesh"
    if mesh_key not in entries:
        mesh_key = next((k for k in entries if k.endswith('_Mesh')), None)
    if not mesh_key:
        print(f"ERROR: No _Mesh entry found. Available: {list(entries.keys())[:10]}")
        sys.exit(1)
    gr2_bytes = entries[mesh_key]
    print(f"  Mesh entry: {mesh_key!r} ({len(gr2_bytes):,} bytes decompressed)")

    print(f"[2/5] Loading SDB: {sdb_path}")
    with open(sdb_path, 'rb') as f:
        sdb_bytes = f.read()

    print(f"[3/5] Loading Granny DLL and building type map")
    dll = setup_granny(args.dll)
    gr2_file, sdb_file, fi, _keep = load_gr2(dll, gr2_bytes, sdb_bytes)

    print(f"[4/5] Reading skeleton and mesh data")
    bones, bone_name_to_idx = read_skeleton(fi)
    print(f"  Skeleton: {len(bones)} bones")

    # 'Meshes' is ArrayOfReferencesMember: {count[4], ptr[8]}.
    _meshes    = _t('granny_file_info', 'Meshes')
    mesh_count = ri(fi, _meshes)
    meshes_arr = rq(fi, _meshes + 4)

    mesh_indices = [args.mesh_index] if args.mesh_index is not None else list(range(mesh_count))

    mesh_data_list = []
    mesh_names = []
    seen_names: dict = {}
    for mi in mesh_indices:
        mesh_ptr = rq(meshes_arr, mi * 8)
        result = read_mesh_data(mesh_ptr, bone_name_to_idx, dll, debug=args.debug)
        if result is None:
            continue
        mesh_name, positions, normals, uvs, weights, joints, indices = result
        if mesh_name in seen_names:
            seen_names[mesh_name] += 1
            mesh_name = f"{mesh_name}_LOD{seen_names[mesh_name]}"
        else:
            seen_names[mesh_name] = 0
        mtype = "rigid" if joints is None else "skinned"
        print(f"  Mesh[{mi}] {mesh_name!r}: {len(positions)} verts, {len(indices)//3} tris  [{mtype}]")
        mesh_data_list.append((positions, normals, uvs, weights, joints, indices))
        mesh_names.append(mesh_name)

    # ── Animations ──
    anim_data = None
    if args.animations or args.anim_filter:
        print(f"[5/6] Extracting animations")
        anim_data = extract_animations(dll, entries, sdb_bytes, anim_filter=args.anim_filter)
    else:
        print(f"[5/6] Animations: skipped (use --animations to include)")

    # ── Textures ──
    texture_png = None
    if args.textures:
        print(f"[*] Extracting 3D model texture", flush=True)
        pkg_dir = args.pkg_dir
        if pkg_dir is None:
            # Auto-detect: gpk_dir is .../Content/GR2/_Optimized, pkg_dir is .../Content/Packages/1080p
            content_dir = os.path.dirname(os.path.dirname(args.gpk_dir))
            pkg_dir = os.path.join(content_dir, "Packages", "1080p")
        if os.path.isdir(pkg_dir):
            try:
                texture_png, dds_data = _extract_model_texture(pkg_dir, args.name)
                if texture_png:
                    print(f"  Texture embedded as PNG in GLB", flush=True)
                    # Save original DDS alongside
                    dds_dir = os.path.splitext(out_path)[0] + "_textures"
                    os.makedirs(dds_dir, exist_ok=True)
                    dds_path = os.path.join(dds_dir, f"{args.name}_Color.dds")
                    with open(dds_path, 'wb') as f:
                        f.write(dds_data)
                    print(f"  Original DDS saved: {dds_path}", flush=True)
                else:
                    print(f"  No texture found for {args.name} in Fx.pkg")
            except Exception as e:
                print(f"  Texture extraction failed: {e}")
        else:
            print(f"  Package directory not found: {pkg_dir}")

    step = "6/6" if (args.animations or args.anim_filter) else "5/5"
    print(f"[{step}] Building glTF -> {out_path}", flush=True)
    gltf = build_gltf(args.name, mesh_data_list, mesh_names, bones,
                       animations=anim_data, texture_png_bytes=texture_png)
    print(f"  Saving GLB file...", flush=True)
    gltf.save(out_path)
    anim_msg = f", {len(anim_data)} animations" if anim_data else ""
    tex_msg = ", textured" if texture_png else ""
    print(f"\nDone!  {len(mesh_data_list)} meshes, {len(bones)} bones{anim_msg}{tex_msg} -> {out_path}")

    dll.GrannyFreeFile(gr2_file)
    dll.GrannyFreeFile(sdb_file)


if __name__ == '__main__':
    main()
