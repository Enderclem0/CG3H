"""
Hades II glTF -> GR2 converter.

Takes a Blender-exported .glb and an original .gpk/.sdb pair, then produces
a patched .gpk the game can load.  Only same-topology modifications (vertex
position / normal / UV / weight changes) are supported; adding or removing
vertices or bones requires rebuilding the skeleton, which is out of scope.

Usage:
    cd "C:/Program Files (x86)/Steam/steamapps/common/Hades II/Ship"
    python C:/Users/ender/GhidraHades2/tools/gltf_to_gr2.py Melinoe.glb \\
        --gpk ../Content/GR2/_Optimized/Melinoe.gpk \\
        --sdb ../Content/GR2/_Optimized/Melinoe.sdb \\
        --output-gpk Melinoe_mod.gpk

Requires:
    pip install numpy pygltflib lz4
    granny2_x64.dll in cwd (run from Ship/)

Write-API (Golden Path — inline strings, valid section descriptors):
    GrannyGetFileInfoType()                                   -> Blueprint ptr
    GrannyBeginFile(SectionCount, 0, Magic)                   -> builder
    GrannyBeginFileDataTreeWriting(Blueprint, fi, 0, 0)       -> tree_writer  # 0 = inline
    GrannyWriteDataTreeToFileBuilder(tree_writer, builder)    -> bool
    GrannyEndFileDataTreeWriting(tree_writer)                  # flush + free
    GrannyCreatePlatformFileWriter(path, 1)                   -> pw
    GrannyEndFileToWriter(builder, pw)                        -> bool
    vtable[0](pw)                                             # Cleanup

    param3=0 embeds all strings inline.  GrannyRemapFileStrings returns False
    (no-op — strings already live), but GrannyGetFileInfo returns valid data.
    The stripped path (param3=1) is NOT used because it produces broken section
    descriptors that crash GrannyRemapFileStrings.
"""

import argparse
import ctypes
import os
import struct
import sys
import tempfile

import numpy as np

try:
    import lz4.block
except ImportError:
    sys.exit("ERROR: lz4 not installed. Run: pip install lz4")
try:
    import pygltflib
except ImportError:
    sys.exit("ERROR: pygltflib not installed. Run: pip install pygltflib")

_tools_dir = os.path.dirname(os.path.abspath(__file__))
if _tools_dir not in sys.path:
    sys.path.insert(0, _tools_dir)

from granny_types import (
    build_type_map, get_struct_stride, get_vertex_layout,
    get_transform_field_offsets, setup_dll_types, _type_sym_addr,
    MTYPE_UINT8, MTYPE_NORMAL_U8,
)
from gpk_pack import extract_gpk, pack_gpk


# ── Module-level DLL state ────────────────────────────────────────────────────

_TYPES: dict = {}
_BONE_STRIDE: int = 0
_BB_STRIDE: int = 0
_TRANSFORM_OFFS: dict = {}

# ctypes function type for the string-stripping callback registered on tree_writer.
# The DLL calls this once per unique string encountered during tree serialization.
# Return value: uint32 SDB array index for that string.
STRING_CB = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p, ctypes.c_char_p)


# ── glTF helpers ──────────────────────────────────────────────────────────────

_COMPONENT_DTYPE = {
    5120: np.int8, 5121: np.uint8,
    5122: np.int16, 5123: np.uint16,
    5125: np.uint32, 5126: np.float32,
}
_TYPE_N = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT4': 16}


def _accessor_to_numpy(gltf, blob, acc_idx) -> np.ndarray:
    acc   = gltf.accessors[acc_idx]
    bv    = gltf.bufferViews[acc.bufferView]
    start = (bv.byteOffset or 0) + (acc.byteOffset or 0)
    dtype = _COMPONENT_DTYPE[acc.componentType]
    n     = _TYPE_N[acc.type]
    nbytes = np.dtype(dtype).itemsize
    raw   = blob[start: start + acc.count * n * nbytes]
    return np.frombuffer(raw, dtype=dtype).reshape(acc.count, n).copy()


def parse_glb(glb_path: str) -> list[dict]:
    """
    Parse a .glb and return a list of mesh dicts, one per mesh primitive.

    Each dict has:
        name        : str
        positions   : (V,3) float32
        normals     : (V,3) float32  (or None)
        uvs         : (V,2) float32  (or None)
        bw_u8       : (V,4) uint8    (bone weights, or None if rigid)
        bj_u8       : (V,4) uint8    (bone indices as local palette, or None)
        indices     : (I,)  uint16
        bone_palette: [str, ...]     (joint names, or None if rigid)
    """
    gltf = pygltflib.GLTF2().load(glb_path)
    blob = bytes(gltf.binary_blob())

    meshes_out = []

    # Build a skin joint -> name mapping if there is a skin
    skin_joint_names = []
    if gltf.skins:
        skin = gltf.skins[0]
        skin_joint_names = [gltf.nodes[ni].name for ni in skin.joints]

    for gltf_mesh in gltf.meshes:
        for prim in gltf_mesh.primitives:
            attrs = prim.attributes

            if attrs.POSITION is None:
                continue

            positions = _accessor_to_numpy(gltf, blob, attrs.POSITION).astype(np.float32)
            V = len(positions)

            normals = None
            if attrs.NORMAL is not None:
                normals = _accessor_to_numpy(gltf, blob, attrs.NORMAL).astype(np.float32)

            uvs = None
            if attrs.TEXCOORD_0 is not None:
                uvs = _accessor_to_numpy(gltf, blob, attrs.TEXCOORD_0).astype(np.float32)

            indices = _accessor_to_numpy(gltf, blob, prim.indices).flatten().astype(np.uint16)

            bw_u8 = bj_u8 = bone_palette = None
            if attrs.JOINTS_0 is not None and attrs.WEIGHTS_0 is not None:
                joints_raw  = _accessor_to_numpy(gltf, blob, attrs.JOINTS_0)   # (V,4)
                weights_raw = _accessor_to_numpy(gltf, blob, attrs.WEIGHTS_0)  # (V,4) float
                # glTF weights are normalized [0,1] -> convert to uint8 sum≈255
                bw_u8 = np.clip(weights_raw * 255.0 + 0.5, 0, 255).astype(np.uint8)
                bj_u8 = joints_raw[:, :4].astype(np.uint8)
                max_local = int(joints_raw.max()) + 1
                bone_palette = skin_joint_names[:max_local] if skin_joint_names else None

            meshes_out.append({
                'name':         gltf_mesh.name,
                'positions':    positions,
                'normals':      normals,   # None → preserve originals from GR2
                'uvs':          uvs if uvs is not None else np.zeros((V, 2), np.float32),
                'has_normals':  normals is not None,
                'bw_u8':        bw_u8,
                'bj_u8':        bj_u8,
                'indices':      indices,
                'bone_palette': bone_palette,
            })

    return meshes_out


# ── Vertex buffer builder ─────────────────────────────────────────────────────

def build_vertex_buffer_40(m: dict, fallback_normals: np.ndarray = None) -> bytes:
    """
    Pack mesh data into the 40-byte physical vertex layout used by Hades II:
        +0   Position    float32×3  (12)
        +12  BoneWeights uint8×4    (4)   — zero for rigid meshes
        +16  BoneIndices uint8×4    (4)   — zero for rigid meshes
        +20  Normal      float32×3  (12)
        +32  UV          float32×2  (8)

    If m['normals'] is None and fallback_normals is provided, those are used.
    This happens when the GLB was exported without normals (Blender → Normals OFF),
    which avoids vertex-splitting at normal seams and keeps the vertex count stable.
    """
    pos = m['positions']
    uvs = m['uvs']
    V   = len(pos)

    nrm = m['normals']
    if nrm is None:
        nrm = fallback_normals if fallback_normals is not None else np.zeros((V, 3), np.float32)

    buf = np.zeros((V, 40), dtype=np.uint8)
    buf[:, 0:12]  = pos.astype('<f4').view(np.uint8).reshape(V, 12)
    if m['bw_u8'] is not None:
        buf[:, 12:16] = m['bw_u8'][:, :4]
    if m['bj_u8'] is not None:
        buf[:, 16:20] = m['bj_u8'][:, :4]
    buf[:, 20:32] = nrm.astype('<f4').view(np.uint8).reshape(V, 12)
    buf[:, 32:40] = uvs.astype('<f4').view(np.uint8).reshape(V, 8)
    return buf.tobytes()


# ── Granny DLL setup ──────────────────────────────────────────────────────────

_kernel32 = ctypes.windll.kernel32


def _valid_ptr(p):
    return isinstance(p, int) and 0x10000 <= p <= 0x7FFFFFFFFFFF


def _readable(ptr, size):
    return _valid_ptr(ptr) and not _kernel32.IsBadReadPtr(ctypes.c_void_p(ptr), ctypes.c_size_t(size))


def rq(addr, off):
    return struct.unpack_from('<Q', (ctypes.c_uint8 * 8).from_address(addr + off), 0)[0]


def ri(addr, off):
    return struct.unpack_from('<i', (ctypes.c_uint8 * 4).from_address(addr + off), 0)[0]


def safe_bytes(addr, n):
    return bytes((ctypes.c_uint8 * n).from_address(addr))


def read_cstr(ptr):
    if not _valid_ptr(ptr):
        return ''
    buf = (ctypes.c_uint8 * 256).from_address(ptr)
    b = bytes(buf)
    return b[:b.index(0)].decode('utf-8', 'replace') if 0 in b else ''


def _t(struct_name, field_name) -> int:
    try:
        return _TYPES[struct_name][field_name]
    except KeyError:
        raise KeyError(f"'{struct_name}.{field_name}' not in type map")


def setup_granny(dll_path: str):
    global _TYPES, _BONE_STRIDE, _BB_STRIDE, _TRANSFORM_OFFS

    dll = ctypes.CDLL(dll_path)

    dll.GrannyReadEntireFileFromMemory.restype  = ctypes.c_void_p
    dll.GrannyReadEntireFileFromMemory.argtypes = [ctypes.c_int, ctypes.c_void_p]
    dll.GrannyGetStringDatabase.restype  = ctypes.c_void_p
    dll.GrannyGetStringDatabase.argtypes = [ctypes.c_void_p]
    dll.GrannyRemapFileStrings.restype   = ctypes.c_bool
    dll.GrannyRemapFileStrings.argtypes  = [ctypes.c_void_p, ctypes.c_void_p]
    dll.GrannyGetFileInfo.restype  = ctypes.c_void_p
    dll.GrannyGetFileInfo.argtypes = [ctypes.c_void_p]
    dll.GrannyFreeFile.restype  = None
    dll.GrannyFreeFile.argtypes = [ctypes.c_void_p]
    dll.GrannyGetTotalObjectSize.restype  = ctypes.c_int
    dll.GrannyGetTotalObjectSize.argtypes = [ctypes.c_void_p]

    # Golden Path write API (confirmed via probe_golden_path.py)
    dll.GrannyGetFileInfoType.restype  = ctypes.c_uint64
    dll.GrannyGetFileInfoType.argtypes = []
    dll.GrannyBeginFile.restype  = ctypes.c_uint64
    dll.GrannyBeginFile.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    dll.GrannyBeginFileDataTreeWriting.restype  = ctypes.c_uint64
    dll.GrannyBeginFileDataTreeWriting.argtypes = [ctypes.c_uint64, ctypes.c_uint64,
                                                    ctypes.c_int, ctypes.c_uint64]
    dll.GrannyWriteDataTreeToFileBuilder.restype  = ctypes.c_bool
    dll.GrannyWriteDataTreeToFileBuilder.argtypes = [ctypes.c_uint64, ctypes.c_uint64]
    dll.GrannyEndFileDataTreeWriting.restype  = None
    dll.GrannyEndFileDataTreeWriting.argtypes = [ctypes.c_uint64]
    dll.GrannyCreatePlatformFileWriter.restype  = ctypes.c_uint64
    dll.GrannyCreatePlatformFileWriter.argtypes = [ctypes.c_char_p, ctypes.c_int]
    dll.GrannyEndFileToWriter.restype  = ctypes.c_bool
    dll.GrannyEndFileToWriter.argtypes = [ctypes.c_uint64, ctypes.c_uint64]
    # Stamps the paired SDB CRC into the file header so the game knows which .sdb
    # to load for GrannyRemapFileStrings.  Called after GrannyBeginFile.
    dll.GrannySetFileStringDatabaseCRC.restype  = None
    dll.GrannySetFileStringDatabaseCRC.argtypes = [ctypes.c_uint64, ctypes.c_uint32]
    # Registers a Python callback on the tree_writer.  Called once per unique string
    # encountered during GrannyWriteDataTreeToFileBuilder.  The callback returns the
    # SDB array index for that string; the DLL writes it as the string field value.
    # Signature: callback(ctx: void*, string: char*) -> uint32
    dll.GrannySetFileWriterStringCallback.restype  = None
    dll.GrannySetFileWriterStringCallback.argtypes = [ctypes.c_uint64, ctypes.c_void_p,
                                                       ctypes.c_void_p]

    setup_dll_types(dll)

    print("  Building Granny type map ...")
    _TYPES          = build_type_map(dll)
    _BONE_STRIDE    = get_struct_stride(dll, 'GrannyBoneType')
    _BB_STRIDE      = get_struct_stride(dll, 'GrannyBoneBindingType')
    _TRANSFORM_OFFS = get_transform_field_offsets(dll)
    print(f"  Type map OK  (bone={_BONE_STRIDE}B, binding={_BB_STRIDE}B)")
    return dll


def load_gr2(dll, gr2_bytes: bytes, sdb_bytes: bytes):
    """
    Load a stripped GR2 + SDB pair via the DLL.

    Always calls GrannyRemapFileStrings so string fields become live RAM pointers
    — required for the Python String Callback path (the DLL walker passes each
    live pointer to the callback as a char*).

    Also extracts sdb_dict: {string_value: sdb_index} by reading the str_db
    object directly from memory.  The callback uses this to reverse-map each
    string back to its SDB array index during serialization.

    Returns (gr2_file, sdb_file, fi, sdb_dict, keep_alive_tuple).
    keep_alive_tuple must be retained by the caller until GrannyFreeFile() is called.
    """
    sdb_buf = ctypes.create_string_buffer(sdb_bytes)
    gr2_buf = ctypes.create_string_buffer(gr2_bytes)
    sdb_file = dll.GrannyReadEntireFileFromMemory(len(sdb_bytes), sdb_buf)
    if not sdb_file:
        raise RuntimeError("GrannyReadEntireFileFromMemory failed for SDB")
    str_db = dll.GrannyGetStringDatabase(sdb_file)
    if not str_db:
        raise RuntimeError("GrannyGetStringDatabase failed")

    # Build reverse-map {string: index} directly from the str_db object.
    # str_db layout (probed from live DLL memory):
    #   +0x00  count       uint32   — number of strings in the database
    #   +0x04  array_ptr   uint64   — pointer to array of char* pointers
    #          (packed: uint64 immediately follows uint32, misaligned)
    sdb_count     = ctypes.c_uint32.from_address(str_db).value
    sdb_array_ptr = ctypes.c_uint64.from_address(str_db + 4).value
    sdb_dict: dict[str, int] = {}
    for i in range(sdb_count):
        str_ptr = ctypes.c_uint64.from_address(sdb_array_ptr + i * 8).value
        if str_ptr:
            s = ctypes.string_at(str_ptr).decode('utf-8', 'replace')
            sdb_dict[s] = i

    # Read the real SDB CRC from str_db at +0x0C.
    # Decompilation of GrannyRemapFileStrings: iVar1 == *(int *)(param_2 + 0xc)
    # where param_2 is str_db (GrannyGetStringDatabase result), NOT sdb_file.
    # str_db layout: [count:u32 +0x00][array_ptr:u64 +0x04][crc:u32 +0x0C]
    sdb_crc = struct.unpack_from('<I', (ctypes.c_uint8 * 4).from_address(str_db + 0x0c), 0)[0]

    gr2_file = dll.GrannyReadEntireFileFromMemory(len(gr2_bytes), gr2_buf)
    if not gr2_file:
        raise RuntimeError("GrannyReadEntireFileFromMemory failed for GR2")
    # GrannyRemapFileStrings returns False for golden-path files (inline strings,
    # no SDB indices to remap). This is normal — strings are already live pointers.
    dll.GrannyRemapFileStrings(gr2_file, str_db)
    fi = dll.GrannyGetFileInfo(gr2_file)
    if not fi:
        raise RuntimeError("GrannyGetFileInfo returned null")
    return gr2_file, sdb_file, fi, sdb_dict, sdb_crc, (sdb_buf, gr2_buf)



# ── GR2 mesh walk ─────────────────────────────────────────────────────────────

def get_gr2_meshes(fi) -> list[dict]:
    """
    Walk the loaded granny_file_info and return a list of mesh dicts:
        name       : str
        vp         : int  — address of vertex buffer in DLL memory
        vc         : int  — vertex count
        mesh_ptr   : int  — address of granny_mesh in DLL memory
        bb_count   : int  — number of bone binding entries
        bb_ptr     : int  — address of bone binding array
    """
    _meshes    = _t('granny_file_info', 'Meshes')
    mesh_count = ri(fi, _meshes)
    meshes_arr = rq(fi, _meshes + 4)

    _off_name = _t('granny_mesh', 'Name')
    _off_vd   = _t('granny_mesh', 'PrimaryVertexData')
    _off_bb   = _t('granny_mesh', 'BoneBindings')
    _verts    = _t('granny_vertex_data', 'Vertices')

    result = []
    for mi in range(mesh_count):
        mesh_ptr  = rq(meshes_arr, mi * 8)
        mesh_name = read_cstr(rq(mesh_ptr, _off_name))

        if 'ShadowMesh' in mesh_name:
            continue

        vd = rq(mesh_ptr, _off_vd)
        if not _valid_ptr(vd):
            continue

        # granny_vertex_data.Vertices is ReferenceToVariantArray:
        #   +0x00 type*(8), +0x08 count(4), +0x0C ptr(8)
        vertex_type_ptr = rq(vd, _verts)
        vc              = ri(vd, _verts + 8)
        vp              = rq(vd, _verts + 12)

        if not _valid_ptr(vp) or vc <= 0:
            continue

        # BoneBindings is ReferenceToArray: {count[4], ptr[8]}
        bb_count = ri(mesh_ptr, _off_bb)
        bb_ptr   = rq(mesh_ptr, _off_bb + 4)

        result.append({
            'name':             mesh_name,
            'vp':               vp,
            'vc':               vc,
            'vertex_type_ptr':  vertex_type_ptr,
            'mesh_ptr':         mesh_ptr,
            'bb_count':         bb_count,
            'bb_ptr':           bb_ptr,
        })
    return result


# ── GR2 serializer ────────────────────────────────────────────────────────────

def build_gr2_bytes(dll, fi: int, gr2_raw: bytes, sdb_dict: dict[str, int],
                    sdb_crc: int) -> bytes:
    """
    Serialize a loaded granny_file_info to raw .gr2 bytes using the golden path
    (param3=0, inline strings).

    The stripped path (param3=1) produces broken section descriptors that crash
    GrannyRemapFileStrings.  The golden path writes inline strings and produces
    valid section descriptors.  GrannyRemapFileStrings returns False (no-op since
    strings are already embedded), and GrannyGetFileInfo returns valid data.

    Pipeline:
      GrannyBeginFile
      →  GrannyBeginFileDataTreeWriting(Blueprint, fi, 0, 0)   # 0 = inline strings
      →  GrannyWriteDataTreeToFileBuilder
      →  GrannyEndFileDataTreeWriting
      →  GrannyCreatePlatformFileWriter  →  GrannyEndFileToWriter  →  vtable[0]
    """
    root_type_def = dll.GrannyGetFileInfoType()
    if not root_type_def:
        raise RuntimeError("GrannyGetFileInfoType returned null")

    section_count = struct.unpack_from('<I', gr2_raw, 0x20)[0]

    magic_buf = ctypes.create_string_buffer(gr2_raw[:32], 32)
    builder = dll.GrannyBeginFile(
        ctypes.c_int(section_count),
        ctypes.c_int(0),
        ctypes.cast(magic_buf, ctypes.c_void_p),
    )
    if not builder:
        raise RuntimeError("GrannyBeginFile returned null")

    # Golden path: param3=0 = inline strings, no string callback needed.
    tree_writer = dll.GrannyBeginFileDataTreeWriting(
        ctypes.c_uint64(root_type_def), ctypes.c_uint64(fi),
        ctypes.c_int(0), ctypes.c_uint64(0))
    if not tree_writer:
        raise RuntimeError("GrannyBeginFileDataTreeWriting returned null")

    ok = dll.GrannyWriteDataTreeToFileBuilder(
        ctypes.c_uint64(tree_writer), ctypes.c_uint64(builder))
    if not ok:
        raise RuntimeError("GrannyWriteDataTreeToFileBuilder returned False")

    dll.GrannyEndFileDataTreeWriting(ctypes.c_uint64(tree_writer))

    tmp = tempfile.mktemp(suffix='.gr2')
    pw = dll.GrannyCreatePlatformFileWriter(tmp.encode(), ctypes.c_int(1))
    if not pw:
        raise RuntimeError("GrannyCreatePlatformFileWriter returned null")

    ok2 = dll.GrannyEndFileToWriter(ctypes.c_uint64(builder), ctypes.c_uint64(pw))
    if not ok2:
        raise RuntimeError("GrannyEndFileToWriter returned False")

    # Finalize via vtable[0] = FUN_18000f6a0 (Ghidra-confirmed flat dispatch table):
    #   flush 1KB internal write buffer → WriteFile → CloseHandle → free struct.
    cleanup_ptr = struct.unpack_from('<Q', (ctypes.c_uint8 * 8).from_address(pw), 0)[0]
    cleanup_fn  = ctypes.CFUNCTYPE(None, ctypes.c_uint64)(cleanup_ptr)
    cleanup_fn(ctypes.c_uint64(pw))

    result = open(tmp, 'rb').read()
    os.unlink(tmp)
    return result


# ── Mesh matching and patching ────────────────────────────────────────────────

# Known character-variant words that appear in GR2 mesh names but not in the
# exported GLB (or vice-versa), e.g. Melinoe_Mesh vs MelinoeOverlook_Mesh.
_VARIANT_WORDS = ('overlook', 'overview', 'overworld', 'portrait')


def _normalize_mesh_name(name: str) -> str:
    """
    Strip rig prefix, LOD suffixes, and path separators for matching.
    'Melinoe_Rig:MelinoeOverlook_MeshShape_LOD1' -> 'melinoeoverlook_meshshape'
    """
    # Drop 'Armature:' / 'Rig:' style prefixes Blender adds on export
    n = name.split(':')[-1].split('|')[-1].split('/')[-1]
    # Strip LOD suffixes
    for suffix in ('_LOD1', '_LOD2', '_LOD3', '_lod1', '_lod2', '_lod3'):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
            break
    return n.lower()


def _strip_variants(s: str) -> str:
    """Remove known variant words from a normalised mesh name."""
    for w in _VARIANT_WORDS:
        s = s.replace(w, '')
    return s


def _best_name_match(glb_key: str, gr2_by_name: dict) -> str | None:
    """
    Return the best-matching key in gr2_by_name for glb_key, or None.

    Pass 1 — exact normalised match (already done by caller, included for clarity).
    Pass 2 — strip variant words from both sides, then compare.
    Pass 3 — substring containment (shorter inside longer), min 60 % ratio.
    """
    glb_stripped = _strip_variants(glb_key)

    # Pass 2: variant-stripped equality
    for gr2_key in gr2_by_name:
        if _strip_variants(gr2_key) == glb_stripped:
            return gr2_key

    # Pass 3: longest contained substring
    best_ratio, best_key = 0.0, None
    for gr2_key in gr2_by_name:
        shorter, longer = sorted([glb_key, gr2_key], key=len)
        if shorter and shorter in longer:
            ratio = len(shorter) / len(longer)
            if ratio > best_ratio:
                best_ratio, best_key = ratio, gr2_key
    if best_ratio >= 0.6:
        return best_key

    return None


def _unique_gr2_meshes(gr2_mesh_list: list) -> list:
    """Return one entry per unique normalised name (first = highest LOD)."""
    seen: set = set()
    out = []
    for gm in gr2_mesh_list:
        k = _normalize_mesh_name(gm['name'])
        if k not in seen:
            seen.add(k)
            out.append(gm)
    return out


def _update_bone_obbs(gm: dict) -> None:
    """
    Recompute OBB (Oriented Bounding Box) min/max for each bone binding
    after vertex positions have been patched.

    Without this, the game's frustum culler uses stale bounds and clips
    the mesh when vertices move beyond the original silhouette.

    granny_bone_binding layout (stride = _BB_STRIDE = 44):
        +0x00  BoneName*      (8 bytes)
        +0x08  OBBMin         float32x3
        +0x14  OBBMax         float32x3
        +0x20  TriangleCount  (4 bytes)
        +0x24  TriangleIndices* (8 bytes)
    """
    bb_count = gm['bb_count']
    bb_ptr   = gm['bb_ptr']
    vc       = gm['vc']
    vp       = gm['vp']

    if bb_count <= 0 or not _valid_ptr(bb_ptr):
        return

    # Read current vertex buffer: positions (+0:12) and bone indices (+16:20)
    raw = np.frombuffer(
        bytes((ctypes.c_uint8 * (vc * 40)).from_address(vp)),
        dtype=np.uint8,
    ).reshape(vc, 40)
    positions = raw[:, 0:12].copy().view(np.float32).reshape(vc, 3)
    bone_indices = raw[:, 16:20].copy()  # uint8 x 4
    bone_weights = raw[:, 12:16].copy()  # uint8 x 4

    # Add padding to prevent exact-boundary culling
    PADDING = 0.1

    for bi in range(bb_count):
        entry = bb_ptr + bi * _BB_STRIDE

        if not _readable(entry, _BB_STRIDE):
            continue

        # Find vertices influenced by this bone binding index
        # A vertex is influenced if any of its 4 bone indices == bi AND
        # the corresponding weight is > 0
        mask = np.zeros(vc, dtype=bool)
        for j in range(4):
            mask |= (bone_indices[:, j] == bi) & (bone_weights[:, j] > 0)

        if not mask.any():
            # Rigid mesh or no vertices for this binding — use all vertices
            if bb_count == 1:
                mask = np.ones(vc, dtype=bool)
            else:
                continue

        bound_pos = positions[mask]
        obb_min = bound_pos.min(axis=0) - PADDING
        obb_max = bound_pos.max(axis=0) + PADDING

        # Write OBBMin at +0x08, OBBMax at +0x14
        obb_buf = (ctypes.c_uint8 * 24).from_address(entry + 0x08)
        struct.pack_into('<3f', obb_buf, 0,  obb_min[0], obb_min[1], obb_min[2])
        struct.pack_into('<3f', obb_buf, 12, obb_max[0], obb_max[1], obb_max[2])


def _apply_patch(glb_m: dict, gm: dict, strict: bool) -> bool:
    """
    Patch one mesh's vertex buffer in DLL-managed memory, then update
    the per-bone bounding boxes so frustum culling works correctly.

    Returns True if the patch was applied, False if skipped.
    """
    vc_glb = len(glb_m['positions'])
    vc_gr2 = gm['vc']
    if vc_glb != vc_gr2:
        msg = (f"Mesh {glb_m['name']!r} -> {gm['name']!r}: vertex count mismatch "
               f"(GLB={vc_glb}, GR2={vc_gr2}).\n"
               f"    Same-topology edits only — vertex count must be identical.\n"
               f"    Common causes: Subdivide, Decimate, Merge by Distance, or\n"
               f"    exporting with Normals ON (splits vertices at seams).\n"
               f"    Re-export from Blender with Normals OFF and no topology changes.")
        if strict:
            raise ValueError(msg)
        print(f"  WARNING: {msg} — skipping")
        return False

    # Read the original vertex buffer from GR2 memory once.
    # Bone indices MUST always come from the GR2, not the GLB.  Blender reorders
    # the skin joint list on import/re-export, so JOINTS_0 values in the returned
    # GLB are in Blender's order, not the original palette order.
    orig_raw = np.frombuffer(
        bytes((ctypes.c_uint8 * (vc_gr2 * 40)).from_address(gm['vp'])),
        dtype=np.uint8,
    ).reshape(vc_gr2, 40)

    fallback_nrm = None
    if not glb_m.get('has_normals', True):
        fallback_nrm = orig_raw[:, 20:32].view(np.float32).reshape(vc_gr2, 3).copy()
        print(f"  [{gm['name']!r}] normals not in GLB — preserving originals from GR2")

    # Always preserve bone weights (+12) and bone indices (+16) from the original.
    glb_m = dict(glb_m)
    glb_m['bw_u8'] = orig_raw[:, 12:16].copy()
    glb_m['bj_u8'] = orig_raw[:, 16:20].copy()

    vbuf = build_vertex_buffer_40(glb_m, fallback_normals=fallback_nrm)
    expected = vc_gr2 * 40
    if len(vbuf) != expected:
        print(f"  WARNING: {glb_m['name']!r}: buffer size mismatch — skipping")
        return False

    ctypes.memmove(gm['vp'], vbuf, expected)

    # Recompute per-bone bounding boxes so the game doesn't cull the mesh
    _update_bone_obbs(gm)

    print(f"  Patched {gm['name']!r}: {vc_gr2} verts x 40 bytes (OBB updated)")
    return True


def patch_vertex_data(
    dll, fi: int, glb_meshes: list[dict],
    strict: bool = False,
    positional: bool = False,
) -> int:
    """
    Match each GLB mesh to the corresponding GR2 mesh, then patch the vertex
    buffer in DLL-managed memory using ctypes.memmove.

    Matching order (unless positional=True):
      1. Exact normalised name
      2. Variant-word-stripped name (e.g. Overlook / Overview stripped)
      3. Longest contained substring (>=60 % overlap)

    positional=True: pair GLB mesh[i] to unique GR2 mesh[i] by index.

    A GPK entry contains multiple LODs under the same mesh name.  We build a
    multi-map and patch every LOD whose vertex count matches the GLB mesh.
    LODs with a different count are silently skipped (expected behaviour).

    Returns n_patched (int).
    """
    gr2_mesh_list = get_gr2_meshes(fi)
    patched = 0

    # Print mesh summary for diagnostics
    glb_summary = ", ".join(f"{m['name']}({len(m['positions'])}v)" for m in glb_meshes)
    gr2_summary = ", ".join(f"{m['name']}({m['vc']}v)" for m in gr2_mesh_list)
    print(f"  GLB: {glb_summary}")
    print(f"  GR2: {gr2_summary}")

    if positional:
        unique_gr2 = _unique_gr2_meshes(gr2_mesh_list)
        if len(glb_meshes) > len(unique_gr2):
            print(f"  WARNING: GLB has {len(glb_meshes)} meshes but GR2 has only "
                  f"{len(unique_gr2)} unique meshes — extra GLB meshes ignored")
        for glb_m, gm in zip(glb_meshes, unique_gr2):
            print(f"  Positional match: {glb_m['name']!r} -> {gm['name']!r}")
            norm_key = _normalize_mesh_name(gm['name'])
            for candidate in gr2_mesh_list:
                if _normalize_mesh_name(candidate['name']) == norm_key:
                    if _apply_patch(glb_m, candidate, strict):
                        patched += 1
        return patched

    # Build multi-map: normalised name -> all GR2 meshes (all LODs)
    gr2_by_name: dict[str, list] = {}
    for gm in gr2_mesh_list:
        key = _normalize_mesh_name(gm['name'])
        gr2_by_name.setdefault(key, []).append(gm)

    for glb_m in glb_meshes:
        glb_key = _normalize_mesh_name(glb_m['name'])
        vc_glb  = len(glb_m['positions'])

        # Pass 1: exact
        matched_key = glb_key if glb_key in gr2_by_name else None

        # Passes 2+3: fuzzy
        if matched_key is None:
            matched_key = _best_name_match(glb_key, gr2_by_name)
            if matched_key is not None:
                rep = gr2_by_name[matched_key][0]['name']
                print(f"  Fuzzy match: {glb_m['name']!r} -> {rep!r}")

        if matched_key is None:
            print(f"  WARNING: GLB mesh {glb_m['name']!r} has no matching GR2 mesh — skipping")
            print(f"    GLB key  : {glb_key!r}")
            print(f"    Available: {sorted(gr2_by_name)}")
            print(f"    Tip: use --positional to match by index instead of name")
            continue

        # Patch every LOD whose vertex count matches; skip mismatches silently
        hit = False
        for gm in gr2_by_name[matched_key]:
            if gm['vc'] == vc_glb:
                if _apply_patch(glb_m, gm, strict):
                    patched += 1
                    hit = True
        if not hit:
            counts = [gm['vc'] for gm in gr2_by_name[matched_key]]
            msg = (f"Mesh {glb_m['name']!r}: no GR2 LOD has {vc_glb} verts "
                   f"(GR2 LOD counts: {counts}). "
                   f"Same-topology edits only — vertex count must be identical.")
            if strict:
                raise ValueError(msg)
            print(f"  WARNING: {msg} — skipping")

    return patched



# ── Main pipeline ─────────────────────────────────────────────────────────────

def convert(
    glb_path: str,
    gpk_path: str,
    sdb_path: str,
    dll_path: str,
    output_gpk: str,
    output_gr2=None,
    entry_name=None,
    strict: bool = False,
    positional: bool = False,
) -> None:
    print(f"[1/6] Parsing GLB: {glb_path}")
    glb_meshes = parse_glb(glb_path)
    print(f"  Found {len(glb_meshes)} mesh(es) in GLB")

    print(f"[2/6] Extracting GPK: {gpk_path}")
    gpk_entries = extract_gpk(gpk_path)
    if entry_name is None:
        candidates = [k for k in gpk_entries if k.endswith('_Mesh')]
        if not candidates:
            candidates = list(gpk_entries.keys())
        # Mirror the exporter: prefer <gpk-basename>_Mesh before falling back
        gpk_stem = os.path.splitext(os.path.basename(gpk_path))[0]
        preferred = f"{gpk_stem}_Mesh"
        if preferred in candidates:
            entry_name = preferred
        else:
            entry_name = max(candidates, key=lambda k: len(gpk_entries[k]))
    if entry_name not in gpk_entries:
        raise KeyError(f"Entry {entry_name!r} not in GPK. Available: {sorted(gpk_entries)}")
    gr2_bytes = gpk_entries[entry_name]
    print(f"  Entry {entry_name!r}: {len(gr2_bytes):,} bytes")

    print(f"[3/6] Reading SDB: {sdb_path}")
    with open(sdb_path, 'rb') as f:
        sdb_bytes = f.read()

    print(f"[4/6] Loading Granny DLL + type map")
    dll = setup_granny(dll_path)

    # Single load with remap=True: GrannyRemapFileStrings converts SDB indices to
    # live RAM pointers.  The DLL walker needs valid string pointers to pass to
    # the Python string callback during serialization.  sdb_dict is built from
    # the str_db object in memory for callback lookups.
    gr2_file, sdb_file, fi, sdb_dict, sdb_crc, _keep = load_gr2(dll, gr2_bytes, sdb_bytes)
    print(f"  SDB: {len(sdb_dict)} strings indexed  CRC=0x{sdb_crc:08X}")

    print(f"[5/6] Patching vertex data")
    n_patched = patch_vertex_data(
        dll, fi, glb_meshes, strict=strict, positional=positional
    )
    if n_patched == 0:
        raise RuntimeError("No meshes were patched — nothing to write")

    print(f"[6/6] Serializing modified GR2")
    new_gr2_bytes = build_gr2_bytes(dll, fi, gr2_bytes, sdb_dict, sdb_crc)

    dll.GrannyFreeFile(gr2_file)
    dll.GrannyFreeFile(sdb_file)

    if output_gr2:
        with open(output_gr2, 'wb') as f:
            f.write(new_gr2_bytes)
        print(f"  GR2 written: {output_gr2!r} ({len(new_gr2_bytes):,} bytes)")

    # Replace the mesh entry in the GPK, keep all other entries unchanged
    gpk_entries[entry_name] = new_gr2_bytes
    pack_gpk(gpk_entries, output_gpk)
    print(f"\nDone!  {n_patched} mesh(es) patched -> {output_gpk!r}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert a Blender-exported .glb back into a Hades II .gpk"
    )
    parser.add_argument('glb', help='Modified .glb from Blender')
    parser.add_argument('--gpk', required=True, help='Original .gpk file')
    parser.add_argument('--sdb', required=True, help='Paired .sdb file')
    parser.add_argument('--dll', default='./granny2_x64.dll', help='granny2_x64.dll path')
    parser.add_argument('--entry-name', default=None,
                        help='GPK entry name to replace (default: auto-detect)')
    parser.add_argument('--output-gpk', default=None,
                        help='Output .gpk path (default: <original>_mod.gpk)')
    parser.add_argument('--output-gr2', default=None,
                        help='Also write raw .gr2 bytes to this path')
    parser.add_argument('--strict', action='store_true',
                        help='Abort on vertex-count mismatch instead of skipping')
    parser.add_argument('--positional', action='store_true',
                        help='Match meshes by index instead of name '
                             '(use when character variant names differ, e.g. '
                             'Melinoe_MeshShape vs MelinoeOverlook_MeshShape)')
    args = parser.parse_args()

    out_gpk = args.output_gpk or args.gpk.replace('.gpk', '_mod.gpk')
    if out_gpk == args.gpk:
        out_gpk = args.gpk.replace('.gpk', '_mod.gpk')

    convert(
        glb_path=args.glb,
        gpk_path=args.gpk,
        sdb_path=args.sdb,
        dll_path=args.dll,
        output_gpk=out_gpk,
        output_gr2=args.output_gr2,
        entry_name=args.entry_name,
        strict=args.strict,
        positional=args.positional,
    )


if __name__ == '__main__':
    main()
