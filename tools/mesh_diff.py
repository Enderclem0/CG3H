"""
CG3H Mesh Diff — CC-free distribution for mesh_replace and mesh_patch mods.

Instead of shipping the full edited GLB (which contains original geometry),
ships only the vertex deltas. The builder applies deltas to the user's
original mesh to produce the modified version.

Diff format (.cg3h_diff):
    JSON with per-mesh vertex deltas:
    {
        "format": "cg3h-diff/1.0",
        "base_character": "Melinoe",
        "base_entry": "Melinoe_Mesh",
        "meshes": {
            "Melinoe_MeshShape": {
                "vertex_count": 7953,  # must match original
                "positions": [[idx, dx, dy, dz], ...],  # only changed vertices
                "normals": [[idx, dx, dy, dz], ...],
                "uvs": [[idx, du, dv], ...],
                "indices": [0, 1, 2, ...],  # full index buffer if topology changed, null if same
            }
        },
        "new_meshes": {
            "CustomArmor_MeshShape": {
                # Full data for truly new meshes (these are CC-free by nature)
                "positions": [[x,y,z], ...],
                "normals": [[x,y,z], ...],
                "uvs": [[u,v], ...],
                "weights": [[w0,w1,w2,w3], ...],
                "joints": [[j0,j1,j2,j3], ...],
                "indices": [0,1,2,...],
                "bone_palette": ["bone1", "bone2", ...],
                "texture": "CustomArmor_Color"
            }
        }
    }
"""
import json
import sys
import os
import struct
import numpy as np

_tools_dir = os.path.dirname(os.path.abspath(__file__))
if _tools_dir not in sys.path:
    sys.path.insert(0, _tools_dir)


def create_diff(original_glb_path, modified_glb_path, output_path, threshold=0.0001):
    """
    Create a mesh diff between original and modified GLBs.
    Only stores vertex deltas that exceed the threshold.
    Returns the diff dict.
    """
    from gltf_to_gr2 import parse_glb

    orig_meshes = {m['name']: m for m in parse_glb(original_glb_path)}
    mod_meshes = {m['name']: m for m in parse_glb(modified_glb_path)}

    diff = {
        'format': 'cg3h-diff/1.0',
        'meshes': {},
        'new_meshes': {},
    }

    for name, mod_m in mod_meshes.items():
        if name in orig_meshes:
            orig_m = orig_meshes[name]
            mesh_diff = _diff_mesh(orig_m, mod_m, threshold)
            if mesh_diff:
                diff['meshes'][name] = mesh_diff
        else:
            # New mesh — store full data (CC-free)
            new_mesh = {
                'positions': mod_m['positions'].tolist(),
                'normals': mod_m['normals'].tolist() if mod_m.get('normals') is not None else None,
                'uvs': mod_m['uvs'].tolist() if mod_m.get('uvs') is not None else None,
                'indices': mod_m['indices'].tolist(),
            }
            if mod_m.get('bw_u8') is not None:
                new_mesh['weights'] = mod_m['bw_u8'].tolist()
                new_mesh['joints'] = mod_m['bj_u8'].tolist()
                new_mesh['bone_palette'] = mod_m.get('bone_palette', [])
            if mod_m.get('texture_name'):
                new_mesh['texture'] = mod_m['texture_name']
            diff['new_meshes'][name] = new_mesh

    # Stats
    n_modified = len(diff['meshes'])
    n_new = len(diff['new_meshes'])
    total_deltas = sum(
        len(m.get('positions', [])) + len(m.get('normals', [])) + len(m.get('uvs', []))
        for m in diff['meshes'].values()
    )

    with open(output_path, 'w') as f:
        json.dump(diff, f)

    # Compute sizes
    orig_size = os.path.getsize(modified_glb_path)
    diff_size = os.path.getsize(output_path)
    print(f"  Diff: {n_modified} modified mesh(es), {n_new} new mesh(es)")
    print(f"  {total_deltas} vertex deltas")
    print(f"  Size: {diff_size:,} bytes (vs {orig_size:,} for full GLB, "
          f"{diff_size*100//max(orig_size,1)}% of original)")

    return diff


def _diff_mesh(orig, mod, threshold):
    """Compute per-vertex deltas between original and modified mesh."""
    o_pos = orig['positions']
    m_pos = mod['positions']

    result = {'vertex_count': len(o_pos)}
    has_changes = False

    # Same vertex count — compute deltas
    if len(o_pos) == len(m_pos):
        # Position deltas
        pos_diff = m_pos - o_pos
        changed = np.any(np.abs(pos_diff) > threshold, axis=1)
        if np.any(changed):
            indices = np.where(changed)[0]
            result['positions'] = [[int(i), *pos_diff[i].tolist()] for i in indices]
            has_changes = True

        # Normal deltas
        o_nrm = orig.get('normals')
        m_nrm = mod.get('normals')
        if o_nrm is not None and m_nrm is not None and len(o_nrm) == len(m_nrm):
            nrm_diff = m_nrm - o_nrm
            changed = np.any(np.abs(nrm_diff) > threshold, axis=1)
            if np.any(changed):
                indices = np.where(changed)[0]
                result['normals'] = [[int(i), *nrm_diff[i].tolist()] for i in indices]
                has_changes = True

        # UV deltas
        o_uv = orig.get('uvs')
        m_uv = mod.get('uvs')
        if o_uv is not None and m_uv is not None and len(o_uv) == len(m_uv):
            uv_diff = m_uv - o_uv
            changed = np.any(np.abs(uv_diff) > threshold, axis=1)
            if np.any(changed):
                indices = np.where(changed)[0]
                result['uvs'] = [[int(i), *uv_diff[i].tolist()] for i in indices]
                has_changes = True

        # Index buffer — check if changed
        o_idx = orig.get('indices')
        m_idx = mod.get('indices')
        if o_idx is not None and m_idx is not None:
            if not np.array_equal(o_idx, m_idx):
                result['indices'] = m_idx.tolist()
                has_changes = True

    else:
        # Different vertex count — topology change, store full modified data
        result['topology_changed'] = True
        result['new_vertex_count'] = len(m_pos)
        result['positions'] = m_pos.tolist()
        if mod.get('normals') is not None:
            result['normals'] = mod['normals'].tolist()
        if mod.get('uvs') is not None:
            result['uvs'] = mod['uvs'].tolist()
        result['indices'] = mod['indices'].tolist()
        if mod.get('bw_u8') is not None:
            result['weights'] = mod['bw_u8'].tolist()
            result['joints'] = mod['bj_u8'].tolist()
            result['bone_palette'] = mod.get('bone_palette', [])
        has_changes = True

    return result if has_changes else None


def apply_diff(original_glb_path, diff_path, output_glb_path):
    """
    Apply a mesh diff to an original GLB to produce the modified version.
    This runs on the user's machine using their original game-exported GLB.
    """
    import pygltflib

    with open(diff_path) as f:
        diff = json.load(f)

    gltf = pygltflib.GLTF2().load(original_glb_path)
    blob = bytearray(gltf.binary_blob())

    for gltf_mesh in gltf.meshes:
        name = gltf_mesh.name
        if name not in diff.get('meshes', {}):
            continue

        mesh_diff = diff['meshes'][name]

        for prim in gltf_mesh.primitives:
            attrs = prim.attributes
            if attrs.POSITION is None:
                continue

            # Apply position deltas
            if 'positions' in mesh_diff and not mesh_diff.get('topology_changed'):
                pos_acc = gltf.accessors[attrs.POSITION]
                pos_bv = gltf.bufferViews[pos_acc.bufferView]
                pos_data = np.frombuffer(
                    blob[pos_bv.byteOffset:pos_bv.byteOffset + pos_bv.byteLength],
                    dtype=np.float32
                ).reshape(-1, 3).copy()

                for delta in mesh_diff['positions']:
                    idx = delta[0]
                    if idx < len(pos_data):
                        pos_data[idx] += np.array(delta[1:], dtype=np.float32)

                blob[pos_bv.byteOffset:pos_bv.byteOffset + pos_bv.byteLength] = pos_data.tobytes()

            # Apply normal deltas
            if 'normals' in mesh_diff and attrs.NORMAL is not None and not mesh_diff.get('topology_changed'):
                nrm_acc = gltf.accessors[attrs.NORMAL]
                nrm_bv = gltf.bufferViews[nrm_acc.bufferView]
                nrm_data = np.frombuffer(
                    blob[nrm_bv.byteOffset:nrm_bv.byteOffset + nrm_bv.byteLength],
                    dtype=np.float32
                ).reshape(-1, 3).copy()

                for delta in mesh_diff['normals']:
                    idx = delta[0]
                    if idx < len(nrm_data):
                        nrm_data[idx] += np.array(delta[1:], dtype=np.float32)

                blob[nrm_bv.byteOffset:nrm_bv.byteOffset + nrm_bv.byteLength] = nrm_data.tobytes()

            # Apply UV deltas
            if 'uvs' in mesh_diff and attrs.TEXCOORD_0 is not None and not mesh_diff.get('topology_changed'):
                uv_acc = gltf.accessors[attrs.TEXCOORD_0]
                uv_bv = gltf.bufferViews[uv_acc.bufferView]
                uv_data = np.frombuffer(
                    blob[uv_bv.byteOffset:uv_bv.byteOffset + uv_bv.byteLength],
                    dtype=np.float32
                ).reshape(-1, 2).copy()

                for delta in mesh_diff['uvs']:
                    idx = delta[0]
                    if idx < len(uv_data):
                        uv_data[idx] += np.array(delta[1:], dtype=np.float32)

                blob[uv_bv.byteOffset:uv_bv.byteOffset + uv_bv.byteLength] = uv_data.tobytes()

    gltf.set_binary_blob(bytes(blob))
    gltf.save(output_glb_path)
    print(f"  Applied diff -> {output_glb_path}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="CG3H Mesh Diff Tool")
    sub = parser.add_subparsers(dest='command')

    p_create = sub.add_parser('create', help='Create diff between original and modified GLB')
    p_create.add_argument('original', help='Original exported GLB')
    p_create.add_argument('modified', help='Modified GLB from Blender')
    p_create.add_argument('-o', '--output', default=None, help='Output .cg3h_diff path')
    p_create.add_argument('--threshold', type=float, default=0.0001,
                          help='Min delta to include (default: 0.0001)')

    p_apply = sub.add_parser('apply', help='Apply diff to original GLB')
    p_apply.add_argument('original', help='Original exported GLB')
    p_apply.add_argument('diff', help='.cg3h_diff file')
    p_apply.add_argument('-o', '--output', default=None, help='Output GLB path')

    args = parser.parse_args()
    if args.command == 'create':
        output = args.output or args.modified.replace('.glb', '.cg3h_diff')
        create_diff(args.original, args.modified, output, args.threshold)
    elif args.command == 'apply':
        output = args.output or args.original.replace('.glb', '_patched.glb')
        apply_diff(args.original, args.diff, output)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
