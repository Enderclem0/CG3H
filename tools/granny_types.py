"""
granny_types.py — Dynamic Granny struct offset resolver.

Walks the granny_data_type_definition arrays exported from granny2_x64.dll
to discover field byte-offsets at runtime, replacing all hardcoded struct
constants elsewhere in the toolchain.

Usage:
    from granny_types import (
        build_type_map, get_struct_stride,
        get_vertex_layout, get_transform_field_offsets, setup_dll_types,
    )

    dll    = ctypes.CDLL("./granny2_x64.dll")
    setup_dll_types(dll)
    types  = build_type_map(dll)          # {struct_name: {field_name: byte_offset}}
    t_offs = get_transform_field_offsets(dll)  # {flags, translation, orientation, scale_shear}
    layout = get_vertex_layout(dll, vtype_ptr) # [(name, offset, total, mtype, count), …]

Standalone test (run from Ship/ directory):
    python granny_types.py
"""

import ctypes
import struct
import os
import sys

# ---------------------------------------------------------------------------
# Low-level memory helpers
# ---------------------------------------------------------------------------

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


def _safe_bytes(addr, n):
    return bytes((ctypes.c_uint8 * n).from_address(addr))


def _read_cstr(ptr):
    if not _valid_ptr(ptr):
        return ''
    buf = (ctypes.c_uint8 * 256).from_address(ptr)
    b = bytes(buf)
    return b[:b.index(0)].decode('utf-8', 'replace') if 0 in b else ''


# ---------------------------------------------------------------------------
# granny_data_type_definition node layout (44 bytes, confirmed)
#   +0x00 (4)  member_type  — int32 enum; 0 = EndMember sentinel
#   +0x04 (8)  name*        — char*
#   +0x0C (8)  ref_type*    — nested type def ptr (non-null for InlineMembers)
#   +0x14 (4)  array_width  — >1 means fixed-length array; 0/1 means scalar
#   +0x18 (24) extra        — unused for offset/size computation
#
# NOTE: Granny exports GrannyBoneType, GrannyMeshType etc. as POINTER variables
#       (granny_data_type_definition * const), not as the arrays themselves.
#       _type_sym_addr() dereferences the symbol to get the actual array address.
# ---------------------------------------------------------------------------

NODE_STRIDE  = 44
_OFF_MTYPE   = 0x00
_OFF_NAME    = 0x04
_OFF_REFTYPE = 0x0C
_OFF_ARRW    = 0x14

# ---------------------------------------------------------------------------
# Granny member type enum values (granny_member_type, Granny 2.x SDK)
# ---------------------------------------------------------------------------

MTYPE_INLINE                    = 1
MTYPE_REFERENCE                 = 2
MTYPE_REFERENCE_TO_ARRAY        = 3
MTYPE_ARRAY_OF_REFERENCES       = 4
MTYPE_VARIANT_REFERENCE         = 5
MTYPE_REFERENCE_TO_VARIANT_ARRAY= 7
MTYPE_STRING                    = 8
MTYPE_TRANSFORM                 = 9
MTYPE_REAL32                    = 10
MTYPE_INT8                      = 11
MTYPE_UINT8                     = 12
MTYPE_BINORMAL_I8               = 13
MTYPE_NORMAL_U8                 = 14
MTYPE_INT16                     = 15
MTYPE_UINT16                    = 16
MTYPE_BINORMAL_I16              = 17
MTYPE_NORMAL_U16                = 18
MTYPE_INT32                     = 19
MTYPE_UINT32                    = 20
MTYPE_REAL16                    = 21
MTYPE_EMPTY_REFERENCE           = 22

# Primitive types — in-memory element size (before array_width scaling).
PRIMITIVE_SIZE: dict[int, int] = {
    MTYPE_REAL32:       4,
    MTYPE_INT8:         1,
    MTYPE_UINT8:        1,
    MTYPE_BINORMAL_I8:  1,
    MTYPE_NORMAL_U8:    1,
    MTYPE_INT16:        2,
    MTYPE_UINT16:       2,
    MTYPE_BINORMAL_I16: 2,
    MTYPE_NORMAL_U16:   2,
    MTYPE_INT32:        4,
    MTYPE_UINT32:       4,
    MTYPE_REAL16:       2,
}

# ---------------------------------------------------------------------------
# In-memory size of each complex (non-primitive) Granny member type on 64-bit.
#
# IMPORTANT: GrannyGetTotalObjectSize returns FILE-FORMAT sizes, not in-memory
# sizes.  The two differ for reference/array/transform member types.
# These constants were derived empirically from confirmed struct offsets
# (see _KNOWN_OFFSETS below) cross-checked against granny_member_type enum.
#
# Layout of each type in the loaded in-memory struct:
#   InlineMember (1)                — embedded struct; use GrannyGetTotalObjectSize(ref_type)
#   ReferenceMember (2)             — void* (8)
#   ReferenceToArrayMember (3)      — int32 count (4) + void* ptr (8) = 12
#   ArrayOfReferencesMember (4)     — int32 count (4) + void** ptr (8) = 12
#   VariantReferenceMember (5)      — void* type (8) + void* data (8) = 16
#   ReferenceToVariantArrayMember(7)— void* type (8) + int32 count (4) + void* data (8) = 20
#   StringMember (8)                — char* (8)
#   TransformMember (9)             — granny_transform inline struct (68 bytes)
#   EmptyReferenceMember (22)       — void* (8)
# ---------------------------------------------------------------------------

MEMBER_MEMORY_SIZE: dict[int, int] = {
    MTYPE_REFERENCE:                  8,
    MTYPE_REFERENCE_TO_ARRAY:         12,
    MTYPE_ARRAY_OF_REFERENCES:        12,
    MTYPE_VARIANT_REFERENCE:          16,
    MTYPE_REFERENCE_TO_VARIANT_ARRAY: 20,
    MTYPE_STRING:                      8,
    MTYPE_TRANSFORM:                  68,  # granny_transform: 4+12+16+36 = 68 bytes
    MTYPE_EMPTY_REFERENCE:             8,
}

_bootstrapped: bool = False


# ---------------------------------------------------------------------------
# Element-size resolution
# ---------------------------------------------------------------------------

def _get_element_size(dll, mtype: int, ref_type_ptr: int) -> int:
    """
    Return the in-memory byte size of ONE element (before array_width scaling).

    Priority:
    1. Primitives (10-21): looked up in PRIMITIVE_SIZE.
    2. InlineMember (1): GrannyGetTotalObjectSize(ref_type_ptr) — the only case
       where the file-format size equals the in-memory size.
    3. All other complex types: MEMBER_MEMORY_SIZE — fixed sizes regardless
       of what GrannyGetTotalObjectSize says (it returns file sizes, not memory).
    4. Unknown fallback: 8 bytes.
    """
    prim = PRIMITIVE_SIZE.get(mtype)
    if prim is not None:
        return prim

    if mtype == MTYPE_INLINE:
        if _valid_ptr(ref_type_ptr) and _readable(ref_type_ptr, NODE_STRIDE):
            try:
                sz = dll.GrannyGetTotalObjectSize(ctypes.c_void_p(ref_type_ptr))
                if sz > 0:
                    return sz
            except Exception:
                pass  # DLL call failed — fall through to return 0
        return 0  # should not happen for a valid InlineMember

    mem_sz = MEMBER_MEMORY_SIZE.get(mtype)
    if mem_sz is not None:
        return mem_sz

    # Unknown type — 8-byte fallback (pointer-sized).
    return 8


def _bootstrapped_check(dll):
    global _bootstrapped
    if not _bootstrapped:
        addr = _type_sym_addr(dll, 'GrannyBoneType')
        if not _valid_ptr(addr):
            raise RuntimeError("GrannyBoneType type-def array is not accessible")
        _bootstrapped = True


# ---------------------------------------------------------------------------
# Core walker — returns 5-tuples so callers can match on type, not just name
# ---------------------------------------------------------------------------

# Each entry: (field_name, byte_offset, total_bytes, mtype, element_count)
FieldEntry = tuple[str, int, int, int, int]


def walk_type_def(dll, type_ptr: int) -> list[FieldEntry]:
    """
    Walk a granny_data_type_definition array starting at type_ptr.

    Returns a list of FieldEntry = (name, byte_offset, total_bytes, mtype, element_count).

    byte_offset is accumulated from the start of the struct.
    mtype is the raw Granny member-type enum value.
    element_count is max(1, array_width).

    InlineMember sizes are resolved via GrannyGetTotalObjectSize on the
    reference_type pointer — no hardcoded inline-struct sizes anywhere.
    """
    if not _valid_ptr(type_ptr) or not _readable(type_ptr, NODE_STRIDE):
        return []

    entries: list[FieldEntry] = []
    acc = 0
    cursor = type_ptr

    while _readable(cursor, NODE_STRIDE):
        raw  = _safe_bytes(cursor, NODE_STRIDE)
        mtype = struct.unpack_from('<i', raw, _OFF_MTYPE)[0]
        if mtype == 0:
            break

        name_ptr     = struct.unpack_from('<Q', raw, _OFF_NAME)[0]
        ref_type_ptr = struct.unpack_from('<Q', raw, _OFF_REFTYPE)[0]
        arr_w        = struct.unpack_from('<i', raw, _OFF_ARRW)[0]

        name  = _read_cstr(name_ptr) if _readable(name_ptr, 4) else f'<{mtype}>'
        count = max(1, arr_w)
        elem  = _get_element_size(dll, mtype, ref_type_ptr)

        if elem == 8 and mtype not in PRIMITIVE_SIZE and not (
            _valid_ptr(ref_type_ptr) and _readable(ref_type_ptr, NODE_STRIDE)
        ):
            # Pointer fallback — emit a one-time note; not a problem, just informational.
            try:
                tn = dll.GrannyGetMemberTypeName(mtype)
                tname = tn.decode('ascii', 'replace') if tn else f'type_{mtype}'
            except Exception:
                tname = f'type_{mtype}'
            print(f"  [granny_types] note: field '{name}' mtype={mtype} ({tname}) "
                  f"treated as 8-byte pointer")

        total = elem * count
        entries.append((name, acc, total, mtype, count))
        acc += total
        cursor += NODE_STRIDE

    return entries


def _type_sym_addr(dll, symbol_name: str) -> int:
    """Return the address of the granny_data_type_definition array for a symbol.

    Granny exports type symbols as pointer variables (granny_data_type_definition*),
    not as the arrays directly.  c_void_p.in_dll reads the VALUE stored at the
    symbol's address — that value is the pointer to the actual type-def array.
    """
    try:
        ptr = ctypes.c_void_p.in_dll(dll, symbol_name)
    except (AttributeError, OSError):
        raise AttributeError(f"Symbol '{symbol_name}' not found in DLL")
    if ptr.value is None:
        raise AttributeError(f"Symbol '{symbol_name}' is a null pointer")
    return ptr.value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_type_map(dll) -> dict[str, dict[str, int]]:
    """
    Walk all exported Granny struct type symbols.

    Returns {struct_name: {field_name: byte_offset}}.

    Raises RuntimeError if any validated offset doesn't match the known-correct
    value — startup fails loudly rather than producing silent bad data.
    """
    _bootstrapped_check(dll)

    symbols = {
        'granny_file_info':    'GrannyFileInfoType',
        'granny_mesh':         'GrannyMeshType',
        'granny_skeleton':     'GrannySkeletonType',
        'granny_bone':         'GrannyBoneType',
        'granny_vertex_data':  'GrannyVertexDataType',
        'granny_tri_topology': 'GrannyTriTopologyType',
        'granny_bone_binding': 'GrannyBoneBindingType',
        'granny_texture':      'GrannyTextureType',
        'granny_material':     'GrannyMaterialType',
        'granny_material_map': 'GrannyMaterialMapType',
    }

    type_map: dict[str, dict[str, int]] = {}
    for struct_name, sym_name in symbols.items():
        try:
            addr = _type_sym_addr(dll, sym_name)
        except AttributeError:
            print(f"  [granny_types] WARNING: '{sym_name}' not exported — "
                  f"'{struct_name}' offsets unavailable")
            type_map[struct_name] = {}
            continue
        entries = walk_type_def(dll, addr)
        type_map[struct_name] = {name: off for name, off, *_ in entries}

    _validate(type_map)
    return type_map


# Field names exactly as returned by GrannyGetMemberTypeName for each struct.
# Array fields (ReferenceToArray etc.) report the offset of the count word;
# the pointer is always count_offset + 4.
# ReferenceToVariantArrayMember fields: offset points to {type*(8), count(4), ptr(8)}.
_KNOWN_OFFSETS = {
    # granny_file_info  (Skeletons/Meshes are ArrayOfReferences: count[4]+ptr[8])
    ('granny_file_info',    'Skeletons'):            0x30,
    ('granny_file_info',    'Meshes'):               0x54,
    # granny_bone
    ('granny_bone',         'ParentIndex'):          0x08,
    ('granny_bone',         'Transform'):            0x0C,
    ('granny_bone',         'InverseWorldTransform'): 0x50,
    # granny_mesh  (PrimaryVertexData is a ReferenceMember = pointer, 8 bytes)
    ('granny_mesh',         'PrimaryVertexData'):    0x08,
    # granny_mesh  (PrimaryTopology is a ReferenceMember = pointer, 8 bytes)
    ('granny_mesh',         'PrimaryTopology'):      0x1C,
    # granny_mesh  (BoneBindings is ReferenceToArray: count[4]+ptr[8])
    ('granny_mesh',         'BoneBindings'):         0x30,
    # granny_tri_topology  (Indices16 is ReferenceToArray: count[4]+ptr[8])
    ('granny_tri_topology', 'Indices16'):            0x18,
    # granny_bone_binding
    ('granny_bone_binding', 'BoneName'):             0x00,
    # granny_vertex_data  (Vertices is ReferenceToVariantArray: type*[8]+count[4]+ptr[8])
    ('granny_vertex_data',  'Vertices'):             0x00,
}


def _validate(type_map: dict):
    failures = []
    for (struct_name, field), expected in _KNOWN_OFFSETS.items():
        actual = type_map.get(struct_name, {}).get(field)
        if actual is None:
            failures.append(f"  {struct_name}.{field}: NOT FOUND (expected +0x{expected:02X})")
            # Print available fields to help diagnose name mismatches.
            avail = sorted(type_map.get(struct_name, {}).keys())
            failures.append(f"    available fields: {avail}")
        elif actual != expected:
            failures.append(
                f"  {struct_name}.{field}: got +0x{actual:02X}, expected +0x{expected:02X}"
            )
    if failures:
        raise RuntimeError(
            "granny_types validation failed:\n" + '\n'.join(failures)
        )


def get_struct_stride(dll, type_symbol_name: str) -> int:
    """
    Return total byte size of a struct via GrannyGetTotalObjectSize on its
    exported type symbol.  E.g. get_struct_stride(dll, 'GrannyBoneType') → 164.
    """
    addr = _type_sym_addr(dll, type_symbol_name)
    return dll.GrannyGetTotalObjectSize(ctypes.c_void_p(addr))


def get_vertex_layout(dll, vertex_type_ptr: int) -> list[FieldEntry] | None:
    """
    Walk the vertex type definition for a single mesh.

    Returns list of FieldEntry = (name, byte_offset, total_bytes, mtype, element_count),
    or None if vertex_type_ptr is null/unreadable.

    Callers can find skinning fields by type signature (mtype=MTYPE_UINT8, count=4)
    rather than by name — no hardcoded field names needed.
    """
    if not _valid_ptr(vertex_type_ptr) or not _readable(vertex_type_ptr, NODE_STRIDE):
        return None
    return walk_type_def(dll, vertex_type_ptr)


def get_transform_field_offsets(dll) -> dict[str, int]:
    """
    Walk GrannyTransformType to discover the byte offsets of each field
    within the granny_transform struct.

    Returns {'flags': int, 'translation': int, 'orientation': int, 'scale_shear': int}
    with offsets relative to the start of the transform struct.

    Falls back to the confirmed constants if GrannyTransformType is not exported.

    These offsets are added to the bone's LocalTransform field offset in
    read_skeleton() to get the absolute position within the bone struct.
    """
    try:
        addr = _type_sym_addr(dll, 'GrannyTransformType')
    except AttributeError:
        # GrannyTransformType not exported — use confirmed fallback constants.
        # granny_transform: Flags(4) + Translation(12) + Orientation(16) + ScaleShear(36)
        print("  [granny_types] GrannyTransformType not exported — using confirmed fallback offsets")
        return {'flags': 0, 'translation': 4, 'orientation': 16, 'scale_shear': 32}

    entries = walk_type_def(dll, addr)
    if len(entries) < 3:
        # GrannyTransformType in this Granny version describes the transform as a
        # special atomic type — individual sub-fields are not enumerated.
        # The confirmed fallback offsets are always correct for Granny 2.x.
        return {'flags': 0, 'translation': 4, 'orientation': 16, 'scale_shear': 32}

    # granny_transform field order is fixed by the Granny SDK:
    #   [0] Flags        — uint32
    #   [1] Translation  — float32×3
    #   [2] Orientation  — float32×4
    #   [3] ScaleShear   — float32×9
    result = {
        'flags':       entries[0][1],
        'translation': entries[1][1],
        'orientation': entries[2][1],
        'scale_shear': entries[3][1] if len(entries) > 3 else entries[2][1] + entries[2][2],
    }

    # Validate against confirmed constants.
    expected = {'flags': 0, 'translation': 4, 'orientation': 16, 'scale_shear': 32}
    for key, exp_off in expected.items():
        got = result[key]
        if got != exp_off:
            print(f"  [granny_types] WARNING: granny_transform.{key} = +0x{got:02X}, "
                  f"expected +0x{exp_off:02X} — check Granny version")

    return result


def setup_dll_types(dll):
    """Wire up ctypes signatures required by this module. Call once after ctypes.CDLL()."""
    dll.GrannyGetTotalObjectSize.restype  = ctypes.c_int
    dll.GrannyGetTotalObjectSize.argtypes = [ctypes.c_void_p]
    dll.GrannyGetMemberTypeName.restype   = ctypes.c_char_p
    dll.GrannyGetMemberTypeName.argtypes  = [ctypes.c_int]
    return dll


# ---------------------------------------------------------------------------
# Standalone diagnostic
# ---------------------------------------------------------------------------

def _standalone_test(dll_path: str):
    dll = ctypes.CDLL(dll_path)
    setup_dll_types(dll)

    print(f"Loading type map from: {dll_path}\n")
    types = build_type_map(dll)
    print("Validation passed.\n")

    for struct_name, fields in types.items():
        print(f"{'-'*60}")
        print(f"  {struct_name}")
        for fname, off in sorted(fields.items(), key=lambda x: x[1]):
            marker = ''
            if (struct_name, fname) in _KNOWN_OFFSETS:
                marker = '  OK'
            print(f"    +0x{off:02X}  {fname}{marker}")

    print(f"\n{'-'*60}")
    print("granny_transform field offsets:")
    t = get_transform_field_offsets(dll)
    for k, v in t.items():
        print(f"  {k}: +0x{v:02X}")

    print(f"\n{'-'*60}")
    print("Struct strides:")
    for sym in ['GrannyBoneType', 'GrannyBoneBindingType', 'GrannyMeshType', 'GrannyVertexDataType']:
        try:
            stride = get_struct_stride(dll, sym)
            print(f"  {sym}: {stride} bytes")
        except AttributeError:
            print(f"  {sym}: NOT EXPORTED")


if __name__ == '__main__':
    dll_path = sys.argv[1] if len(sys.argv) > 1 else './granny2_x64.dll'
    if not os.path.exists(dll_path):
        print(f"ERROR: DLL not found at {dll_path}")
        print("Run from the game Ship/ directory or pass the path as an argument.")
        sys.exit(1)
    _standalone_test(dll_path)
