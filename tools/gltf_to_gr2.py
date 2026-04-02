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

# Keep Python-allocated buffers alive until serialization completes.
# Without this, the GC would free them before GrannyWriteDataTreeToFileBuilder reads them.
_keepalive: list = []

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


def parse_glb_animations(glb_path: str) -> tuple:
    """
    Parse animation channels from a .glb file.
    Returns (animations_dict, hashes_dict):
      animations: {animation_name: {bone_name: {path: (times, values)}}}
      hashes: {animation_name: content_hash_from_export} (for diff detection)
    """
    gltf = pygltflib.GLTF2().load(glb_path)
    blob = bytes(gltf.binary_blob())

    if not gltf.animations:
        return {}, {}

    result = {}
    hashes = {}
    for anim in gltf.animations:
        tracks = {}
        for channel in anim.channels:
            node_idx = channel.target.node
            if node_idx is None or node_idx >= len(gltf.nodes):
                continue
            bone_name = gltf.nodes[node_idx].name
            path = channel.target.path

            sampler = anim.samplers[channel.sampler]
            times = _accessor_to_numpy(gltf, blob, sampler.input).flatten().astype(np.float32)
            values = _accessor_to_numpy(gltf, blob, sampler.output).astype(np.float32)

            if bone_name not in tracks:
                tracks[bone_name] = {}
            tracks[bone_name][path] = (times, values)

        if tracks:
            result[anim.name] = tracks
            # Extract content_hash from extras (stamped by our exporter)
            if anim.extras and isinstance(anim.extras, dict):
                h = anim.extras.get('content_hash')
                if h:
                    hashes[anim.name] = h

    return result, hashes


ANIM_TRACK_STRIDE = 60  # granny_transform_track: Name(8)+Flags(4)+3×Curve2(16)


def _build_dak32f_curve(knots, controls, degree=2):
    """
    Build a DaK32fC32f curve data struct in Python memory.
    Returns (curve_data_buf, knots_buf, controls_buf) — all must be kept alive.

    Layout at curve_data_buf:
      +0x00: Format(u8)=1, Degree(u8), Padding(u16)=0
      +0x04: Knots {count(i32), ptr(u64)}
      +0x10: Controls {count(i32), ptr(u64)}
    """
    knots_f32 = np.ascontiguousarray(knots, dtype=np.float32)
    ctrls_f32 = np.ascontiguousarray(controls.flatten(), dtype=np.float32)

    knots_buf = (ctypes.c_uint8 * knots_f32.nbytes)()
    ctypes.memmove(knots_buf, knots_f32.tobytes(), knots_f32.nbytes)

    ctrls_buf = (ctypes.c_uint8 * ctrls_f32.nbytes)()
    ctypes.memmove(ctrls_buf, ctrls_f32.tobytes(), ctrls_f32.nbytes)

    data_buf = (ctypes.c_uint8 * 28)()
    data_buf[0] = 1       # Format = DaK32fC32f
    data_buf[1] = degree   # Degree
    # +0x02: padding = 0 (already zero)
    struct.pack_into('<I', data_buf, 4, len(knots_f32))
    struct.pack_into('<Q', data_buf, 8, ctypes.addressof(knots_buf))
    struct.pack_into('<I', data_buf, 16, len(ctrls_f32))
    struct.pack_into('<Q', data_buf, 20, ctypes.addressof(ctrls_buf))

    return data_buf, knots_buf, ctrls_buf


def _patch_quantized_curve_inplace(dll, curve2_addr, data_ptr, new_values_xyzw):
    """
    Patch a DaK8uC8u (fmt=9) or DaK16uC16u (fmt=8) curve IN PLACE.

    Instead of allocating a new DaK32fC32f curve (which changes format and
    breaks SDB string remapping), this modifies the uint8/uint16 control bytes
    in the original KC buffer using the existing scale/offset.

    new_values_xyzw: float32 array (N, 4) of XYZW quaternions. Only XYZ is
    stored (W is derived). N must match the original knot count.

    Returns True if patched, False if format not supported or mismatch.
    """
    if not _valid_ptr(data_ptr):
        return False

    fmt = safe_bytes(data_ptr, 1)[0]
    if fmt not in (8, 9):
        return False

    is_16bit = (fmt == 8)
    dtype = np.uint16 if is_16bit else np.uint8
    max_val = 65535.0 if is_16bit else 255.0
    bytes_per = 2 if is_16bit else 1

    # Read KC {count, ptr} at data_ptr + 0x08
    kc_count = struct.unpack_from('<I', safe_bytes(data_ptr + 8, 4))[0]
    kc_ptr = struct.unpack_from('<Q', safe_bytes(data_ptr + 12, 8))[0]
    if not _valid_ptr(kc_ptr) or kc_count == 0:
        return False

    dim = 3  # quaternion stored as XYZ
    n_knots = kc_count // (dim + 1)
    if n_knots == 0 or n_knots != len(new_values_xyzw):
        return False

    # Read the original KC data
    kc_data = np.frombuffer(safe_bytes(kc_ptr, kc_count * bytes_per), dtype=dtype).copy()
    ctrl_start = n_knots  # controls start after knots
    orig_ctrls = kc_data[ctrl_start:].reshape(n_knots, dim)

    # Convert the original uint controls to float using the DLL converter
    # to get the exact scale/offset the original uses
    sz = dll.GrannyGetResultingDaK32fC32fCurveSize(ctypes.c_void_p(curve2_addr))
    if sz <= 0:
        return False
    buf = (ctypes.c_uint8 * sz)()
    ret = dll.GrannyCurveConvertToDaK32fC32f(
        ctypes.c_void_p(curve2_addr), ctypes.cast(buf, ctypes.c_void_p))
    if not ret:
        return False
    conv_data = rq(ret, 8)
    if not _valid_ptr(conv_data):
        dll.GrannyFreeCurve(ctypes.c_void_p(ret))
        return False
    conv_raw = safe_bytes(conv_data, 28)
    conv_kc = struct.unpack_from('<I', conv_raw, 4)[0]
    conv_cc = struct.unpack_from('<I', conv_raw, 16)[0]
    conv_cp = struct.unpack_from('<Q', conv_raw, 20)[0]
    if conv_kc != n_knots or not _valid_ptr(conv_cp):
        dll.GrannyFreeCurve(ctypes.c_void_p(ret))
        return False
    orig_floats = np.frombuffer(safe_bytes(conv_cp, conv_cc * 4),
                                dtype=np.float32).copy().reshape(conv_kc, 4)
    dll.GrannyFreeCurve(ctypes.c_void_p(ret))

    # Compute per-component scale and offset from original data
    # value = uint_ctrl * scale + offset → uint_ctrl = (value - offset) / scale
    new_ctrls = np.zeros((n_knots, dim), dtype=dtype)
    for comp in range(dim):
        orig_bytes = orig_ctrls[:, comp].astype(np.float64)
        orig_vals = orig_floats[:, comp].astype(np.float64)

        # Least-squares to find scale, offset
        A = np.column_stack([orig_bytes, np.ones_like(orig_bytes)])
        result = np.linalg.lstsq(A, orig_vals, rcond=None)
        scale, offset = result[0]

        if abs(scale) < 1e-15:
            new_ctrls[:, comp] = orig_ctrls[:, comp]
            continue

        # Encode new values (XYZ only, index comp from XYZW)
        new_vals = new_values_xyzw[:, comp].astype(np.float64)
        encoded = np.round((new_vals - offset) / scale)
        encoded = np.clip(encoded, 0, max_val)
        new_ctrls[:, comp] = encoded.astype(dtype)

    # Write new control bytes back to the original KC buffer
    new_kc = kc_data.copy()
    new_kc[ctrl_start:] = new_ctrls.flatten()
    ctypes.memmove(kc_ptr, new_kc.tobytes(), len(new_kc) * bytes_per)
    return True


def _fixup_quat_signs(values):
    """Ensure consecutive quaternions have positive dot product."""
    if len(values) < 2 or values.shape[-1] != 4:
        return
    flip = 1.0
    for i in range(len(values) - 1):
        if np.dot(values[i], values[i + 1] * flip) < 0:
            flip = -flip
        values[i + 1] *= flip


def patch_animation_entries(dll, gpk_entries, sdb_bytes, glb_animations,
                            anim_patch_filter=None):
    """
    Patch animation entries in the GPK using data from glTF animations.
    Only patches entries matching anim_patch_filter (required — prevents
    accidental re-encoding of all animations which causes rendering corruption).
    """
    if anim_patch_filter is None:
        print("  WARNING: --anim-patch-filter required to specify which animations to patch")
        return 0
    if not glb_animations:
        return 0

    # Resolve DaK32fC32f type pointer from DLL symbol
    try:
        sym_addr = ctypes.c_void_p.in_dll(dll, 'GrannyCurveDataDaK32fC32fType').value
        dak32f_type_ptr = sym_addr
    except (ValueError, AttributeError):
        print("  WARNING: Could not resolve GrannyCurveDataDaK32fC32fType — skipping animations")
        return 0

    sdb_buf = ctypes.create_string_buffer(sdb_bytes)
    sdb_file = dll.GrannyReadEntireFileFromMemory(len(sdb_bytes), sdb_buf)
    if not sdb_file:
        return 0
    str_db = dll.GrannyGetStringDatabase(sdb_file)

    patched = 0
    anim_entries = {k: v for k, v in gpk_entries.items() if not k.endswith('_Mesh')}

    # Filter entries to only those matching the patch filter
    pattern = anim_patch_filter.lower()
    anim_entries = {k: v for k, v in anim_entries.items() if pattern in k.lower()}
    print(f"  {len(anim_entries)} entries match filter '{anim_patch_filter}'", flush=True)

    # Build lookup tables for matching GLB animation names to GPK entries.
    # Primary: exact match by GPK entry name (from our exporter).
    # Fallback: normalized match stripping Blender suffixes (_Skin, .001).
    import re as _re
    def _norm_anim_name(name):
        n = name
        for suffix in ('_Melinoe_Skin', '_Armature', '_Skin'):
            if n.endswith(suffix):
                n = n[:-len(suffix)]
                break
        n = _re.sub(r'\.\d{3,}$', '', n)
        return n

    glb_by_exact = dict(glb_animations)  # exact name match
    glb_by_norm = {}                      # normalized fallback
    for glb_name, tracks in glb_animations.items():
        norm = _norm_anim_name(glb_name)
        if norm not in glb_by_norm:
            glb_by_norm[norm] = tracks

    total = len(anim_entries)
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

        # Match GLB animation: try GPK entry name (exact), then internal name, then normalized
        glb_tracks = (glb_by_exact.get(entry_name)
                      or glb_by_exact.get(anim_name)
                      or glb_by_norm.get(entry_name)
                      or glb_by_norm.get(anim_name)
                      or glb_by_norm.get(_norm_anim_name(anim_name)))
        if glb_tracks is None:
            dll.GrannyFreeFile(gr2_file)
            continue

        # Walk transform tracks and patch matching curves
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

        tracks_modified = 0
        needs_reserialize = False

        for ti in range(tt_count):
            t_base = tt_ptr + ti * ANIM_TRACK_STRIDE
            t_name = read_cstr(rq(t_base, 0))

            bone_data = glb_tracks.get(t_name)
            if bone_data is None:
                short = t_name.split(':')[-1]
                bone_data = glb_tracks.get(short)
            if bone_data is None:
                continue

            for path, coff, dim in [('rotation', 0x0C, 4),
                                     ('translation', 0x1C, 3),
                                     ('scale', 0x2C, 3)]:
                if path not in bone_data:
                    continue
                times, values = bone_data[path]
                if len(times) == 0:
                    continue

                if path == 'scale' and values.shape[-1] == 3:
                    mat_values = np.zeros((len(values), 9), dtype=np.float32)
                    mat_values[:, 0] = values[:, 0]
                    mat_values[:, 4] = values[:, 1]
                    mat_values[:, 8] = values[:, 2]
                    values = mat_values
                    dim = 9

                if path == 'rotation' and values.shape[-1] == 4:
                    _fixup_quat_signs(values)

                # Try in-place quantized patching first (preserves original format)
                if path == 'rotation' and values.shape[-1] == 4:
                    data_p = rq(t_base, coff + 8)
                    if _valid_ptr(data_p) and safe_bytes(data_p, 1)[0] in (8, 9):
                        if _patch_quantized_curve_inplace(dll, t_base + coff, data_p, values):
                            tracks_modified += 1
                            continue  # patched in-place, no reserialization needed

                # Fallback: allocate new DaK32fC32f curve
                data_buf, knots_buf, ctrls_buf = _build_dak32f_curve(times, values, degree=2)
                _keepalive.extend([data_buf, knots_buf, ctrls_buf])
                struct.pack_into('<Q', (ctypes.c_uint8 * 8).from_address(t_base + coff),
                                 0, dak32f_type_ptr)
                struct.pack_into('<Q', (ctypes.c_uint8 * 8).from_address(t_base + coff + 8),
                                 0, ctypes.addressof(data_buf))
                tracks_modified += 1
                needs_reserialize = True

        if tracks_modified > 0:
            # Only re-serialize if we used DaK32fC32f fallback (changed format).
            # In-place patches modify the existing buffer directly — serialize always
            # to capture the changes in the output GR2.
            new_gr2 = build_gr2_bytes(dll, fi, gr2_bytes, {}, 0)
            gpk_entries[entry_name] = new_gr2
            patched += 1
            print(f"    Patched: {entry_name}", flush=True)

        dll.GrannyFreeFile(gr2_file)

        if (idx + 1) % 25 == 0 or idx + 1 == total:
            print(f"    {idx+1}/{total} entries scanned ({patched} patched)", flush=True)

    dll.GrannyFreeFile(sdb_file)
    _keepalive.clear()
    return patched


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
    _off_topo = _t('granny_mesh', 'PrimaryTopology')
    _off_bb   = _t('granny_mesh', 'BoneBindings')
    _verts    = _t('granny_vertex_data', 'Vertices')
    _idx16    = _t('granny_tri_topology', 'Indices16')

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

        # PrimaryTopology -> Indices16 {count[4], ptr[8]}
        topo_ptr = rq(mesh_ptr, _off_topo)
        idx_count = 0
        idx_ptr   = 0
        if _valid_ptr(topo_ptr):
            idx_count = ri(topo_ptr, _idx16)       # number of uint16 index values
            idx_ptr   = rq(topo_ptr, _idx16 + 4)   # pointer to index array

        # BoneBindings is ReferenceToArray: {count[4], ptr[8]}
        bb_count = ri(mesh_ptr, _off_bb)
        bb_ptr   = rq(mesh_ptr, _off_bb + 4)

        # Read bone binding names for weight remapping
        _off_bb_name = _t('granny_bone_binding', 'BoneName')
        bb_names = []
        for bi in range(bb_count):
            entry = bb_ptr + bi * _BB_STRIDE
            if _valid_ptr(bb_ptr) and _readable(entry, 8):
                name_ptr = rq(entry, _off_bb_name)
                bb_names.append(read_cstr(name_ptr) if _valid_ptr(name_ptr) else '')
            else:
                bb_names.append('')

        result.append({
            'name':             mesh_name,
            'vp':               vp,
            'vc':               vc,
            'vd_ptr':           vd,           # granny_vertex_data address (for count/ptr patching)
            'topo_ptr':         topo_ptr,     # granny_tri_topology address
            'vertex_type_ptr':  vertex_type_ptr,
            'mesh_ptr':         mesh_ptr,
            'idx_count':        idx_count,
            'idx_ptr':          idx_ptr,
            'bb_count':         bb_count,
            'bb_ptr':           bb_ptr,
            'bb_names':         bb_names,
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
    Strip rig prefix, LOD suffixes, Blender duplicate suffixes, and path
    separators for matching.
    'Melinoe_Rig:MelinoeOverlook_MeshShape_LOD1.001' -> 'melinoeoverlook_meshshape'
    """
    # Drop 'Armature:' / 'Rig:' style prefixes Blender adds on export
    n = name.split(':')[-1].split('|')[-1].split('/')[-1]
    # Strip Blender duplicate suffixes (.001, .002, etc.)
    import re
    n = re.sub(r'\.\d{3,}$', '', n)
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


def _remap_bone_indices(glb_m: dict, gr2_bb_names: list[str]) -> tuple:
    """
    Build a remapping from GLB joint indices to GR2 bone binding indices
    using bone names. Returns (remapped_bj_u8, remapped_bw_u8, success).

    GLB stores joint indices into its own skin.joints order (bone_palette).
    GR2 stores bone indices into the mesh's BoneBindings array (bb_names).
    These orders differ after a Blender round-trip.

    If the GLB has no skinning data or no bone_palette, returns (None, None, False).
    """
    glb_palette = glb_m.get('bone_palette')
    glb_bj = glb_m.get('bj_u8')
    glb_bw = glb_m.get('bw_u8')

    if glb_palette is None or glb_bj is None or glb_bw is None:
        return None, None, False

    if not gr2_bb_names:
        return None, None, False

    # Build name -> GR2 index mapping
    # Bone names may have prefixes like "Armature:" from Blender — strip them
    gr2_name_to_idx = {}
    for i, name in enumerate(gr2_bb_names):
        gr2_name_to_idx[name] = i
        # Also index by the part after the last colon (short name)
        short = name.split(':')[-1]
        if short not in gr2_name_to_idx:
            gr2_name_to_idx[short] = i

    # Build the remap table: glb_joint_idx -> gr2_binding_idx
    remap = np.zeros(len(glb_palette), dtype=np.uint8)
    unmapped = []
    for glb_idx, glb_name in enumerate(glb_palette):
        # Try exact match first
        gr2_idx = gr2_name_to_idx.get(glb_name)
        if gr2_idx is None:
            # Try short name (after last colon or pipe)
            short = glb_name.split(':')[-1].split('|')[-1]
            gr2_idx = gr2_name_to_idx.get(short)
        if gr2_idx is None:
            # Try with Blender's colon escape (_x003A_)
            unescaped = glb_name.replace('_x003A_', ':')
            gr2_idx = gr2_name_to_idx.get(unescaped)
            if gr2_idx is None:
                gr2_idx = gr2_name_to_idx.get(unescaped.split(':')[-1])
        if gr2_idx is not None:
            remap[glb_idx] = gr2_idx
        else:
            unmapped.append(glb_name)
            remap[glb_idx] = 0  # fallback to binding 0

    if unmapped:
        print(f"    WARNING: {len(unmapped)} GLB joints not found in GR2 bone bindings:")
        for name in unmapped[:5]:
            print(f"      '{name}' -> fallback to binding 0")
        if len(unmapped) > 5:
            print(f"      ... and {len(unmapped) - 5} more")

    # Apply remap to all vertices
    remapped_bj = np.zeros_like(glb_bj)
    for j in range(4):
        col = glb_bj[:, j]
        # Clamp to valid palette range
        col_clamped = np.clip(col, 0, len(remap) - 1)
        remapped_bj[:, j] = remap[col_clamped]

    matched = len(glb_palette) - len(unmapped)
    print(f"    Bone remap: {matched}/{len(glb_palette)} joints matched")
    return remapped_bj, glb_bw, True


def _apply_patch(glb_m: dict, gm: dict, strict: bool,
                 allow_topology_change: bool = False) -> bool:
    """
    Patch one mesh's vertex buffer (and optionally index buffer) in DLL-managed
    memory, then update bounding boxes.

    If vertex counts match: patches in-place (fast path).
    If vertex counts differ and allow_topology_change=True: allocates new
    Python buffers, patches the granny_vertex_data count/ptr fields so the
    DLL serializer picks up the new data.

    Returns True if the patch was applied, False if skipped.
    """
    vc_glb = len(glb_m['positions'])
    vc_gr2 = gm['vc']
    topology_changed = (vc_glb != vc_gr2)

    if topology_changed and not allow_topology_change:
        msg = (f"Mesh {glb_m['name']!r} -> {gm['name']!r}: vertex count mismatch "
               f"(GLB={vc_glb}, GR2={vc_gr2}).\n"
               f"    Same-topology edits only — vertex count must be identical.\n"
               f"    Common causes: Subdivide, Decimate, Merge by Distance, or\n"
               f"    exporting with Normals ON (splits vertices at seams).\n"
               f"    Re-export from Blender with Normals OFF and no topology changes.\n"
               f"    Or use --allow-topology-change to enable variable vertex counts.")
        if strict:
            raise ValueError(msg)
        print(f"  WARNING: {msg} — skipping")
        return False

    if topology_changed:
        print(f"  [{gm['name']!r}] Topology change: {vc_gr2} -> {vc_glb} vertices")

    # Read the original vertex buffer from GR2 memory as fallback
    orig_raw = np.frombuffer(
        bytes((ctypes.c_uint8 * (vc_gr2 * 40)).from_address(gm['vp'])),
        dtype=np.uint8,
    ).reshape(vc_gr2, 40)

    fallback_nrm = None
    if not glb_m.get('has_normals', True):
        if not topology_changed:
            fallback_nrm = orig_raw[:, 20:32].view(np.float32).reshape(vc_gr2, 3).copy()
            print(f"  [{gm['name']!r}] normals not in GLB — preserving originals from GR2")
        else:
            print(f"  [{gm['name']!r}] normals not in GLB + topology changed — using zero normals")

    glb_m = dict(glb_m)

    # Remap bone weights from GLB using name-based joint matching
    remapped_bj, remapped_bw, remap_ok = _remap_bone_indices(glb_m, gm.get('bb_names', []))
    if remap_ok:
        glb_m['bj_u8'] = remapped_bj
        glb_m['bw_u8'] = remapped_bw
    elif not topology_changed:
        # Same topology, no skinning data — preserve from original
        glb_m['bw_u8'] = orig_raw[:, 12:16].copy()
        glb_m['bj_u8'] = orig_raw[:, 16:20].copy()
    # else: topology changed + no remap → zeros (from build_vertex_buffer_40 defaults)

    vbuf = build_vertex_buffer_40(glb_m, fallback_normals=fallback_nrm)

    # Validate vertex count fits in uint16 indices (engine limit)
    if vc_glb > 65535:
        print(f"  ERROR: {glb_m['name']!r} has {vc_glb} vertices — exceeds engine limit "
              f"of 65,535 (uint16 index buffer). Reduce polygon count.")
        return False

    # Validate indices are within vertex bounds
    glb_indices = glb_m.get('indices')
    if glb_indices is not None and len(glb_indices) > 0:
        max_idx = int(np.max(glb_indices))
        if max_idx >= vc_glb:
            print(f"  ERROR: {glb_m['name']!r} has index {max_idx} but only {vc_glb} vertices "
                  f"— mesh is corrupt (faces reference deleted vertices)")
            return False

    if topology_changed:
        # ── Variable-topology path: allocate new buffers, patch struct fields ──
        new_vc = vc_glb

        # Allocate new vertex buffer in Python memory
        new_vbuf = (ctypes.c_uint8 * len(vbuf))()
        ctypes.memmove(new_vbuf, vbuf, len(vbuf))
        _keepalive.append(new_vbuf)

        # Patch granny_vertex_data: count at +0x08, ptr at +0x0C
        vd_ptr = gm['vd_ptr']
        _verts = _t('granny_vertex_data', 'Vertices')
        struct.pack_into('<i', (ctypes.c_uint8 * 4).from_address(vd_ptr + _verts + 8),
                         0, new_vc)
        struct.pack_into('<Q', (ctypes.c_uint8 * 8).from_address(vd_ptr + _verts + 12),
                         0, ctypes.addressof(new_vbuf))

        # Update gm so OBB recomputation reads from the new buffer
        gm['vp'] = ctypes.addressof(new_vbuf)
        gm['vc'] = new_vc
    else:
        # ── Same-topology path: patch in-place ──
        ctypes.memmove(gm['vp'], vbuf, len(vbuf))

    # ── Patch index buffer ──
    idx_patched = False
    glb_indices = glb_m.get('indices')
    gr2_idx_count = gm.get('idx_count', 0)
    gr2_idx_ptr   = gm.get('idx_ptr', 0)
    topo_ptr      = gm.get('topo_ptr', 0)

    if glb_indices is not None and len(glb_indices) > 0:
        glb_idx_count = len(glb_indices)
        if glb_idx_count == gr2_idx_count and _valid_ptr(gr2_idx_ptr):
            # Same index count — patch in-place
            idx_bytes = glb_indices.astype(np.uint16).tobytes()
            ctypes.memmove(gr2_idx_ptr, idx_bytes, len(idx_bytes))
            idx_patched = True
        elif glb_idx_count != gr2_idx_count and allow_topology_change and _valid_ptr(topo_ptr):
            # Different index count — allocate new buffer, patch topology struct
            idx_bytes = glb_indices.astype(np.uint16).tobytes()
            new_idx_buf = (ctypes.c_uint8 * len(idx_bytes))()
            ctypes.memmove(new_idx_buf, idx_bytes, len(idx_bytes))
            _keepalive.append(new_idx_buf)

            _idx16 = _t('granny_tri_topology', 'Indices16')
            # Patch Indices16: count at +0x18, ptr at +0x1C
            struct.pack_into('<i', (ctypes.c_uint8 * 4).from_address(topo_ptr + _idx16),
                             0, glb_idx_count)
            struct.pack_into('<Q', (ctypes.c_uint8 * 8).from_address(topo_ptr + _idx16 + 4),
                             0, ctypes.addressof(new_idx_buf))

            # GrannyGetMeshTriangleCount reads topology+0x0C first (priority field).
            # If non-zero it ignores Indices16.count at +0x18. Update it too.
            struct.pack_into('<i', (ctypes.c_uint8 * 4).from_address(topo_ptr + 0x0C),
                             0, glb_idx_count)

            gm['idx_count'] = glb_idx_count
            gm['idx_ptr'] = ctypes.addressof(new_idx_buf)
            idx_patched = True
            print(f"    Index count changed: {gr2_idx_count} -> {glb_idx_count}")
        elif glb_idx_count != gr2_idx_count:
            print(f"    Index count mismatch (GLB={glb_idx_count}, GR2={gr2_idx_count}) — keeping original")

    # Recompute per-bone bounding boxes
    _update_bone_obbs(gm)

    weights_src = "GLB (remapped)" if remap_ok else "original GR2"
    topo_msg = f" TOPOLOGY CHANGED ({vc_gr2}->{vc_glb}v)" if topology_changed else ""
    idx_msg = ", indices patched" if idx_patched else ""
    print(f"  Patched {gm['name']!r}: {gm['vc']} verts x 40 bytes "
          f"(OBB updated, weights: {weights_src}{idx_msg}){topo_msg}")
    return True


def _create_new_mesh(dll, fi, glb_m, skeleton_bones, template_mesh):
    """
    Create a new granny_mesh by CLONING an existing mesh and replacing
    vertex data, index data, bone bindings, and name.

    Cloning ensures all metadata (material bindings, extended data, etc.)
    is valid — the serializer accepts the struct without crashes.

    Returns new mesh pointer (int) or None on failure.
    """
    vc = len(glb_m['positions'])
    if vc > 65535:
        print(f"  ERROR: New mesh '{glb_m['name']}' has {vc} vertices — exceeds uint16 limit")
        return None

    indices = glb_m.get('indices')
    if indices is None or len(indices) == 0:
        print(f"  ERROR: New mesh '{glb_m['name']}' has no indices")
        return None

    # ── Clone the template mesh struct ──
    # Use GrannyGetTotalObjectSize for correct struct sizes
    from granny_types import _type_sym_addr
    mesh_size = dll.GrannyGetTotalObjectSize(ctypes.c_void_p(
        _type_sym_addr(dll, 'GrannyMeshType')))
    vd_size = dll.GrannyGetTotalObjectSize(ctypes.c_void_p(
        _type_sym_addr(dll, 'GrannyVertexDataType')))
    topo_size = dll.GrannyGetTotalObjectSize(ctypes.c_void_p(
        _type_sym_addr(dll, 'GrannyTriTopologyType')))

    tmpl_ptr = template_mesh['mesh_ptr']
    mesh_buf = (ctypes.c_uint8 * mesh_size)()
    ctypes.memmove(mesh_buf, tmpl_ptr, mesh_size)
    _keepalive.append(mesh_buf)

    # ── Replace Name ──
    name_bytes = glb_m['name'].encode('utf-8') + b'\x00'
    name_buf = (ctypes.c_uint8 * len(name_bytes))(*name_bytes)
    _keepalive.append(name_buf)
    struct.pack_into('<Q', mesh_buf, 0x00, ctypes.addressof(name_buf))

    # ── Build BoneBindings FIRST (needed to remap vertex bone indices) ──
    bb_entries = []
    bb_name_list = []  # ordered list of bone names in our bindings
    bone_palette = glb_m.get('bone_palette') or []
    bj_u8 = glb_m.get('bj_u8')
    skel_name_set = set(skeleton_bones)

    for bp_idx, bp_name in enumerate(bone_palette):
        matched = bp_name if bp_name in skel_name_set else None
        if matched is None:
            short = bp_name.split(':')[-1]
            matched = next((s for s in skeleton_bones if s.split(':')[-1] == short), None)
        if matched is None:
            continue

        if bj_u8 is not None:
            mask = np.any(bj_u8 == bp_idx, axis=1)
            pos = glb_m['positions'][mask] if np.any(mask) else glb_m['positions'][:1]
        else:
            pos = glb_m['positions']
        obb_min = pos.min(axis=0).astype(np.float32)
        obb_max = pos.max(axis=0).astype(np.float32)

        bb = (ctypes.c_uint8 * 44)()
        bn_bytes = matched.encode('utf-8') + b'\x00'
        bn_buf = (ctypes.c_uint8 * len(bn_bytes))(*bn_bytes)
        _keepalive.append(bn_buf)
        struct.pack_into('<Q', bb, 0, ctypes.addressof(bn_buf))
        struct.pack_into('<3f', bb, 8, *obb_min)
        struct.pack_into('<3f', bb, 20, *obb_max)
        bb_entries.append(bytes(bb))
        bb_name_list.append(matched)

    if not bb_entries:
        bn = (skeleton_bones[0] if skeleton_bones else 'root').encode('utf-8') + b'\x00'
        bn_buf = (ctypes.c_uint8 * len(bn))(*bn)
        _keepalive.append(bn_buf)
        bb = (ctypes.c_uint8 * 44)()
        struct.pack_into('<Q', bb, 0, ctypes.addressof(bn_buf))
        struct.pack_into('<3f', bb, 8, *glb_m['positions'].min(axis=0))
        struct.pack_into('<3f', bb, 20, *glb_m['positions'].max(axis=0))
        bb_entries.append(bytes(bb))
        bb_name_list.append(skeleton_bones[0] if skeleton_bones else 'root')

    # ── Remap bone indices: GLB palette index → new BoneBindings index ──
    glb_m = dict(glb_m)  # don't modify original
    if bj_u8 is not None and glb_m.get('bw_u8') is not None:
        # Build remap: glb_palette_idx → bb_entries index
        remap = np.zeros(len(bone_palette), dtype=np.uint8)
        for bp_idx, bp_name in enumerate(bone_palette):
            # Find this bone in our bb_name_list
            matched = bp_name
            if matched not in bb_name_list:
                short = bp_name.split(':')[-1]
                matched = next((n for n in bb_name_list if n.split(':')[-1] == short), None)
            if matched and matched in bb_name_list:
                remap[bp_idx] = bb_name_list.index(matched)

        # Apply remap to all vertices
        new_bj = np.zeros_like(bj_u8)
        for j in range(4):
            col = np.clip(bj_u8[:, j], 0, len(remap) - 1)
            new_bj[:, j] = remap[col]
        glb_m['bj_u8'] = new_bj

    # ── Build + replace vertex buffer (now with correct bone indices) ──
    vbuf_bytes = build_vertex_buffer_40(glb_m)
    vbuf = (ctypes.c_uint8 * len(vbuf_bytes))()
    ctypes.memmove(vbuf, vbuf_bytes, len(vbuf_bytes))
    _keepalive.append(vbuf)

    # Clone VertexData struct, update vertices
    tmpl_vd = rq(tmpl_ptr, 0x08)
    vd_buf = (ctypes.c_uint8 * vd_size)()
    ctypes.memmove(vd_buf, tmpl_vd, vd_size)
    _keepalive.append(vd_buf)
    # Keep original vertex type, update count + data ptr
    struct.pack_into('<I', vd_buf, 0x08, vc)
    struct.pack_into('<Q', vd_buf, 0x0C, ctypes.addressof(vbuf))
    struct.pack_into('<Q', mesh_buf, 0x08, ctypes.addressof(vd_buf))

    # ── Build + replace index buffer ──
    idx_np = indices.astype(np.uint16)
    idx_buf = (ctypes.c_uint8 * idx_np.nbytes)()
    ctypes.memmove(idx_buf, idx_np.tobytes(), idx_np.nbytes)
    _keepalive.append(idx_buf)

    # Clone Topology struct, update indices
    tmpl_topo = rq(tmpl_ptr, 0x1C)
    topo_buf = (ctypes.c_uint8 * topo_size)()
    ctypes.memmove(topo_buf, tmpl_topo, topo_size)
    _keepalive.append(topo_buf)
    # Update Indices16
    struct.pack_into('<I', topo_buf, 0x18, len(idx_np))
    struct.pack_into('<Q', topo_buf, 0x1C, ctypes.addressof(idx_buf))
    # Update Groups[0].TriCount (at +8 in the group entry)
    grp_ptr = struct.unpack_from('<Q', bytes(topo_buf), 0x04)[0]
    if _valid_ptr(grp_ptr):
        grp_buf = (ctypes.c_uint8 * 24)()
        ctypes.memmove(grp_buf, grp_ptr, 24)
        struct.pack_into('<i', grp_buf, 8, len(idx_np) // 3)
        _keepalive.append(grp_buf)
        struct.pack_into('<Q', topo_buf, 0x04, ctypes.addressof(grp_buf))
    struct.pack_into('<Q', mesh_buf, 0x1C, ctypes.addressof(topo_buf))

    # ── Write BoneBindings (built earlier, before vertex buffer) ──
    bb_array = (ctypes.c_uint8 * (44 * len(bb_entries)))()
    for i, bb_data in enumerate(bb_entries):
        ctypes.memmove(ctypes.addressof(bb_array) + i * 44, bb_data, 44)
    _keepalive.append(bb_array)
    struct.pack_into('<I', mesh_buf, 0x30, len(bb_entries))
    struct.pack_into('<Q', mesh_buf, 0x34, ctypes.addressof(bb_array))

    tri_count = len(idx_np) // 3
    print(f"  NEW MESH '{glb_m['name']}': {vc} verts, {tri_count} tris, "
          f"{len(bb_entries)} bone bindings")
    return ctypes.addressof(mesh_buf)


def patch_vertex_data(
    dll, fi: int, glb_meshes: list[dict],
    strict: bool = False,
    positional: bool = False,
    allow_topology_change: bool = False,
) -> int:
    """
    Match each GLB mesh to the corresponding GR2 mesh, then patch the vertex
    buffer in DLL-managed memory.

    Matching order (unless positional=True):
      1. Exact normalised name
      2. Variant-word-stripped name (e.g. Overlook / Overview stripped)
      3. Longest contained substring (>=60 % overlap)

    positional=True: pair GLB mesh[i] to unique GR2 mesh[i] by index.

    When allow_topology_change=False (default): only patches LODs whose vertex
    count matches the GLB mesh. When True: patches the first matching LOD
    regardless of vertex count (allocates new buffers for the serializer).

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
            if _apply_patch(glb_m, gm, strict, allow_topology_change):
                patched += 1
        return patched

    # Build ordered groups: normalised name -> [GR2 meshes in file order]
    # GR2 LODs share the same name (e.g. two "Melinoe_MeshShape" at different resolutions).
    # We match by name first, then pair by position within the group (LOD0→LOD0, LOD1→LOD1).
    gr2_by_name: dict[str, list] = {}
    for gm in gr2_mesh_list:
        key = _normalize_mesh_name(gm['name'])
        gr2_by_name.setdefault(key, []).append(gm)

    glb_by_name: dict[str, list] = {}
    for glb_m in glb_meshes:
        key = _normalize_mesh_name(glb_m['name'])
        glb_by_name.setdefault(key, []).append(glb_m)

    # Resolve GLB group names to GR2 group names (exact, then fuzzy)
    for glb_key, glb_group in glb_by_name.items():
        matched_key = glb_key if glb_key in gr2_by_name else None
        if matched_key is None:
            matched_key = _best_name_match(glb_key, gr2_by_name)
            if matched_key is not None:
                print(f"  Fuzzy match: '{glb_key}' -> '{matched_key}'")

        if matched_key is None:
            # No GR2 match — this is a NEW mesh to add
            if not allow_topology_change:
                print(f"  WARNING: GLB mesh '{glb_key}' not in GR2 — use --allow-topology-change to add new meshes")
                continue

            # Use first existing mesh as template + get bone names from its bindings
            # Pick template: prefer body mesh over outline (outline uses a
            # different material/shader that renders behind the character)
            template = None
            for _gm in gr2_mesh_list:
                if 'Outline' not in _gm['name'] and 'Shadow' not in _gm['name']:
                    template = _gm
                    break
            if template is None:
                template = gr2_mesh_list[0] if gr2_mesh_list else None
            if template is None:
                print(f"  ERROR: No existing mesh to use as template — cannot add new meshes")
                continue

            skel_bones = template.get('bb_names', [])

            for glb_m in glb_group:
                # _create_new_mesh handles bone index remapping internally
                # (maps GLB palette indices to the new mesh's BoneBindings order)
                new_mesh_ptr = _create_new_mesh(dll, fi, glb_m, skel_bones, template)
                if new_mesh_ptr is not None:
                    # Expand fi->Meshes array
                    _meshes_off = _t('granny_file_info', 'Meshes')
                    old_count = ri(fi, _meshes_off)
                    old_arr = rq(fi, _meshes_off + 4)

                    new_count = old_count + 1
                    new_arr = (ctypes.c_uint8 * (new_count * 8))()
                    if _valid_ptr(old_arr):
                        ctypes.memmove(new_arr, old_arr, old_count * 8)
                    struct.pack_into('<Q', new_arr, old_count * 8, new_mesh_ptr)
                    _keepalive.append(new_arr)

                    struct.pack_into('<i', (ctypes.c_uint8 * 4).from_address(fi + _meshes_off),
                                     0, new_count)
                    struct.pack_into('<Q', (ctypes.c_uint8 * 8).from_address(fi + _meshes_off + 4),
                                     0, ctypes.addressof(new_arr))

                    # Also expand Model[0]->MeshBindings so the game renders it
                    model_count = ri(fi, 0x60)
                    if model_count > 0:
                        model0 = rq(rq(fi, 0x64), 0)
                        # MeshBindings at model+0x54 (after Name(8)+Skeleton(8)+Transform(68))
                        mb_off = 0x54
                        old_mb_count = ri(model0, mb_off)
                        old_mb_ptr = rq(model0, mb_off + 4)
                        new_mb_count = old_mb_count + 1
                        new_mb_arr = (ctypes.c_uint8 * (new_mb_count * 8))()
                        if _valid_ptr(old_mb_ptr):
                            ctypes.memmove(new_mb_arr, old_mb_ptr, old_mb_count * 8)
                        struct.pack_into('<Q', new_mb_arr, old_mb_count * 8, new_mesh_ptr)
                        _keepalive.append(new_mb_arr)
                        struct.pack_into('<i', (ctypes.c_uint8 * 4).from_address(model0 + mb_off),
                                         0, new_mb_count)
                        struct.pack_into('<Q', (ctypes.c_uint8 * 8).from_address(model0 + mb_off + 4),
                                         0, ctypes.addressof(new_mb_arr))

                    # Refresh mesh list for subsequent iterations
                    gr2_mesh_list = get_gr2_meshes(fi)
                    patched += 1
            continue

        gr2_group = gr2_by_name[matched_key]

        # Pair by position: GLB[0]→GR2[0], GLB[1]→GR2[1], etc.
        for i, glb_m in enumerate(glb_group):
            if i >= len(gr2_group):
                print(f"  WARNING: GLB has more LODs than GR2 for '{matched_key}' "
                      f"— skipping GLB LOD {i}")
                continue

            gm = gr2_group[i]
            vc_glb = len(glb_m['positions'])
            vc_gr2 = gm['vc']

            if vc_glb != vc_gr2 and not allow_topology_change:
                msg = (f"Mesh '{glb_m['name']}' LOD{i}: vertex count mismatch "
                       f"(GLB={vc_glb}, GR2={vc_gr2}). "
                       f"Use --allow-topology-change to enable.")
                if strict:
                    raise ValueError(msg)
                print(f"  WARNING: {msg} — skipping")
                continue

            if _apply_patch(glb_m, gm, strict, allow_topology_change):
                patched += 1

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
    allow_topology_change: bool = False,
    patch_animations: bool = False,
    anim_patch_filter: str = None,
) -> None:
    print(f"[1/6] Parsing GLB: {glb_path}")
    glb_meshes = parse_glb(glb_path)
    print(f"  Found {len(glb_meshes)} mesh(es) in GLB")

    glb_animations = {}
    if patch_animations:
        glb_animations, _ = parse_glb_animations(glb_path)
        print(f"  Found {len(glb_animations)} animation(s) in GLB")

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
        dll, fi, glb_meshes, strict=strict, positional=positional,
        allow_topology_change=allow_topology_change,
    )
    if n_patched == 0:
        raise RuntimeError("No meshes were patched — nothing to write")

    print(f"[6/6] Serializing modified GR2")
    new_gr2_bytes = build_gr2_bytes(dll, fi, gr2_bytes, sdb_dict, sdb_crc)

    # Free Python-allocated buffers now that serialization is complete
    _keepalive.clear()

    dll.GrannyFreeFile(gr2_file)
    dll.GrannyFreeFile(sdb_file)

    if output_gr2:
        with open(output_gr2, 'wb') as f:
            f.write(new_gr2_bytes)
        print(f"  GR2 written: {output_gr2!r} ({len(new_gr2_bytes):,} bytes)")

    # Replace the mesh entry in the GPK, keep all other entries unchanged
    gpk_entries[entry_name] = new_gr2_bytes

    # Patch animation entries if requested
    n_anim_patched = 0
    if glb_animations:
        print(f"[7/7] Patching animation entries")
        n_anim_patched = patch_animation_entries(dll, gpk_entries, sdb_bytes, glb_animations,
                                                    anim_patch_filter=anim_patch_filter)
        print(f"  {n_anim_patched} animation(s) patched")

    pack_gpk(gpk_entries, output_gpk)
    anim_msg = f", {n_anim_patched} animation(s)" if n_anim_patched else ""
    print(f"\nDone!  {n_patched} mesh(es){anim_msg} patched -> {output_gpk!r}")


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
    parser.add_argument('--allow-topology-change', action='store_true',
                        help='Allow different vertex/triangle counts between GLB and GR2. '
                             'Enables sculpting, subdivide, decimate, and other mesh edits '
                             'that change topology. EXPERIMENTAL.')
    parser.add_argument('--patch-animations', action='store_true',
                        help='Also patch animation entries if the GLB contains animations. '
                             'Requires --anim-patch-filter. EXPERIMENTAL.')
    parser.add_argument('--anim-patch-filter', default=None,
                        help='Only patch animation entries matching this pattern '
                             '(e.g. "NoWeapon_Base_Idle_00"). Required with --patch-animations '
                             'to prevent accidental mass re-encoding.')
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
        allow_topology_change=args.allow_topology_change,
        patch_animations=args.patch_animations,
        anim_patch_filter=args.anim_patch_filter,
    )


if __name__ == '__main__':
    main()
