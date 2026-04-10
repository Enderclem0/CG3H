"""
Pure helper functions for the CG3H Blender addon.

Importable from tests without bpy.  Contains the BoneBindings logic shared
between the Blender viewport (bone color overlay), the template toggle UI,
and the export-time validation.
"""


def select_template(manifest, active_bones, restrict_entries=None):
    """Pick the existing mesh whose BoneBindings best match the new mesh's bones.

    Mirrors the algorithm in tools/gltf_to_gr2.py:1587-1606.

    manifest:         dict (parsed manifest.json)
    active_bones:     iterable of bone names painted on the new mesh
    restrict_entries: optional set of entry names to limit candidates to

    Returns the manifest mesh dict, or None if no candidates.
    """
    if not manifest:
        return None
    active_bones = set(active_bones)
    candidates = manifest.get('meshes', [])
    if restrict_entries is not None:
        restrict = set(restrict_entries)
        candidates = [m for m in candidates if m.get('entry') in restrict]

    best = None
    best_score = -1
    for m in candidates:
        name = m.get('name', '')
        if 'Outline' in name or 'Shadow' in name:
            continue
        bb_names = set(m.get('bb_names', []))
        overlap = len(active_bones & bb_names)
        bb_count = len(bb_names)
        if best is None:
            best = m
            best_score = overlap
            continue
        # Higher overlap wins; ties broken by larger bb_count
        if overlap > best_score or (
                overlap == best_score and bb_count > len(best.get('bb_names', []))):
            best = m
            best_score = overlap
    return best


def find_weight_violations(mesh_vertex_data, bb_names_lookup):
    """Walk per-mesh vertex group weights and flag bones not in the allowed set.

    mesh_vertex_data: list of dicts
        {'name': str, 'is_original': bool,
         'groups': {vg_name: vertex_count_with_nonzero_weight}}
    bb_names_lookup: dict {mesh_name: set_of_allowed_bones}.  A mesh missing
        from this dict is skipped (no manifest data).

    Returns list of dicts:
        {'mesh': str, 'bone': str, 'vertex_count': int, 'is_original': bool}
    """
    violations = []
    for md in mesh_vertex_data:
        allowed = bb_names_lookup.get(md['name'])
        if allowed is None:
            continue
        for vg_name, vc in md.get('groups', {}).items():
            if vc > 0 and vg_name not in allowed:
                violations.append({
                    'mesh': md['name'],
                    'bone': vg_name,
                    'vertex_count': vc,
                    'is_original': md.get('is_original', False),
                })
    return violations
