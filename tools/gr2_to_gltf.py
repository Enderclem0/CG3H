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
from pygltflib import GLTF2, Scene, Node, Mesh, Primitive, Skin, Accessor, BufferView, Buffer
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
    gr2_file = dll.GrannyReadEntireFileFromMemory(len(gr2_bytes), gr2_buf)
    if not gr2_file:
        raise RuntimeError("Failed to load GR2")
    if not dll.GrannyRemapFileStrings(gr2_file, str_db):
        raise RuntimeError("GrannyRemapFileStrings failed")
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
        # Skinned — remap local bone palette indices to global skeleton indices.
        palette_np               = np.array(palette, dtype=np.uint16)
        joints_global            = palette_np[raw_bj]
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

# ─────────────────────────── glTF export ─────────────────────────────────────

def build_gltf(character_name, mesh_data_list, mesh_names, bones):
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

        gltf.meshes.append(Mesh(
            name=mesh_name,
            primitives=[Primitive(attributes=attrs, indices=idx_acc)],
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

    combined = b''.join(all_buffers)
    gltf.buffers = [Buffer(byteLength=len(combined))]
    gltf.set_binary_blob(combined)
    return gltf

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
                        help="Include LOD duplicates (suffixed _LOD1, _LOD2…)")
    parser.add_argument("-o", "--output", default=None, help="Output .glb path")
    parser.add_argument("--debug", action="store_true",
                        help="Print per-mesh vertex layout and bone binding info")
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
            if not args.all_lods:
                print(f"  Mesh[{mi}] {mesh_name!r}: skipped (LOD duplicate; use --all-lods)")
                continue
            seen_names[mesh_name] += 1
            mesh_name = f"{mesh_name}_LOD{seen_names[mesh_name]}"
        else:
            seen_names[mesh_name] = 0
        mtype = "rigid" if joints is None else "skinned"
        print(f"  Mesh[{mi}] {mesh_name!r}: {len(positions)} verts, {len(indices)//3} tris  [{mtype}]")
        mesh_data_list.append((positions, normals, uvs, weights, joints, indices))
        mesh_names.append(mesh_name)

    print(f"[5/5] Building glTF -> {out_path}")
    gltf = build_gltf(args.name, mesh_data_list, mesh_names, bones)
    gltf.save(out_path)
    print(f"\nDone!  {len(mesh_data_list)} meshes, {len(bones)} bones -> {out_path}")

    dll.GrannyFreeFile(gr2_file)
    dll.GrannyFreeFile(sdb_file)


if __name__ == '__main__':
    main()
