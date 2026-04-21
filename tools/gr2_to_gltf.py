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
import argparse
import ctypes
import hashlib
import io
import json
import multiprocessing
import os
import re
import struct
import sys
import traceback
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import numpy as np
import lz4.block
import pygltflib
from pygltflib import (GLTF2, Scene, Node, Mesh, Primitive, Skin, Accessor, BufferView, Buffer,
                       Animation, AnimationSampler, AnimationChannel, AnimationChannelTarget)
from pygltflib import FLOAT, UNSIGNED_SHORT, VEC3, VEC4, SCALAR, MAT4, VEC2
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
from pkg_texture import (
    _read_7bit_int, _read_csstring, _swap32, build_dds_header, load_texture_index,
    read_pkg_chunks, save_texture_index, scan_textures,
)

# Module-level state populated by setup_granny().
_TYPES: dict = {}
_BONE_STRIDE: int = 0
_BB_STRIDE: int = 0
_TRANSFORM_OFFS: dict = {}   # offsets within granny_transform: flags/translation/orientation


def build_manifest(character, out_basename, mesh_entries, mesh_names,
                   exported_gr2_indices, mesh_data_list, mesh_bb_names_list,
                   manifest_textures=None, anim_data=None):
    """Construct the manifest dict for an exported character.

    Pure function — no I/O.  mesh_data_list contains tuples of
    (positions, normals, uvs, weights, joints, indices); mesh_bb_names_list
    parallels it with the bone-binding name list per mesh.
    """
    meshes = []
    for mn, gi, md, bb in zip(mesh_names, exported_gr2_indices,
                              mesh_data_list, mesh_bb_names_list):
        meshes.append({
            'name': mn,
            'entry': gi['entry'],
            'gr2_index': gi['gr2_index'],
            'vertex_count': len(md[0]),
            'index_count': len(md[5]),
            'position_hash': hashlib.md5(md[0].tobytes()).hexdigest(),
            'bb_names': list(bb),
        })
    manifest = {
        'character': character,
        'glb': out_basename,
        'gpk': f"{character}.gpk",
        'sdb': f"{character}.sdb",
        'mesh_entries': mesh_entries,
        'meshes': meshes,
    }
    if manifest_textures:
        manifest['textures'] = manifest_textures
    if anim_data:
        manifest['animations'] = {
            'count': len(anim_data),
            'names': [a['name'] for a in anim_data],
        }
    return manifest


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
        nl = raw[off]
        name = raw[off+1:off+1+nl].decode('utf-8')
        off += 1 + nl
        cs = struct.unpack_from('<I', raw, off)[0]
        off += 4
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

try:
    _kernel32 = ctypes.windll.kernel32
except AttributeError:
    _kernel32 = None

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
    bb_name_list = []
    debug_palette = []
    for bi in range(bb_count):
        entry    = bb_ptr + bi * _BB_STRIDE
        name_ptr = rq(entry, off_bb_name) if _readable(entry, 8) else 0
        bb_name  = read_cstr(name_ptr) if _readable(name_ptr, 4) else ''
        global_idx = bone_name_to_idx.get(bb_name, 0)
        palette.append(global_idx)
        bb_name_list.append(bb_name)
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

    return mesh_name, positions, normals, uvs, weights, joints, indices, bb_name_list

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
    1. Drop constant single-key curves (no-ops that confuse Blender)
    2. Quaternion sign fixup (prevents slerp long-path jitter)
    3. Merge adjacent keyframes within 4ms
    4. Remove trivial keyframes that lie on the interpolation path
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


def _process_single_anim(dll, str_db, entry_name, gr2_bytes):
    """Process one animation entry, return dict or None."""
    gr2_buf = ctypes.create_string_buffer(gr2_bytes)
    gr2_file = dll.GrannyReadEntireFileFromMemory(len(gr2_bytes), gr2_buf)
    if not gr2_file:
        return None
    dll.GrannyRemapFileStrings(gr2_file, str_db)
    fi = dll.GrannyGetFileInfo(gr2_file)
    if not fi:
        dll.GrannyFreeFile(gr2_file)
        return None

    anim_count = ri(fi, 0x78)
    if anim_count == 0:
        dll.GrannyFreeFile(gr2_file)
        return None

    anim0 = rq(rq(fi, 0x7C), 0)
    anim_name = read_cstr(rq(anim0, 0))
    duration = struct.unpack_from('<f', safe_bytes(anim0 + 8, 4))[0]
    if duration <= 0:
        dll.GrannyFreeFile(gr2_file)
        return None

    tg_count = ri(fi, 0x6C)
    if tg_count == 0:
        dll.GrannyFreeFile(gr2_file)
        return None
    tg0 = rq(rq(fi, 0x70), 0)
    tt_count = ri(tg0, 0x14)
    tt_ptr = rq(tg0, 0x18)
    if tt_count == 0 or not _valid_ptr(tt_ptr):
        dll.GrannyFreeFile(gr2_file)
        return None

    tracks = []
    for ti in range(tt_count):
        t_base = tt_ptr + ti * TRACK_STRIDE
        t_name = read_cstr(rq(t_base, 0))

        orient = _decode_curve(dll, t_base + 0x0C, rq(t_base, 0x14), 4)
        pos    = _decode_curve(dll, t_base + 0x1C, rq(t_base, 0x24), 3)
        scale  = _decode_curve(dll, t_base + 0x2C, rq(t_base, 0x34), 9)

        if orient is None and pos is None and scale is None:
            continue
        track = {'name': t_name, 'orient': orient, 'pos': pos, 'scale': scale,
                 'bone_index': ti}
        _postprocess_track(track)
        if track['orient'] is None and track['pos'] is None and track['scale'] is None:
            continue
        tracks.append(track)

    dll.GrannyFreeFile(gr2_file)

    if tracks:
        return {
            'name': entry_name,
            'granny_name': anim_name,
            'duration': duration,
            'tracks': tracks,
        }
    return None


def _anim_worker_batch(args):
    """
    Worker function for multiprocessing animation extraction.
    Each worker loads its own DLL + SDB instance and processes a batch of entries.
    args: (dll_path, sdb_bytes, entries_list)  where entries_list = [(name, gr2_bytes), ...]
    Returns list of animation dicts.
    """
    dll_path, sdb_bytes, entries_list = args
    # Each worker gets its own DLL instance
    dll = setup_granny(dll_path)
    sdb_buf = ctypes.create_string_buffer(sdb_bytes)
    sdb_file = dll.GrannyReadEntireFileFromMemory(len(sdb_bytes), sdb_buf)
    if not sdb_file:
        return []
    str_db = dll.GrannyGetStringDatabase(sdb_file)

    results = []
    for entry_name, gr2_bytes in entries_list:
        anim = _process_single_anim(dll, str_db, entry_name, gr2_bytes)
        if anim:
            results.append(anim)

    dll.GrannyFreeFile(sdb_file)
    return results


def extract_animations(dll, gpk_entries, sdb_bytes, anim_filter=None,
                       dll_path=None, anim_workers=0):
    """
    Extract animation data from all non-mesh GPK entries.
    Uses multiprocessing when dll_path is provided and entry count is large enough.
    anim_workers: 0 = auto (cpu_count), 1 = sequential, N = use N workers.
    Returns list of dicts with name, duration, and decoded track data.
    """
    anim_entries = {k: v for k, v in gpk_entries.items() if not k.endswith('_Mesh')}
    if anim_filter:
        pattern = anim_filter.lower()
        anim_entries = {k: v for k, v in anim_entries.items() if pattern in k.lower()}

    total = len(anim_entries)
    print(f"  Processing {total} animation entries" +
          (f" (filtered by '{anim_filter}')" if anim_filter else ""), flush=True)

    if total == 0:
        return []

    # Use multiprocessing for large animation sets
    if anim_workers == 0:
        n_workers = min(os.cpu_count() or 4, max(1, total // 10))
    else:
        n_workers = anim_workers
    if dll_path and n_workers > 1 and total >= 20:
        print(f"  Using {n_workers} parallel workers", flush=True)

        # Split entries into roughly equal chunks
        entries_list = list(anim_entries.items())
        chunk_size = (total + n_workers - 1) // n_workers
        chunks = []
        for i in range(0, total, chunk_size):
            chunks.append((dll_path, sdb_bytes, entries_list[i:i+chunk_size]))

        animations = []
        with multiprocessing.Pool(n_workers) as pool:
            for batch_idx, batch_result in enumerate(pool.imap_unordered(_anim_worker_batch, chunks)):
                animations.extend(batch_result)
                print(f"    Worker done: +{len(batch_result)} animations "
                      f"({len(animations)} total)", flush=True)

        print(f"  Extracted {len(animations)} animations")
        return animations

    # Sequential fallback
    animations = []
    sdb_buf = ctypes.create_string_buffer(sdb_bytes)
    sdb_file = dll.GrannyReadEntireFileFromMemory(len(sdb_bytes), sdb_buf)
    if not sdb_file:
        return animations
    str_db = dll.GrannyGetStringDatabase(sdb_file)

    for idx, (entry_name, gr2_bytes) in enumerate(anim_entries.items()):
        anim = _process_single_anim(dll, str_db, entry_name, gr2_bytes)
        if anim:
            animations.append(anim)
        if (idx + 1) % 25 == 0 or idx + 1 == total:
            print(f"    {idx+1}/{total} processed ({len(animations)} animations)", flush=True)

    dll.GrannyFreeFile(sdb_file)
    print(f"  Extracted {len(animations)} animations")
    return animations


# ─────────────────────────── glTF export ─────────────────────────────────────

def build_gltf(character_name, mesh_data_list, mesh_names, bones, animations=None,
               texture_map=None):
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

    # ── Textures + Materials ──
    # texture_map: {tex_base_name: (png_bytes, mesh_indices)} from PKG extraction
    tex_name_to_mat = {}  # tex_base_name → glTF material index
    mesh_idx_to_mat = {}  # GR2 mesh index → glTF material index

    if texture_map:
        for tex_name, (png_bytes, mesh_indices) in texture_map.items():
            png_bv_offset = sum(len(b) for b in all_buffers)
            pad = (4 - (len(png_bytes) % 4)) % 4
            all_buffers.append(png_bytes + b'\x00' * pad)
            png_bv_idx = len(gltf.bufferViews)
            gltf.bufferViews.append(BufferView(
                buffer=0, byteOffset=png_bv_offset, byteLength=len(png_bytes),
            ))

            img_idx = len(gltf.images)
            gltf.images.append(pygltflib.Image(
                bufferView=png_bv_idx, mimeType="image/png", name=tex_name,
            ))

            tex_idx = len(gltf.textures)
            gltf.textures.append(pygltflib.Texture(source=img_idx))

            mat_idx = len(gltf.materials)
            gltf.materials.append(pygltflib.Material(
                name=f"Mat_{tex_name}",
                pbrMetallicRoughness=pygltflib.PbrMetallicRoughness(
                    baseColorTexture=pygltflib.TextureInfo(index=tex_idx),
                    metallicFactor=0.0,
                    roughnessFactor=1.0,
                ),
                doubleSided=True,
            ))
            tex_name_to_mat[tex_name] = mat_idx
            for mi in mesh_indices:
                mesh_idx_to_mat[mi] = mat_idx

    # ── Mesh nodes ──
    for mesh_i, (mesh_data, mesh_name) in enumerate(zip(mesh_data_list, mesh_names)):
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

        # Assign material — skip outline/shadow meshes
        prim_mat = None
        if 'Outline' not in mesh_name and 'Shadow' not in mesh_name:
            prim_mat = mesh_idx_to_mat.get(mesh_i)

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
        # Build bone name -> node index mapping for animation channel targeting.
        # Granny uses case-insensitive exact name matching by default.
        # Some characters have animation tracks with '_static' suffix that the
        # mesh skeleton lacks — we strip it as a workaround (see project_static_suffix.md).
        bone_name_to_node = {}
        for i, bone in enumerate(bones):
            bone_name_to_node[bone['name']] = bone_node_indices[i]
            short = bone['name'].split(':')[-1]
            if short not in bone_name_to_node:
                bone_name_to_node[short] = bone_node_indices[i]

        total_anims = len(animations)
        print(f"  Building {total_anims} animation(s) into glTF...", flush=True)
        for anim_idx, anim_data in enumerate(animations):
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
                # Strip _static suffix (some animation skeletons use it)
                if node_idx is None:
                    stripped = track['name'].removesuffix('_static').removesuffix('_Static')
                    node_idx = bone_name_to_node.get(stripped)
                    if node_idx is None:
                        node_idx = bone_name_to_node.get(stripped.split(':')[-1])
                if node_idx is None:
                    continue

                # Gap-fill: if a bone has ANY channel, emit ALL three (pos+rot+scale).
                # Blender misinterprets partial channels (e.g. position-only causes
                # bogus scale values). Fill missing channels with rest pose constants.
                bone_idx = bone_node_indices.index(node_idx) if node_idx in bone_node_indices else None
                has_any = track['pos'] is not None or track['orient'] is not None or track['scale'] is not None
                if bone_idx is not None and has_any:
                    if track['pos'] is None:
                        tx, ty, tz = bones[bone_idx]['translation']
                        track['pos'] = (np.array([0.0], dtype=np.float32),
                                        np.array([[tx, ty, tz]], dtype=np.float32), 0)
                    if track['orient'] is None:
                        rx, ry, rz, rw = bones[bone_idx]['rotation']
                        track['orient'] = (np.array([0.0], dtype=np.float32),
                                           np.array([[rx, ry, rz, rw]], dtype=np.float32), 0)
                    if track['scale'] is None:
                        track['scale'] = (np.array([0.0], dtype=np.float32),
                                          np.array([[1.0, 1.0, 1.0]], dtype=np.float32), 0)

                # Translation channel
                if track['pos'] is not None:
                    knots, values, _ = track['pos']
                    if (len(knots) > 0 and values.shape[-1] == 3
                            and np.all(np.isfinite(values))
                            and not np.any(np.abs(values) > 1000.0)):
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
                    if len(knots) > 0 and values.shape[-1] == 4 and np.all(np.isfinite(values)):
                        # Normalize quaternions — non-unit quats cause mesh stretching
                        norms = np.linalg.norm(values, axis=1, keepdims=True)
                        norms = np.maximum(norms, 1e-10)
                        values = values / norms
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
                            # Check if the matrix has significant off-diagonal elements
                            # (rotation/shear) — if so, diagonal is NOT a valid scale
                            off_diag = values[:, [1, 2, 3, 5, 6, 7]]
                            if np.any(np.abs(off_diag) > 0.01):
                                continue  # skip — shear/rotation matrix, not pure scale
                        elif values.shape[-1] == 3:
                            scale_vals = values
                        else:
                            continue
                        # Validate: skip NaN/Inf/extreme values
                        if not np.all(np.isfinite(scale_vals)):
                            continue
                        if np.any(np.abs(scale_vals) > 100.0):
                            continue  # extreme scale values — likely corrupt
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

def _load_granny_texture_overrides(scripts_dir):
    """
    Parse Lua scripts for GrannyTexture overrides.
    The game's entity data can replace a GR2's texture reference at runtime.
    Returns dict {character_name_lower: [texture_base_names]}.
    """
    overrides = {}  # character_lower -> [tex_base_name, ...]
    if not os.path.isdir(scripts_dir):
        return overrides
    for root, dirs, files in os.walk(scripts_dir):
        for f in files:
            if not f.endswith('.lua'):
                continue
            try:
                data = open(os.path.join(root, f), 'r', errors='replace').read()
            except Exception:
                continue
            for m in re.finditer(r'GrannyTexture\s*=\s*"([^"]+)"', data):
                tex_path = m.group(1)
                tex_name = tex_path.replace('\\', '/').split('/')[-1]
                # Derive character from filename: EnemyData_EarthElemental.lua -> earthelemental
                char = f.replace('.lua', '')
                for prefix in ('EnemyData_', 'NPCData_', 'NPCData'):
                    if char.startswith(prefix):
                        char = char[len(prefix):]
                        break
                char = char.lower()
                overrides.setdefault(char, []).append(tex_name)
    return overrides


def _resolve_texture_from_material(mat_ptr, debug=False):
    """
    Given a granny_material pointer, try to find a texture filename.
    Tries two paths:
      1. material → Maps[0] → nested material → Texture → FromFileName
      2. material → Texture → FromFileName (direct reference)
    Returns base filename or None.
    """
    try:
        fn_off = _t('granny_texture', 'FromFileName')
    except KeyError:
        fn_off = 0

    try:
        tex_ref_off = _t('granny_material', 'Texture')
    except KeyError:
        tex_ref_off = None

    def _tex_filename(tex_ptr):
        if not _valid_ptr(tex_ptr):
            return None
        fn_ptr = rq(tex_ptr, fn_off)
        fn = read_cstr(fn_ptr)
        if fn:
            base = fn.replace('\\', '/').split('/')[-1]
            return base.rsplit('.', 1)[0] if '.' in base else base
        return None

    # Path 1: material → Maps[0] → nested material → Texture → FromFileName
    try:
        maps_off = _t('granny_material', 'Maps')
    except KeyError:
        maps_off = None
        if debug:
            print("        No 'Maps' field in granny_material type map")

    if maps_off is not None:
        maps_count = ri(mat_ptr, maps_off)
        if debug:
            print(f"        Maps: count={maps_count} at offset 0x{maps_off:X}")
        if maps_count > 0:
            maps_arr = rq(mat_ptr, maps_off + 4)
            if _valid_ptr(maps_arr):
                try:
                    map_mat_off = _t('granny_material_map', 'Material')
                except KeyError:
                    map_mat_off = 8

                # Check each map entry
                try:
                    map_stride = _t('granny_material_map', '__stride__')
                except KeyError:
                    map_stride = 16  # Usage*(8) + Material*(8)

                if debug:
                    print(f"        map_stride={map_stride} map_mat_off={map_mat_off}")

                for mi in range(maps_count):
                    nested_mat = rq(maps_arr, mi * map_stride + map_mat_off)
                    if not _valid_ptr(nested_mat):
                        if debug:
                            print(f"        Map[{mi}]: invalid nested_mat ptr")
                        continue
                    nested_name = read_cstr(rq(nested_mat, 0))
                    if debug:
                        print(f"        Map[{mi}]: nested material '{nested_name}' at 0x{nested_mat:X}")
                    # Try nested material → Texture
                    if tex_ref_off is not None:
                        tex_ptr = rq(nested_mat, tex_ref_off)
                        if debug:
                            print(f"          Texture ptr at +0x{tex_ref_off:X}: 0x{tex_ptr:X} valid={_valid_ptr(tex_ptr)}")
                        result = _tex_filename(tex_ptr)
                        if result:
                            return result
                    # Try nested material → Maps recursively (one level)
                    if maps_off is not None:
                        inner_count = ri(nested_mat, maps_off)
                        if inner_count > 0:
                            inner_arr = rq(nested_mat, maps_off + 4)
                            if debug:
                                print(f"          Inner maps: count={inner_count}")
                            if _valid_ptr(inner_arr):
                                for ii in range(inner_count):
                                    inner_mat = rq(inner_arr, ii * map_stride + map_mat_off)
                                    if _valid_ptr(inner_mat) and tex_ref_off is not None:
                                        tex_ptr = rq(inner_mat, tex_ref_off)
                                        result = _tex_filename(tex_ptr)
                                        if result:
                                            return result

    # Path 2: material → Texture directly
    if tex_ref_off is not None:
        tex_ptr = rq(mat_ptr, tex_ref_off)
        result = _tex_filename(tex_ptr)
        if result:
            return result

    return None


def _resolve_mesh_texture(mesh_ptr, debug=False):
    """
    Walk the material chain for a mesh to find its texture filename.
    Returns base filename (e.g. "Melinoe_Color512") or None.
    """
    try:
        mb_off = _t('granny_mesh', 'MaterialBindings')
        if debug:
            print(f"      MaterialBindings offset from type map: 0x{mb_off:X}")
    except KeyError:
        try:
            bb_off = _t('granny_mesh', 'BoneBindings')
            mb_off = bb_off - 0xC
            if debug:
                print(f"      MaterialBindings fallback from BoneBindings(0x{bb_off:X}): 0x{mb_off:X}")
        except KeyError:
            if debug:
                print("      No MaterialBindings or BoneBindings offset found")
            return None

    mb_count = ri(mesh_ptr, mb_off)
    if mb_count <= 0:
        if debug:
            print(f"      MaterialBindings count = {mb_count}")
        return None
    mb_arr = rq(mesh_ptr, mb_off + 4)
    if not _valid_ptr(mb_arr):
        if debug:
            print(f"      MaterialBindings ptr invalid: 0x{mb_arr:X}")
        return None

    if debug:
        print(f"      MaterialBindings: count={mb_count} arr=0x{mb_arr:X}")

    # Try each material binding until we find a texture
    for bi in range(mb_count):
        mat_ptr = rq(mb_arr, bi * 8)
        if not _valid_ptr(mat_ptr):
            if debug:
                print(f"      Binding[{bi}]: invalid ptr")
            continue
        mat_name = read_cstr(rq(mat_ptr, 0))
        if debug:
            print(f"      Binding[{bi}]: Material '{mat_name}' at 0x{mat_ptr:X}")
        result = _resolve_texture_from_material(mat_ptr, debug=debug)
        if result:
            return result

    return None


def _read_mesh_texture_map(fi, debug=False):
    """
    Build a mapping of mesh_index → texture base filename by walking
    each mesh's material chain.
    Returns dict {mesh_index: "TextureName"} and list of unique texture names.
    """
    _meshes = _t('granny_file_info', 'Meshes')
    mesh_count = ri(fi, _meshes)
    meshes_arr = rq(fi, _meshes + 4)

    mesh_tex = {}
    unique_textures = []
    for mi in range(mesh_count):
        mesh_ptr = rq(meshes_arr, mi * 8)
        mesh_name = read_cstr(rq(mesh_ptr, 0))
        tex_name = _resolve_mesh_texture(mesh_ptr, debug=debug)
        if tex_name:
            mesh_tex[mi] = tex_name
            if tex_name not in unique_textures:
                unique_textures.append(tex_name)
            print(f"    Mesh[{mi}] {mesh_name!r} → texture: {tex_name}")
        else:
            print(f"    Mesh[{mi}] {mesh_name!r} → no texture found")

    return mesh_tex, unique_textures


_TEXTURE_DECODE_WARNING_SHOWN = False

def _decode_texture_to_png(pixel_data, tw, th, fmt):
    """Decode raw pixel data to PNG bytes. Returns PNG bytes or None."""
    global _TEXTURE_DECODE_WARNING_SHOWN
    try:
        import texture2ddecoder
        from PIL import Image
    except ImportError:
        if not _TEXTURE_DECODE_WARNING_SHOWN:
            _TEXTURE_DECODE_WARNING_SHOWN = True
            print("  ERROR: texture2ddecoder or Pillow not installed.")
            print("  Run: pip install texture2ddecoder Pillow")
        return None

    img = None
    if fmt == 0x1C:  # BC7
        blocks = max(tw // 4, 1) * max(th // 4, 1)
        mip0_size = blocks * 16
        bgra = texture2ddecoder.decode_bc7(pixel_data[:mip0_size], tw, th)
        img = Image.frombytes('RGBA', (tw, th), bgra)
        r, g, b, a = img.split()
        img = Image.merge('RGBA', (b, g, r, a))
    elif fmt in (0x00, 0x0E):
        mip0_size = tw * th * 4
        img = Image.frombytes('RGBA', (tw, th), pixel_data[:mip0_size])
    elif fmt == 0x06:  # BC3/DXT5
        blocks = max(tw // 4, 1) * max(th // 4, 1)
        mip0_size = blocks * 16
        bgra = texture2ddecoder.decode_bc3(pixel_data[:mip0_size], tw, th)
        img = Image.frombytes('RGBA', (tw, th), bgra)
        r, g, b, a = img.split()
        img = Image.merge('RGBA', (b, g, r, a))
    elif fmt == 0x04:  # BC1/DXT1
        blocks = max(tw // 4, 1) * max(th // 4, 1)
        mip0_size = blocks * 8
        bgra = texture2ddecoder.decode_bc1(pixel_data[:mip0_size], tw, th)
        img = Image.frombytes('RGBA', (tw, th), bgra)
        r, g, b, a = img.split()
        img = Image.merge('RGBA', (b, g, r, a))

    if img:
        png_buf = io.BytesIO()
        img.save(png_buf, format='PNG')
        return png_buf.getvalue()
    return None


def _extract_all_textures(pkg_dir, texture_names):
    """
    Extract multiple textures by exact name from .pkg files.
    Handles both Tex2D (0xAD) and Atlas (0xDE) texture entries.
    texture_names: list of base filenames (e.g. ["Athena_Color", "Artemis_Color"])
    Returns dict {base_name: (png_bytes, dds_bytes)} for found textures.
    """
    remaining = {n.lower(): n for n in texture_names}  # lowercase → original
    results = {}

    # Use index for fast .pkg file lookup (build if missing)
    tex_index = load_texture_index(pkg_dir)
    if tex_index is None:
        print("  Building texture index (one-time)...", flush=True)
        save_texture_index(pkg_dir)
        tex_index = load_texture_index(pkg_dir)

    pkg_targets = {}  # pkg_path → set of lowercase names to find
    if tex_index:
        for lc_name in list(remaining.keys()):
            if lc_name in tex_index:
                pkg_path = os.path.join(pkg_dir, tex_index[lc_name]['pkg'])
                pkg_targets.setdefault(pkg_path, set()).add(lc_name)
            # not found in index → will fall through to full scan

    # For any names not found in index, scan all .pkg files
    indexed_names = set()
    for names_set in pkg_targets.values():
        indexed_names.update(names_set)
    unindexed = set(remaining.keys()) - indexed_names
    if unindexed:
        pkg_files = sorted(os.path.join(pkg_dir, f) for f in os.listdir(pkg_dir)
                          if f.endswith('.pkg') and os.path.isfile(os.path.join(pkg_dir, f)))
        for p in pkg_files:
            pkg_targets.setdefault(p, set()).update(unindexed)

    for pkg_path, search_names in pkg_targets.items():
        if not remaining:
            break
        try:
            chunks, _, _ = read_pkg_chunks(pkg_path)
        except Exception:
            continue

        # Use scan_textures which handles both Tex2D and Atlas entries
        all_textures = scan_textures(chunks)

        for t in all_textures:
            if not remaining:
                break
            fn_lower = t['name'].lower().replace('\\', '/').split('/')[-1]
            fn_base = fn_lower.rsplit('.', 1)[0] if '.' in fn_lower else fn_lower

            matched_key = None
            for sn in search_names:
                if sn in remaining and fn_base == sn:
                    matched_key = sn
                    break

            if matched_key:
                ci = t['chunk_idx']
                chunk = chunks[ci][0]
                pixel_data = chunk[t['data_offset']:t['data_offset'] + t['pixel_size']]

                dds_header = build_dds_header(t['width'], t['height'], t['format'], t['pixel_size'])
                dds_bytes = dds_header + pixel_data

                png_bytes = _decode_texture_to_png(pixel_data, t['width'], t['height'], t['format'])
                if png_bytes:
                    orig_name = remaining.pop(matched_key)
                    pkg_name = os.path.basename(pkg_path)
                    # Find ALL .pkg files containing this texture
                    all_pkgs = [pkg_name]
                    if tex_index and matched_key in tex_index:
                        all_pkgs = tex_index[matched_key].get('pkgs', [pkg_name])
                    results[orig_name] = {
                        'png': png_bytes,
                        'dds': dds_bytes,
                        'pkg': pkg_name,
                        'pkgs': all_pkgs,
                        'pkg_entry_name': t['name'],
                        'format': t['format'],
                        'format_name': t.get('format_name', ''),
                        'width': t['width'],
                        'height': t['height'],
                        'pixel_size': t['pixel_size'],
                        'mip_count': t.get('mip_count', 1),
                    }
                    src = "atlas" if t.get('atlas') else "tex2d"
                    print(f"  Found: {t['name']} ({t['width']}x{t['height']} "
                          f"fmt=0x{t['format']:X} {src}) in {', '.join(all_pkgs)}")

    # Fallback: if textures still missing and we used an index, try full scan
    if remaining and tex_index is not None:
        print(f"  Index miss — scanning all .pkg files for: {list(remaining.keys())}")
        fallback_targets = {}
        pkg_files = sorted(os.path.join(pkg_dir, f) for f in os.listdir(pkg_dir)
                          if f.endswith('.pkg') and os.path.isfile(os.path.join(pkg_dir, f)))
        for p in pkg_files:
            fallback_targets.setdefault(p, set()).update(remaining.keys())

        remaining_before = set(remaining.keys())
        for pkg_path, search_names in fallback_targets.items():
            if not remaining:
                break
            try:
                chunks, _, _ = read_pkg_chunks(pkg_path)
            except Exception:
                continue
            all_textures = scan_textures(chunks)
            for t in all_textures:
                if not remaining:
                    break
                fn_lower = t['name'].lower().replace('\\', '/').split('/')[-1]
                fn_base = fn_lower.rsplit('.', 1)[0] if '.' in fn_lower else fn_lower
                matched_key = None
                for sn in search_names:
                    if sn in remaining and fn_base == sn:
                        matched_key = sn
                        break
                if matched_key:
                    ci = t['chunk_idx']
                    chunk = chunks[ci][0]
                    pixel_data = chunk[t['data_offset']:t['data_offset'] + t['pixel_size']]
                    dds_header = build_dds_header(t['width'], t['height'], t['format'], t['pixel_size'])
                    png_bytes = _decode_texture_to_png(pixel_data, t['width'], t['height'], t['format'])
                    if png_bytes:
                        orig_name = remaining.pop(matched_key)
                        pkg_name = os.path.basename(pkg_path)
                        results[orig_name] = {
                            'png': png_bytes, 'dds': dds_header + pixel_data,
                            'pkg': pkg_name, 'pkgs': [pkg_name],
                            'pkg_entry_name': t['name'], 'format': t['format'],
                            'format_name': t.get('format_name', ''),
                            'width': t['width'], 'height': t['height'],
                            'pixel_size': t['pixel_size'], 'mip_count': t.get('mip_count', 1),
                        }
                        print(f"  Found (fallback): {t['name']} in {pkg_name}")

        # Delete stale index if fallback found textures the index missed
        if remaining_before - set(remaining.keys()):
            idx_path = os.path.join(pkg_dir, '_texture_index.json')
            if os.path.isfile(idx_path):
                print("  Removing stale texture index")
                try:
                    os.remove(idx_path)
                except OSError:
                    pass  # stale index removal is best-effort

    return results


def _extract_model_texture(pkg_dir, character_name, gr2_texture_names=None):
    """
    Extract the 3D model texture from .pkg files for the given character.
    Uses texture names from the GR2 material chain if available, otherwise
    falls back to name-based guessing.
    Returns (png_bytes, dds_bytes, texture_name) or (None, None, None) if not found.
    """
    try:
        import texture2ddecoder
        from PIL import Image
    except ImportError:
        print("  WARNING: texture2ddecoder or Pillow not installed. "
              "Run: pip install texture2ddecoder Pillow")
        return None, None

    # Build search patterns: prefer GR2 texture names (exact), fall back to guessing
    search = []
    if gr2_texture_names:
        for tn in gr2_texture_names:
            search.append(tn.lower())
        print(f"  GR2 texture names: {gr2_texture_names}")
    # Always add fallback patterns
    cn = character_name.lower()
    for pat in [f"{cn}transform_color", f"{cn}_color512", f"{cn}_color"]:
        if pat not in search:
            search.append(pat)

    # Fast path: use pre-built texture index to only read the right .pkg file
    tex_index = load_texture_index(pkg_dir)
    if tex_index:
        # Find which .pkg contains our texture using the index
        target_pkg = None
        for s in search:
            if s in tex_index:
                target_pkg = tex_index[s]['pkg']
                print(f"  Index hit: '{s}' in {target_pkg}")
                break
            # Also try startswith match for fallback patterns
            for key in tex_index:
                if key.startswith(s) or s.startswith(key):
                    target_pkg = tex_index[key]['pkg']
                    print(f"  Index hit: '{key}' ~ '{s}' in {target_pkg}")
                    break
            if target_pkg:
                break

        if target_pkg:
            pkg_files = [os.path.join(pkg_dir, target_pkg)]
        else:
            print("  No index match, falling back to full scan")
            pkg_files = None  # fall through to full scan
    else:
        pkg_files = None  # no index, full scan

    if pkg_files is None:
        # Full scan fallback: check all .pkg files
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
                tag = chunk[doff]
                doff += 1
                if tag in (0xFF, 0xBE):
                    break
                if tag == 0xAD:  # Tex2D
                    name, doff = _read_csstring(chunk, doff)
                    total_sz = _swap32(struct.unpack_from('<I', chunk, doff)[0])
                    doff += 4
                    data_start = doff

                    filename_lower = name.lower().replace('\\', '/').split('/')[-1]
                    # Strip extension if present in pkg entry name
                    fn_base = filename_lower.rsplit('.', 1)[0] if '.' in filename_lower else filename_lower
                    if any(fn_base == s or fn_base.startswith(s) for s in search):
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
                            tex_name = name.replace('\\', '/').split('/')[-1]
                            print(f"  Found: {name} ({tw}x{th} fmt=0x{fmt:X} "
                                  f"{tps:,} bytes) in {pkg_name}")
                            return png_bytes, dds_bytes, tex_name

                    doff = data_start + total_sz
                elif tag == 0xDE:
                    sz = struct.unpack_from('<i', chunk, doff)[0]
                    doff += 4 + sz
                elif tag == 0xAA:
                    nl, doff = _read_7bit_int(chunk, doff)
                    doff += nl
                    sz = struct.unpack_from('<i', chunk, doff)[0]
                    doff += 4 + max(sz, 0)
                else:
                    break

    return None, None, None


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
                        help="(deprecated, ignored — all meshes exported by default)")
    parser.add_argument("-o", "--output", default=None, help="Output .glb path")
    parser.add_argument("--debug", action="store_true",
                        help="Print per-mesh vertex layout and bone binding info")
    parser.add_argument("--animations", action="store_true",
                        help="Include animation data from the GPK")
    parser.add_argument("--anim-filter", default=None,
                        help="Only export animations matching this pattern (e.g. 'Idle')")
    parser.add_argument("--anim-workers", type=int, default=0,
                        help="Number of parallel animation workers (0=auto, 1=sequential)")
    parser.add_argument("--mesh-entry", default=None,
                        help="Mesh entry name(s) in GPK, comma-separated "
                             "(e.g. 'HecateHub_Mesh,HecateBattle_Mesh'). "
                             "Use --list-entries to see available entries.")
    parser.add_argument("--list-entries", action="store_true",
                        help="List all entries in the GPK and exit")
    parser.add_argument("--textures", action="store_true",
                        help="Embed 3D model texture in GLB + save original DDS alongside")
    parser.add_argument("--pkg-dir", default=None,
                        help="Directory containing .pkg texture packages "
                             "(default: auto-detect from gpk-dir)")
    args = parser.parse_args()

    gpk_path = os.path.join(args.gpk_dir, f"{args.name}.gpk")
    sdb_path = os.path.join(args.gpk_dir, f"{args.name}.sdb")
    out_path = args.output or f"{args.name}.glb"
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    print(f"[1/5] Extracting GPK: {gpk_path}")
    entries = extract_all_from_gpk(gpk_path)
    all_mesh_keys = [k for k in entries if k.endswith('_Mesh')]

    if args.list_entries:
        print(f"Entries in {gpk_path}:")
        for k, v in entries.items():
            tag = " [MESH]" if k.endswith('_Mesh') else ""
            print(f"  {k} ({len(v):,} bytes){tag}")
        sys.exit(0)

    # Select mesh entries to export.
    # Default: all _Mesh entries.  Use --mesh-entry to filter to specific entries.
    if not all_mesh_keys:
        print(f"ERROR: No _Mesh entry found. Available: {list(entries.keys())[:10]}")
        sys.exit(1)

    if args.mesh_entry:
        selected_keys = [s.strip() for s in args.mesh_entry.split(',')]
        mesh_keys = []
        for sk in selected_keys:
            if sk in entries:
                mesh_keys.append(sk)
            else:
                match = next((k for k in all_mesh_keys if sk in k), None)
                if match:
                    mesh_keys.append(match)
                else:
                    print(f"WARNING: entry '{sk}' not found. Available: {all_mesh_keys}")
        if not mesh_keys:
            print(f"ERROR: No matching mesh entries. Available: {all_mesh_keys}")
            sys.exit(1)
    else:
        # Default: all mesh entries
        mesh_keys = list(all_mesh_keys)
        if len(mesh_keys) > 1:
            print(f"  Multi-entry GPK: {mesh_keys}")

    print(f"[2/5] Loading SDB: {sdb_path}")
    with open(sdb_path, 'rb') as f:
        sdb_bytes = f.read()

    print("[3/5] Loading Granny DLL and building type map")
    dll = setup_granny(args.dll)

    print("[4/5] Reading skeleton and mesh data")
    mesh_data_list = []
    mesh_bb_names_list = []
    mesh_names = []
    exported_gr2_indices = []
    local_mesh_tex = {}  # local_index → tex_name (resolved per-entry)
    seen_names: dict = {}
    bones = None
    bone_name_to_idx = None
    _all_keeps = []  # keep all DLL buffers alive

    for mk_idx, mesh_key in enumerate(mesh_keys):
        gr2_bytes = entries[mesh_key]
        print(f"  Mesh entry: {mesh_key!r} ({len(gr2_bytes):,} bytes decompressed)")
        gr2_file, sdb_file, cur_fi, _keep = load_gr2(dll, gr2_bytes, sdb_bytes)
        _all_keeps.append((gr2_file, sdb_file, _keep))

        # Merge skeleton: first entry provides base, subsequent entries add missing bones
        entry_bones, entry_name_to_idx = read_skeleton(cur_fi)
        if bones is None:
            bones = list(entry_bones)
            bone_name_to_idx = dict(entry_name_to_idx)
            print(f"  Skeleton: {len(bones)} bones")
        else:
            added = 0
            for bone in entry_bones:
                if bone['name'] not in bone_name_to_idx:
                    # Remap parent index to the merged list
                    new_parent = -1
                    if bone['parent'] >= 0 and bone['parent'] < len(entry_bones):
                        parent_name = entry_bones[bone['parent']]['name']
                        new_parent = bone_name_to_idx.get(parent_name, -1)
                    new_bone = dict(bone, parent=new_parent)
                    bone_name_to_idx[bone['name']] = len(bones)
                    bones.append(new_bone)
                    added += 1
            if added:
                print(f"  Merged {added} new bone(s) from {mesh_key} (total: {len(bones)})")

        # Get per-mesh texture map for THIS entry
        entry_tex_map, _ = _read_mesh_texture_map(cur_fi, debug=args.debug)

        _meshes    = _t('granny_file_info', 'Meshes')
        mesh_count = ri(cur_fi, _meshes)
        meshes_arr = rq(cur_fi, _meshes + 4)

        mesh_indices = [args.mesh_index] if args.mesh_index is not None else list(range(mesh_count))

        for mi in mesh_indices:
            mesh_ptr = rq(meshes_arr, mi * 8)
            result = read_mesh_data(mesh_ptr, bone_name_to_idx, dll, debug=args.debug)
            if result is None:
                continue
            mesh_name, positions, normals, uvs, weights, joints, indices, bb_names = result
            if mesh_name in seen_names:
                seen_names[mesh_name] += 1
                mesh_name = f"{mesh_name}_{seen_names[mesh_name]}"
            else:
                seen_names[mesh_name] = 0
            mtype = "rigid" if joints is None else "skinned"
            print(f"  Mesh[{mi}] {mesh_name!r}: {len(positions)} verts, {len(indices)//3} tris  [{mtype}]")
            local_idx = len(mesh_data_list)
            mesh_data_list.append((positions, normals, uvs, weights, joints, indices))
            mesh_bb_names_list.append(bb_names)
            mesh_names.append(mesh_name)
            exported_gr2_indices.append({'entry': mesh_key, 'gr2_index': mi})
            # Map texture from this entry's texture map
            if mi in entry_tex_map:
                local_mesh_tex[local_idx] = entry_tex_map[mi]

    # ── Animations ──
    anim_data = None
    if args.animations or args.anim_filter:
        print("[5/6] Extracting animations")
        anim_data = extract_animations(dll, entries, sdb_bytes,
                                       anim_filter=args.anim_filter, dll_path=args.dll,
                                       anim_workers=args.anim_workers)
    else:
        print("[5/6] Animations: skipped (use --animations to include)")

    # ── Textures ──
    texture_map = None  # {tex_name: (png_bytes, [mesh_indices])}
    if args.textures:
        print("[*] Extracting 3D model textures", flush=True)
        pkg_dir = args.pkg_dir
        if pkg_dir is None:
            content_dir = os.path.dirname(os.path.dirname(os.path.abspath(args.gpk_dir)))
            pkg_dir = os.path.join(content_dir, "Packages", "1080p")

        if os.path.isdir(pkg_dir):
            try:
                # Collect unique texture names from the per-mesh mapping
                unique_tex_names = []
                for tn in local_mesh_tex.values():
                    if tn not in unique_tex_names:
                        unique_tex_names.append(tn)

                # Check Lua GrannyTexture overrides (game can replace texture at runtime)
                scripts_dir = os.path.join(os.path.dirname(os.path.dirname(args.gpk_dir)),
                                           "Scripts")
                lua_overrides = _load_granny_texture_overrides(scripts_dir)
                char_lower = args.name.lower()
                lua_variant_names = set()
                if char_lower in lua_overrides:
                    lua_tex = lua_overrides[char_lower]
                    print(f"  Lua GrannyTexture overrides (variants): {lua_tex}")
                    for lt in lua_tex:
                        if lt not in unique_tex_names:
                            unique_tex_names.append(lt)
                            lua_variant_names.add(lt)

                # Last resort: name-based guessing
                if not unique_tex_names:
                    cn = args.name.lower()
                    guesses = [f"{cn}transform_color", f"{cn}_color512", f"{cn}_color"]
                    unique_tex_names = guesses
                    print(f"  Name-based fallback: {guesses}")

                print(f"  Searching for textures: {unique_tex_names}")
                extracted = _extract_all_textures(pkg_dir, unique_tex_names)

                if extracted:
                    texture_map = {}
                    out_dir = os.path.dirname(out_path)
                    manifest_textures = {}
                    for tex_name, tex_info in extracted.items():
                        # Find which local meshes use this texture
                        indices_for_tex = [li for li, tn in local_mesh_tex.items()
                                           if tn == tex_name]
                        is_variant = tex_name in lua_variant_names

                        if is_variant:
                            # Lua variants (e.g. EM textures) — save as files
                            # but don't embed in GLB or assign to meshes
                            indices_for_tex = []
                        elif not indices_for_tex:
                            # No mesh mapping from material chain — assign to all body meshes
                            indices_for_tex = [i for i, mn in enumerate(mesh_names)
                                               if 'Outline' not in mn and 'Shadow' not in mn]

                        if indices_for_tex:
                            texture_map[tex_name] = (tex_info['png'], indices_for_tex)
                        # Save DDS + PNG alongside GLB
                        png_hash = hashlib.md5(tex_info['png']).hexdigest()
                        dds_filename = f"{tex_name}.dds"
                        with open(os.path.join(out_dir, dds_filename), 'wb') as f:
                            f.write(tex_info['dds'])
                        png_filename = f"{tex_name}.png"
                        with open(os.path.join(out_dir, png_filename), 'wb') as f:
                            f.write(tex_info['png'])
                        # Build manifest entry
                        tex_manifest = {
                            'dds_file': dds_filename,
                            'png_hash': png_hash,
                            'pkg': tex_info['pkg'],
                            'pkgs': tex_info.get('pkgs', [tex_info['pkg']]),
                            'pkg_entry_name': tex_info['pkg_entry_name'],
                            'format': tex_info['format'],
                            'format_name': tex_info['format_name'],
                            'width': tex_info['width'],
                            'height': tex_info['height'],
                            'pixel_size': tex_info['pixel_size'],
                            'mip_count': tex_info['mip_count'],
                            'mesh_names': [mesh_names[i] for i in indices_for_tex],
                        }
                        if is_variant:
                            tex_manifest['variant'] = True
                        manifest_textures[tex_name] = tex_manifest
                    n_tex = len(texture_map)
                    n_meshes = sum(len(v[1]) for v in texture_map.values())
                    print(f"  {n_tex} texture(s) embedded, assigned to {n_meshes} mesh(es)")
                else:
                    manifest_textures = {}
                    print(f"  No textures found for {args.name}")
            except Exception as e:
                print(f"  Texture extraction failed: {e}")
                traceback.print_exc()
        else:
            print(f"  Package directory not found: {pkg_dir}")

    step = "6/6" if (args.animations or args.anim_filter) else "5/5"
    print(f"[{step}] Building glTF -> {out_path}", flush=True)
    gltf = build_gltf(args.name, mesh_data_list, mesh_names, bones,
                       animations=anim_data, texture_map=texture_map)
    print("  Saving GLB file...", flush=True)
    gltf.save(out_path)
    anim_msg = f", {len(anim_data)} animations" if anim_data else ""
    tex_msg = ", textured" if texture_map else ""
    print(f"\nDone!  {len(mesh_data_list)} meshes, {len(bones)} bones{anim_msg}{tex_msg} -> {out_path}")

    # Write manifest for reimport
    manifest = build_manifest(
        character=args.name,
        out_basename=os.path.basename(out_path),
        mesh_entries=mesh_keys,
        mesh_names=mesh_names,
        exported_gr2_indices=exported_gr2_indices,
        mesh_data_list=mesh_data_list,
        mesh_bb_names_list=mesh_bb_names_list,
        manifest_textures=manifest_textures if (args.textures and 'manifest_textures' in dir()) else None,
        anim_data=anim_data,
    )
    manifest_path = os.path.join(os.path.dirname(out_path), 'manifest.json')
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)
    print(f"  Manifest: {manifest_path}")

    # Save baseline vertex positions for change detection at build time.
    # Keyed by mesh name so the build tool can compare per-mesh.
    baseline = {mn: md[0] for mn, md in zip(mesh_names, mesh_data_list)}
    baseline_path = os.path.join(os.path.dirname(out_path), '.baseline_positions.npz')
    np.savez_compressed(baseline_path, **baseline)
    print(f"  Baseline: {baseline_path}")

    for _gf, _sf, _kp in _all_keeps:
        dll.GrannyFreeFile(_gf)
        dll.GrannyFreeFile(_sf)


if __name__ == '__main__':
    # Required so worker processes spawned by multiprocessing.Pool inside a
    # PyInstaller-bundled exe don't each re-run main() from scratch — without
    # this, --animations on a character with >=20 anims fork-bombs the exe.
    multiprocessing.freeze_support()
    main()
