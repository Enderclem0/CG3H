"""
CG3H — Custom Geometry 3D for Hades II (Blender Addon)

Adds File > Import/Export menu entries for Hades II models:
  Import: .gpk file → Blender scene (meshes + armature)
  Export: Blender scene → patched .gpk file

Self-contained: bundles cg3h_exporter.exe and cg3h_importer.exe,
no external Python or dependencies required.
"""

bl_info = {
    "name": "CG3H — Hades II Model Tools",
    "author": "Enderclem",
    "version": (3, 4, 0),
    # Minimum Blender version.  4.2 is the current LTS and our floor for
    # testing / API targets.  Older versions may work but are unsupported.
    # Blender 4.3+ ships Python 3.12 where the stricter parent-package
    # import check made an earlier top-level importlib.reload crash with
    # "partially initialized module" — fixed in this file, but flagging
    # 4.2 as the verified baseline.
    "blender": (4, 2, 0),
    "location": "File > Import/Export",
    "description": "Import and export Hades II 3D models (.gpk)",
    "category": "Import-Export",
}

import bpy
import importlib
import json
import os
import re
import struct
import subprocess
import tempfile
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

# Pure helpers (importable from tests without bpy).
#
# Submodule reload uses Blender's standard "bpy in locals" guard: on the
# INITIAL import of this package `bpy` is not yet bound in module locals
# (we import it on line 22 but `locals()` checked below reflects the
# namespace as seen BEFORE this block), so we do a plain import.  On a
# subsequent reload triggered by addon-disable-then-enable, `bpy` IS
# already bound and we reload `cg3h_core` from disk so edits take effect
# without a full Blender restart.
#
# Unconditional top-level `importlib.reload(cg3h_core)` triggered a
# "partially initialized module" ImportError under Python 3.12+ because
# the reload ran while this package's __init__ was still executing —
# the import machinery's stricter parent-state check rejected it.
if "cg3h_core" in locals():
    importlib.reload(cg3h_core)
else:
    from . import cg3h_core


# ── Default paths ─────────────────────────────────────────────────────────────

def _find_game_path():
    """Find Hades II via Steam registry + libraryfolders.vdf, then fallback paths."""
    # Try Steam registry (Windows only — winreg deferred for cross-platform safety)
    try:
        import winreg
        for hive, subkey in [
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
        ]:
            try:
                key = winreg.OpenKey(hive, subkey)
                for val in ("SteamPath", "InstallPath"):
                    try:
                        steam_root, _ = winreg.QueryValueEx(key, val)
                        if steam_root and os.path.isdir(steam_root):
                            # Parse libraryfolders.vdf
                            vdf = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
                            libraries = [steam_root]
                            if os.path.isfile(vdf):
                                with open(vdf, encoding="utf-8", errors="replace") as f:
                                    for m in re.finditer(r'"path"\s+"([^"]+)"', f.read()):
                                        p = m.group(1).replace("\\\\", "\\")
                                        if os.path.isdir(p) and p not in libraries:
                                            libraries.append(p)
                            for lib in libraries:
                                game = os.path.join(lib, "steamapps", "common", "Hades II")
                                if os.path.isdir(game):
                                    return game
                    except OSError:
                        continue
            except OSError:
                continue
    except ImportError:
        pass  # winreg not available (non-Windows) — fall through to path scan
    # Fallback
    for p in [
        r"C:\Program Files (x86)\Steam\steamapps\common\Hades II",
        r"C:\Program Files\Steam\steamapps\common\Hades II",
        r"D:\SteamLibrary\steamapps\common\Hades II",
    ]:
        if os.path.isdir(p):
            return p
    return ""


def _addon_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _exe_path(name):
    """Find a bundled exe (cg3h_exporter.exe or cg3h_importer.exe)."""
    return os.path.join(_addon_dir(), name)


# ── Preferences ───────────────────────────────────────────────────────────────

class CG3HPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    game_path: StringProperty(
        name="Hades II Game Directory",
        description="Root of the Hades II installation (contains Ship/ and Content/)",
        subtype='DIR_PATH',
        default=_find_game_path(),
    )
    author: StringProperty(
        name="Default Author",
        description="Your modder name (saved for future exports)",
        default="Modder",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "game_path")
        layout.prop(self, "author")

        # Validation
        issues = []
        if not self.game_path or not os.path.isdir(self.game_path):
            issues.append("Game directory not found")
        else:
            dll = os.path.join(self.game_path, "Ship", "granny2_x64.dll")
            gpk_dir = os.path.join(self.game_path, "Content", "GR2", "_Optimized")
            if not os.path.isfile(dll):
                issues.append("granny2_x64.dll not found in Ship/")
            if not os.path.isdir(gpk_dir):
                issues.append("Content/GR2/_Optimized/ not found")

        if not os.path.isfile(_exe_path("cg3h_exporter.exe")):
            issues.append("cg3h_exporter.exe not found in addon directory")
        if not os.path.isfile(_exe_path("cg3h_importer.exe")):
            issues.append("cg3h_importer.exe not found in addon directory")

        if issues:
            box = layout.box()
            for issue in issues:
                box.label(text=issue, icon='ERROR')
        else:
            layout.label(text="All paths valid", icon='CHECKMARK')


def _prefs():
    return bpy.context.preferences.addons[__package__].preferences


def _gpk_dir():
    return os.path.join(_prefs().game_path, "Content", "GR2", "_Optimized")


def _dll_path():
    return os.path.join(_prefs().game_path, "Ship", "granny2_x64.dll")


# ── Character list for enum ───────────────────────────────────────────────────

# Module-level caches for enum item lists.  Blender's EnumProperty(items=callable)
# stores raw C string pointers from the returned list, so the list MUST live
# longer than Blender's lookup.  Caching at module level avoids the GC crash
# documented at https://docs.blender.org/api/current/bpy.props.html#bpy.props.EnumProperty
_characters_items_cache = []
_characters_items_key = None
def _get_characters(self, context):
    """Build enum items list from available .gpk files."""
    global _characters_items_cache, _characters_items_key
    gpk_dir = _gpk_dir()
    key = gpk_dir if os.path.isdir(gpk_dir) else None
    if key == _characters_items_key:
        return _characters_items_cache

    if key is None:
        _characters_items_cache = [("NONE", "No models found",
                                    "Set game path in addon preferences")]
    else:
        names = sorted(
            os.path.splitext(f)[0]
            for f in os.listdir(gpk_dir) if f.endswith(".gpk")
        )
        if names:
            _characters_items_cache = [(n, n, f"{n}.gpk") for n in names]
        else:
            _characters_items_cache = [("NONE", "No models found", "")]
    _characters_items_key = key
    return _characters_items_cache


# ── Armature lookup ───────────────────────────────────────────────────────────

def _find_armature_for(mesh_obj, context=None):
    """Return the armature driving this mesh.

    Lookup order:
    1. Armature modifier on the mesh
    2. Direct ARMATURE parent
    3. Fallback: scene-level CG3H armature (any armature whose name contains
       the imported character name) — useful when the modder added a new mesh
       but hasn't yet parented it.
    """
    for mod in mesh_obj.modifiers:
        if mod.type == 'ARMATURE' and mod.object and mod.object.type == 'ARMATURE':
            return mod.object
    if mesh_obj.parent and mesh_obj.parent.type == 'ARMATURE':
        return mesh_obj.parent
    if context is None:
        context = bpy.context
    character = context.scene.get("cg3h_character", "")
    for o in bpy.data.objects:
        if o.type != 'ARMATURE':
            continue
        if not character or character.lower() in o.name.lower():
            return o
    return None


# ── Manifest helpers ──────────────────────────────────────────────────────────

def _get_manifest(context):
    """Parse cached manifest JSON from the scene. Returns dict or None."""
    raw = context.scene.get("cg3h_manifest_json", "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _get_mesh_bb_names(context, mesh_obj_name):
    """Return the bone-binding name list for a mesh, or None if not in manifest."""
    m = _get_manifest(context)
    if not m:
        return None
    for entry in m.get('meshes', []):
        if entry.get('name') == mesh_obj_name:
            return entry.get('bb_names')
    return None


def _is_original_mesh(context, mesh_obj_name):
    """True if this mesh was imported from the GR2 (not added in Blender)."""
    originals = context.scene.get("cg3h_original_meshes", "")
    return mesh_obj_name in [n for n in originals.split(",") if n]


def _get_mesh_target_entries(obj, all_entries):
    """Return list of entries the mesh targets via cg3h_entry_* checkboxes."""
    return [e for e in all_entries if obj.get(f"cg3h_entry_{e}", True)]


def _resolve_validation_template(manifest, preset, obj, all_entries):
    """Determine which template a NEW mesh's weights will be validated against.

    Returns (template_name, bone_count) or (None, 0) if not resolvable.

    - If the preset is M:<name>, that mesh is the template.
    - If the preset is E:<entry>, auto-select within that entry.
    - If the preset is ALL, auto-select within the mesh's own target entries.
    """
    if not manifest:
        return None, 0

    if preset and preset.startswith("M:"):
        target_name = preset[2:]
        for mesh in manifest.get('meshes', []):
            if mesh.get('name') == target_name:
                return target_name, len(mesh.get('bb_names', []))
        return None, 0

    # Auto-select via select_template, restricting candidates by entries
    if preset and preset.startswith("E:"):
        restrict = {preset[2:]}
    else:
        restrict = set(_get_mesh_target_entries(obj, all_entries))
        restrict = restrict if restrict else None

    active = set(vg.name for vg in obj.vertex_groups)
    tpl = cg3h_core.select_template(manifest, active, restrict_entries=restrict)
    if tpl:
        return tpl.get('name', ''), len(tpl.get('bb_names', []))
    return None, 0


# ── Bone visibility presets ───────────────────────────────────────────────────

# Module-level cache for the preset enum (avoids Blender enum-callback GC crash).
_bone_preset_items_cache = [
    ("WHOLE", "Whole armature", "Show every bone in the rig (no filter)"),
    ("ALL", "All routed bones", "Show bones bound by any mesh in the routed entries"),
]
_bone_preset_items_key = None


def _get_bone_preset_items(self, context):
    """Build the bone visibility preset enum: All + per-entry + per-mesh.

    Items are filtered by the active mesh's routing (cg3h_entry_* checkboxes).
    Only entries the active mesh targets — and meshes inside those entries —
    appear as presets.  For original meshes, the active mesh's own entry is
    used.  Cache key includes target entries so the list updates when the
    modder toggles routing checkboxes.
    """
    global _bone_preset_items_cache, _bone_preset_items_key
    character = context.scene.get("cg3h_character", "")

    # Compute target entries from the active mesh's routing
    target_entries = ()
    obj = context.active_object
    if obj is not None and obj.type == 'MESH':
        entries_str = context.scene.get("cg3h_entries", "")
        all_entries = [e for e in entries_str.split(",") if e]
        if _is_original_mesh(context, obj.name):
            # Look up the original mesh's entry from the manifest
            m = _get_manifest(context)
            if m:
                for mesh in m.get('meshes', []):
                    if mesh.get('name') == obj.name:
                        own = mesh.get('entry', '')
                        if own:
                            target_entries = (own,)
                        break
        else:
            target_entries = tuple(sorted(_get_mesh_target_entries(obj, all_entries)))

    # Include manifest length in the cache key so a re-import (different
    # character or refreshed manifest) invalidates the cache.
    manifest_len = len(context.scene.get("cg3h_manifest_json", ""))
    key = (character, target_entries, manifest_len)
    if key == _bone_preset_items_key:
        return _bone_preset_items_cache

    items = [
        ("WHOLE", "Whole armature", "Show every bone in the rig (no filter)"),
        ("ALL", "All routed bones",
         "Show bones bound by any mesh in this mesh's routed entries"),
    ]
    m = _get_manifest(context)
    if m:
        target_set = set(target_entries) if target_entries else None
        # Per-entry presets — union of all bb_names for meshes in that entry
        seen = set()
        for mesh in m.get('meshes', []):
            entry = mesh.get('entry', '')
            if not entry or entry in seen:
                continue
            if target_set is not None and entry not in target_set:
                continue
            items.append((f"E:{entry}", f"Entry: {entry}",
                          f"Show only bones used by any mesh in {entry}"))
            seen.add(entry)
        # Per-mesh presets (also filtered by target entries)
        for mesh in m.get('meshes', []):
            nm = mesh.get('name', '')
            bb = mesh.get('bb_names', [])
            if not bb or 'Outline' in nm or 'Shadow' in nm:
                continue
            if target_set is not None and mesh.get('entry') not in target_set:
                continue
            short = nm.split(':')[-1]
            items.append((f"M:{nm}", f"Mesh: {short}",
                          f"Show only bones bound by {short} ({len(bb)})"))
    _bone_preset_items_cache = items
    _bone_preset_items_key = key
    return _bone_preset_items_cache


def _compute_visible_bones(manifest, preset, target_entries=None):
    """Return the set of bone names that should be visible for a preset.

    target_entries: the active mesh's routed entries.  Used by the 'ALL'
    preset to compute the union of routed bb_names.
    Returns None to mean 'show every bone in the armature' (literal whole
    rig — used for the WHOLE preset and as a fallback when there is no
    routing context).
    """
    if not manifest or preset == "WHOLE":
        return None

    if preset == "ALL":
        if not target_entries:
            return None  # no routing context — show literally everything
        v = set()
        for mesh in manifest.get('meshes', []):
            if mesh.get('entry') in target_entries:
                v.update(mesh.get('bb_names', []))
        return v

    if preset.startswith("E:"):
        entry = preset[2:]
        visible = set()
        for mesh in manifest.get('meshes', []):
            if mesh.get('entry') == entry:
                visible.update(mesh.get('bb_names', []))
        return visible

    if preset.startswith("M:"):
        target_name = preset[2:]
        for mesh in manifest.get('meshes', []):
            if mesh.get('name') == target_name:
                return set(mesh.get('bb_names', []))
        return set()

    return None


def _apply_bone_preset(self, context):
    """Property update callback — set bone hide flags based on the preset."""
    arm = None
    character = context.scene.get("cg3h_character", "")
    for o in bpy.data.objects:
        if o.type == 'ARMATURE' and (not character
                                     or character.lower() in o.name.lower()):
            arm = o
            break
    if arm is None:
        return

    manifest = _get_manifest(context)
    preset = context.scene.cg3h_bone_preset

    # Validate: if the preset is no longer in the available items (e.g. the
    # modder unchecked an entry after selecting a mesh from it), snap to ALL.
    valid_keys = {item[0] for item in _get_bone_preset_items(self, context)}
    if preset not in valid_keys:
        context.scene.cg3h_bone_preset = "ALL"
        return  # the assignment above re-fires this callback

    # Save original hide state once per armature so we can restore on full reset
    if "_cg3h_saved_hide" not in arm:
        saved = {b.name: b.hide for b in arm.data.bones}
        arm["_cg3h_saved_hide"] = json.dumps(saved)

    # Determine target entries from the active mesh's routing
    target_entries = None
    obj = context.active_object
    if obj is not None and obj.type == 'MESH':
        entries_str = context.scene.get("cg3h_entries", "")
        all_entries = [e for e in entries_str.split(",") if e]
        if _is_original_mesh(context, obj.name):
            for mesh in (manifest or {}).get('meshes', []):
                if mesh.get('name') == obj.name:
                    own = mesh.get('entry', '')
                    if own:
                        target_entries = {own}
                    break
        else:
            t = _get_mesh_target_entries(obj, all_entries)
            target_entries = set(t) if t else None

    visible = _compute_visible_bones(manifest, preset, target_entries)
    if visible is None:
        # Restore original state (no routing constraint)
        try:
            saved = json.loads(arm.get("_cg3h_saved_hide", "{}"))
        except Exception:
            saved = {}
        for bone in arm.data.bones:
            bone.hide = saved.get(bone.name, False)
        return

    for bone in arm.data.bones:
        bone.hide = bone.name not in visible


def _cycle_bone_preset(context, direction):
    """Move the preset enum by +/-1 in the items list."""
    items = _get_bone_preset_items(None, context)
    if len(items) <= 1:
        return
    current = context.scene.cg3h_bone_preset
    keys = [it[0] for it in items]
    try:
        idx = keys.index(current)
    except ValueError:
        idx = 0
    new_idx = (idx + direction) % len(keys)
    context.scene.cg3h_bone_preset = keys[new_idx]


# ── Import Operator ───────────────────────────────────────────────────────────

class CG3H_OT_Import(bpy.types.Operator, ImportHelper):
    """Import a Hades II model (.gpk) into the scene"""
    bl_idname = "import_scene.cg3h_gpk"
    bl_label = "Import Hades II Model"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".gpk"
    filter_glob: StringProperty(default="*.gpk", options={'HIDDEN'}, maxlen=255)

    textures: BoolProperty(
        name="Include Textures",
        description="Embed character textures from .pkg files into the GLB",
        default=True,
    )
    animations: BoolProperty(
        name="Include Animations",
        description="Import animation data (can be slow for characters with many animations)",
        default=False,
    )
    anim_filter: StringProperty(
        name="Animation Filter",
        description="Only import animations matching this pattern (e.g. 'Idle'). Leave empty for all.",
        default="",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "textures")
        layout.prop(self, "animations")
        if self.animations:
            layout.prop(self, "anim_filter")

    def execute(self, context):
        exporter_exe = _exe_path("cg3h_exporter.exe")
        if not os.path.isfile(exporter_exe):
            self.report({'ERROR'}, "cg3h_exporter.exe not found in addon directory")
            return {'CANCELLED'}

        gpk_path = self.filepath
        gpk_dir = os.path.dirname(gpk_path)
        name = os.path.splitext(os.path.basename(gpk_path))[0]
        dll = _dll_path()

        if not os.path.isfile(dll):
            self.report({'ERROR'}, "granny2_x64.dll not found. Set game path in addon preferences.")
            return {'CANCELLED'}

        tmp_glb = tempfile.mktemp(suffix=".glb")

        cmd = [
            exporter_exe, name,
            "--gpk-dir", gpk_dir,
            "--dll", dll,
            "-o", tmp_glb,
        ]
        if self.textures:
            cmd.append("--textures")
        if self.animations:
            cmd.append("--animations")
            if self.anim_filter.strip():
                cmd += ["--anim-filter", self.anim_filter.strip()]

        timeout = 600 if self.animations else 120
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                cwd=os.path.join(_prefs().game_path, "Ship"),
            )
            if result.returncode != 0:
                self.report({'ERROR'}, f"Export failed:\n{result.stderr or result.stdout}")
                return {'CANCELLED'}
        except subprocess.TimeoutExpired:
            self.report({'ERROR'}, f"Export timed out (>{timeout}s)")
            return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f"Export error: {e}")
            return {'CANCELLED'}

        if not os.path.isfile(tmp_glb):
            self.report({'ERROR'}, "Export produced no output file")
            return {'CANCELLED'}

        # Read manifest written alongside the temp GLB by the exporter.
        # Cache as JSON string on the scene so panels and validators can use it.
        manifest_path = os.path.join(os.path.dirname(tmp_glb), "manifest.json")
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as mf:
                    context.scene.cg3h_manifest_json = mf.read()
            except Exception as e:
                self.report({'WARNING'}, f"Manifest read failed: {e}")
            try:
                os.unlink(manifest_path)
            except OSError:
                pass  # temp manifest cleanup is best-effort

        bpy.ops.import_scene.gltf(filepath=tmp_glb)

        try:
            os.unlink(tmp_glb)
        except OSError:
            pass  # temp GLB cleanup is best-effort

        # Auto-load mesh entries for the CG3H panel
        entries = _read_gpk_entries(name)
        if entries:
            context.scene.cg3h_entries = ",".join(entries)
            context.scene.cg3h_character = name
            # Store imported mesh names so the panel can hide checkboxes for them
            imported_names = [obj.name for obj in context.selected_objects if obj.type == 'MESH']
            context.scene.cg3h_original_meshes = ",".join(imported_names)
            # Init default entry assignments for all imported meshes
            for obj in context.selected_objects:
                if obj.type == 'MESH':
                    for entry in entries:
                        obj[f"cg3h_entry_{entry}"] = True

        self.report({'INFO'}, f"Imported {name} ({len(context.selected_objects)} objects)")
        return {'FINISHED'}


# ── Export Operator ───────────────────────────────────────────────────────────

class CG3H_OT_Export(bpy.types.Operator):
    """Export selected meshes as a CG3H mod (GLB + mod.json + Thunderstore ZIP)"""
    bl_idname = "export_scene.cg3h_gpk"
    bl_label = "Export as Hades II Mod"
    bl_options = {'REGISTER'}

    character: EnumProperty(
        name="Original Character",
        description="Which character this mod targets",
        items=_get_characters,
    )
    mod_name: StringProperty(
        name="Mod Name",
        description="Name for your mod",
        default="MyMod",
    )
    author: StringProperty(
        name="Author",
        description="Your name (loaded from addon preferences)",
        default="Modder",
    )
    output_dir: StringProperty(
        name="Output Directory",
        description="Where to create the mod workspace",
        subtype='DIR_PATH',
        default=os.path.join(os.path.expanduser("~"), "Documents", "CG3H_Mods"),
    )
    skip_validation: BoolProperty(
        name="Skip Validation",
        default=False,
        options={'HIDDEN', 'SKIP_SAVE'},
    )

    def invoke(self, context, event):
        # Load author from preferences
        self.author = _prefs().author

        # Auto-detect character from selected objects
        for obj in context.selected_objects:
            name = obj.name or ""
            # Try matching armature/skin name (e.g. "Melinoe_Skin", "Moros_Rig:...")
            for part in [name.split("_")[0], name.split(":")[0].split("_")[0]]:
                items = _get_characters(self, context)
                for value, label, _ in items:
                    if value.lower() == part.lower():
                        self.character = value
                        break
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "character")
        layout.prop(self, "mod_name")
        layout.prop(self, "author")
        layout.prop(self, "output_dir")

    def execute(self, context):
        if self.character == "NONE":
            self.report({'ERROR'}, "No character selected. Set game path in addon preferences.")
            return {'CANCELLED'}

        # Pre-flight: validate bone bindings unless explicitly skipped
        if not self.skip_validation:
            try:
                violations = self._check_bone_bindings(context)
            except Exception as e:
                print(f"[CG3H] Bone binding validation failed, skipping: {e}")
                violations = []
            if violations:
                _violations_cache.clear()
                _violations_cache.extend(violations)
                bpy.ops.cg3h.export_violations_confirm('INVOKE_DEFAULT')
                return {'CANCELLED'}

        character = self.character
        mod_name = self.mod_name.strip() or "MyMod"
        author = self.author.strip() or "Modder"
        workspace = os.path.join(self.output_dir, mod_name)
        os.makedirs(workspace, exist_ok=True)

        # Export GLB
        glb_path = os.path.join(workspace, f"{character}.glb")
        bpy.ops.export_scene.gltf(
            filepath=glb_path,
            use_selection=True,
            export_format='GLB',
            export_normals=False,
            export_tangents=False,
            export_yup=True,
        )

        if not os.path.isfile(glb_path):
            self.report({'ERROR'}, "glTF export produced no file. Select meshes + armature first.")
            return {'CANCELLED'}

        # Build mesh entry list and per-mesh routing from CG3H panel properties
        entries_str = context.scene.get("cg3h_entries", "")
        all_entries = entries_str.split(",") if entries_str else [f"{character}_Mesh"]
        all_entries = [e for e in all_entries if e]  # filter empties

        original_meshes = set(context.scene.get("cg3h_original_meshes", "").split(","))

        # Auto-detect mod type from the selected meshes.  A mesh whose
        # name matches a stock mesh (was in the imported GR2) is a
        # replacement; any other mesh is a new addition.
        has_replace = False
        has_add = False
        new_mesh_routing = {}
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            if obj.name in original_meshes:
                has_replace = True
                continue  # Original meshes routed by manifest
            has_add = True
            # Auto-init if no entry properties (mesh added without clicking Init)
            mesh_entries = []
            has_props = False
            for entry in all_entries:
                prop_name = f"cg3h_entry_{entry}"
                if prop_name not in obj:
                    obj[prop_name] = True  # default: all entries
                has_props = True
                if obj[prop_name]:
                    mesh_entries.append(entry)
            # Always write routing for new meshes so runtime per-mesh
            # visibility can locate them (rom.data.set_mesh_visible).
            # The routing's value may be the full entry list — that's
            # fine; partial vs full is distinguished by content, not by
            # the presence of the key.
            if has_props and mesh_entries:
                new_mesh_routing[obj.name] = mesh_entries

        target = {
            "character": character,
            "mesh_entries": all_entries,
        }
        if new_mesh_routing:
            target["new_mesh_routing"] = new_mesh_routing

        # type field shape: single string when only one, list when both.
        # Fallback to "mesh_replace" when neither (selection contained no
        # meshes — shouldn't happen in practice) so existing builder
        # classification still routes it correctly.
        if has_replace and has_add:
            mod_type = ["mesh_add", "mesh_replace"]
        elif has_add:
            mod_type = "mesh_add"
        else:
            mod_type = "mesh_replace"

        mod_json = {
            "format": "cg3h-mod/1.0",
            "metadata": {
                "name": mod_name,
                "author": author,
                "version": "1.0.0",
                "description": f"{mod_name} for {character}",
            },
            "type": mod_type,
            "target": target,
            "assets": {
                "glb": f"{character}.glb",
            },
        }
        with open(os.path.join(workspace, "mod.json"), "w") as f:
            json.dump(mod_json, f, indent=2)

        # Copy icon if available
        icon_src = os.path.join(_addon_dir(), "..", "icon.png")
        if not os.path.isfile(icon_src):
            icon_src = os.path.join(_addon_dir(), "..", "..", "icon.png")
        if os.path.isfile(icon_src):
            import shutil
            shutil.copy2(icon_src, os.path.join(workspace, "icon.png"))

        # Run exporter to generate manifest.json (for mesh routing)
        exporter_exe = _exe_path("cg3h_exporter.exe")
        gpk_dir = _gpk_dir()
        dll = _dll_path()
        if os.path.isfile(exporter_exe) and os.path.isfile(dll):
            manifest_glb = tempfile.mktemp(suffix=".glb")
            cmd = [
                exporter_exe, character,
                "--gpk-dir", gpk_dir,
                "--dll", dll,
                "-o", manifest_glb,
            ]
            try:
                subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120,
                    cwd=os.path.join(_prefs().game_path, "Ship"),
                )
                # Exporter writes manifest.json in the same dir as the output GLB
                manifest_src = os.path.join(os.path.dirname(manifest_glb), "manifest.json")
                if os.path.isfile(manifest_src):
                    import shutil
                    shutil.copy2(manifest_src, os.path.join(workspace, "manifest.json"))
                    os.unlink(manifest_src)
            except Exception as e:
                print(f"[CG3H] Manifest sync skipped: {e}")
            finally:
                if os.path.isfile(manifest_glb):
                    os.unlink(manifest_glb)

        # Build mod package (PKG + Thunderstore ZIP)
        # Run cg3h_build.py as subprocess with system Python since Blender's
        # Python doesn't have etcpak/Pillow needed for texture compression.
        zip_path = None
        build_script = os.path.join(_addon_dir(), "cg3h_build.py")
        if os.path.isfile(build_script):
            import shutil
            system_python = shutil.which("python") or shutil.which("python3") or shutil.which("py")
            if system_python:
                try:
                    result = subprocess.run(
                        [system_python, build_script, workspace, "--package"],
                        capture_output=True, text=True, timeout=120,
                    )
                    if result.returncode == 0:
                        mod_id = f"{author}-{mod_name}".replace(" ", "")
                        zip_name = f"{mod_id}-1.0.0.zip"
                        zip_path = os.path.join(workspace, zip_name)
                    else:
                        print(f"CG3H build failed:\n{result.stderr or result.stdout}")
                except Exception as build_err:
                    print(f"CG3H build error: {build_err}")

        if zip_path and os.path.isfile(zip_path):
            self.report({'INFO'}, f"Mod ready: {zip_path}")
        else:
            self.report({'INFO'}, f"Workspace created: {workspace} (build manually with CG3H GUI)")
        return {'FINISHED'}

    def _check_bone_bindings(self, context):
        """Walk selected meshes' vertex groups and flag weights on bones not in
        the resolved BoneBindings.

        Originals validate against their own bb_names.  New meshes validate
        against the template resolved by the scene's cg3h_bone_preset:
          - M:<name>  → use that mesh's bb_names directly
          - E:<entry> → auto-select template within that entry
          - ALL       → auto-select template within the new mesh's target entries
        """
        manifest = _get_manifest(context)
        if not manifest:
            return []

        original_meshes = set(
            n for n in context.scene.get("cg3h_original_meshes", "").split(",") if n)
        entries_str = context.scene.get("cg3h_entries", "")
        all_entries = [e for e in entries_str.split(",") if e]
        preset = context.scene.cg3h_bone_preset

        bb_lookup = {}
        mesh_data = []
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue

            if obj.name in original_meshes:
                bb = _get_mesh_bb_names(context, obj.name)
                if bb is None:
                    continue
                allowed = set(bb)
                is_original = True
            else:
                tpl_name, _ = _resolve_validation_template(
                    manifest, preset, obj, all_entries)
                if tpl_name:
                    bb = _get_mesh_bb_names(context, tpl_name)
                    allowed = set(bb) if bb else set()
                else:
                    allowed = set()
                is_original = False
            bb_lookup[obj.name] = allowed

            # Count vertices with non-zero weight per group
            groups = {}
            for v in obj.data.vertices:
                for vge in v.groups:
                    if vge.weight > 1e-6:
                        vg_name = obj.vertex_groups[vge.group].name
                        groups[vg_name] = groups.get(vg_name, 0) + 1
            mesh_data.append({
                'name': obj.name,
                'is_original': is_original,
                'groups': groups,
            })

        return cg3h_core.find_weight_violations(mesh_data, bb_lookup)


# ── Export validation popup ───────────────────────────────────────────────────

# Module-level violation cache: props_dialog can't pass complex data, so the
# main Export operator stashes its violations here for the confirm operator.
_violations_cache = []


class CG3H_OT_ExportViolationsConfirm(bpy.types.Operator):
    """Confirm dialog shown when bone weight violations are found."""
    bl_idname = "cg3h.export_violations_confirm"
    bl_label = "Bone Binding Violations Found"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=600)

    def draw(self, context):
        layout = self.layout
        layout.label(
            text=f"Found {len(_violations_cache)} weight violation(s):",
            icon='ERROR')
        layout.label(
            text="Weights on these bones will silently fall back to root (0).")
        layout.separator()

        by_mesh = {}
        for v in _violations_cache:
            by_mesh.setdefault(v['mesh'], []).append(v)
        for mesh_name, vs in by_mesh.items():
            box = layout.box()
            tag = " (existing)" if vs[0]['is_original'] else " (new)"
            box.label(text=f"{mesh_name}{tag}", icon='MESH_DATA')
            for v in vs[:10]:
                box.label(text=f"  • {v['bone']}: {v['vertex_count']} verts",
                          icon='BONE_DATA')
            if len(vs) > 10:
                box.label(text=f"  ...and {len(vs) - 10} more")

        layout.separator()
        layout.label(text="Click OK to export anyway, or Cancel to fix weights.")

    def execute(self, context):
        # User confirmed — re-run export with validation skipped
        bpy.ops.export_scene.cg3h_gpk('INVOKE_DEFAULT', skip_validation=True)
        return {'FINISHED'}


# ── Mesh Entry Assignment Panel ───────────────────────────────────────────────

def _read_gpk_entries(character):
    """Read mesh entry names from a character's GPK file."""
    gpk_dir = os.path.join(_prefs().game_path, "Content", "GR2", "_Optimized")
    gpk_path = os.path.join(gpk_dir, f"{character}.gpk")
    if not os.path.isfile(gpk_path):
        return []
    try:
        with open(gpk_path, 'rb') as f:
            data = f.read()
        count = struct.unpack_from('<I', data, 4)[0]
        pos = 8
        entries = []
        for _ in range(count):
            nl = data[pos]
            pos += 1
            name = data[pos:pos+nl].decode('utf-8', errors='replace')
            pos += nl
            cs = struct.unpack_from('<I', data, pos)[0]
            pos += 4
            if name.endswith('_Mesh'):
                entries.append(name)
            pos += cs
        return entries
    except Exception as e:
        print(f"[CG3H] Failed to parse GPK {gpk_path}: {e}")
        return []


class CG3H_OT_InitMeshEntries(bpy.types.Operator):
    """Initialize entry checkboxes for the active mesh"""
    bl_idname = "cg3h.init_mesh_entries"
    bl_label = "Enable Entry Selection"

    def execute(self, context):
        obj = context.active_object
        entries_str = context.scene.get("cg3h_entries", "")
        if not obj or obj.type != 'MESH' or not entries_str:
            return {'CANCELLED'}
        for entry in entries_str.split(","):
            if not entry:
                continue
            if f"cg3h_entry_{entry}" not in obj:
                obj[f"cg3h_entry_{entry}"] = True
        return {'FINISHED'}


class CG3H_PT_MeshEntries(bpy.types.Panel):
    """Panel in the 3D View sidebar for assigning meshes to GPK entries"""
    bl_label = "CG3H Entry Assignment"
    bl_idname = "CG3H_PT_mesh_entries"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'CG3H'

    def draw(self, context):
        layout = self.layout

        entries_str = context.scene.get("cg3h_entries", "")
        character = context.scene.get("cg3h_character", "")

        if not entries_str:
            layout.label(text="Import a character to see entries")
            return

        entries = [e for e in entries_str.split(",") if e]
        if len(entries) <= 1:
            layout.label(text=f"Single entry: {entries[0] if entries else '(none)'}")
            return

        layout.label(text=f"Character: {character} ({len(entries)} entries)")
        layout.separator()

        obj = context.active_object
        if not obj or obj.type != 'MESH':
            layout.label(text="Select a mesh to assign entries")
            return

        # Check if this is an original (imported) mesh
        original_meshes = [n for n in context.scene.get("cg3h_original_meshes", "").split(",") if n]
        if obj.name in original_meshes:
            layout.label(text=f"{obj.name}", icon='MESH_DATA')
            layout.label(text="Original mesh — routed via manifest", icon='INFO')
            return

        layout.label(text=f"{obj.name}", icon='MESH_DATA')

        # Check if entry properties exist on this mesh
        first_prop = f"cg3h_entry_{entries[0]}"
        if first_prop not in obj:
            layout.operator("cg3h.init_mesh_entries", text="Enable Entry Selection", icon='CHECKMARK')
            return

        for entry in entries:
            prop_name = f"cg3h_entry_{entry}"
            if prop_name in obj:
                layout.prop(obj, f'["{prop_name}"]', text=entry)

        # Bone visibility preset — hides bones not relevant to the chosen
        # template/entry. Also drives export validation: when a specific
        # template (M:...) is picked, the validator checks against THAT
        # template's bb_names instead of auto-selecting per mesh.
        layout.separator()
        vis_box = layout.box()
        vis_box.label(text="BoneBindings Template", icon='ARMATURE_DATA')
        row = vis_box.row(align=True)
        row.operator("cg3h.bone_preset_prev", text="", icon='TRIA_LEFT')
        row.prop(context.scene, "cg3h_bone_preset", text="")
        row.operator("cg3h.bone_preset_next", text="", icon='TRIA_RIGHT')

        # Show what template will be used for export validation
        manifest = _get_manifest(context)
        if manifest:
            preset = context.scene.cg3h_bone_preset
            tpl_name, tpl_count = _resolve_validation_template(
                manifest, preset, obj, entries)
            if tpl_name:
                vis_box.label(text=f"Validate vs: {tpl_name} ({tpl_count} bones)",
                              icon='BONE_DATA')

        # Skinning helper: parent + armature modifier
        layout.separator()
        diag = layout.box()
        arm = _find_armature_for(obj, context)
        if arm:
            diag.label(text=f"Armature: {arm.name}", icon='ARMATURE_DATA')
            has_mod = any(m.type == 'ARMATURE' and m.object == arm
                          for m in obj.modifiers)
            if has_mod:
                diag.label(text="Ready — Tab to Weight Paint",
                           icon='CHECKMARK')
            else:
                diag.label(text="Not linked to armature", icon='INFO')
                diag.operator("cg3h.setup_skinning",
                              text="Setup for Skinning", icon='ARMATURE_DATA')
        else:
            diag.label(text="No armature found", icon='ERROR')


class CG3H_OT_BonePresetNext(bpy.types.Operator):
    """Cycle to the next bone visibility preset"""
    bl_idname = "cg3h.bone_preset_next"
    bl_label = "Next Bone Preset"

    def execute(self, context):
        _cycle_bone_preset(context, +1)
        return {'FINISHED'}


class CG3H_OT_BonePresetPrev(bpy.types.Operator):
    """Cycle to the previous bone visibility preset"""
    bl_idname = "cg3h.bone_preset_prev"
    bl_label = "Previous Bone Preset"

    def execute(self, context):
        _cycle_bone_preset(context, -1)
        return {'FINISHED'}


class CG3H_OT_SetupSkinning(bpy.types.Operator):
    """Parent the active mesh to the character armature and add an Armature
    modifier so it can be weight-painted."""
    bl_idname = "cg3h.setup_skinning"
    bl_label = "Setup for Skinning"

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, "Select a mesh first")
            return {'CANCELLED'}

        arm = _find_armature_for(obj, context)
        if arm is None:
            self.report({'ERROR'}, "No CG3H armature found in the scene")
            return {'CANCELLED'}

        # Add an Armature modifier if missing
        has_mod = any(m.type == 'ARMATURE' and m.object == arm
                      for m in obj.modifiers)
        if not has_mod:
            mod = obj.modifiers.new(name="Armature", type='ARMATURE')
            mod.object = arm
            mod.use_vertex_groups = True

        # Parent without altering transform
        if obj.parent != arm:
            obj.parent = arm
            obj.matrix_parent_inverse = arm.matrix_world.inverted()

        self.report({'INFO'}, f"Linked {obj.name} to {arm.name}. "
                              f"Tab into Weight Paint mode to skin.")
        return {'FINISHED'}


# ── Registration ──────────────────────────────────────────────────────────────

def menu_func_import(self, context):
    self.layout.operator(CG3H_OT_Import.bl_idname, text="Hades II Model (.gpk)")


def menu_func_export(self, context):
    self.layout.operator(CG3H_OT_Export.bl_idname, text="Hades II Mod (CG3H)")


classes = [
    CG3HPreferences,
    CG3H_OT_Import,
    CG3H_OT_Export,
    CG3H_OT_ExportViolationsConfirm,
    CG3H_OT_InitMeshEntries,
    CG3H_OT_BonePresetNext,
    CG3H_OT_BonePresetPrev,
    CG3H_OT_SetupSkinning,
    CG3H_PT_MeshEntries,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)
    bpy.types.Scene.cg3h_entries = StringProperty(default="")
    bpy.types.Scene.cg3h_character = StringProperty(default="")
    bpy.types.Scene.cg3h_original_meshes = StringProperty(default="")
    bpy.types.Scene.cg3h_manifest_json = StringProperty(default="")
    bpy.types.Scene.cg3h_bone_preset = EnumProperty(
        name="Bone Preset",
        description="Hide/show bones by entry or template — pick a preset to "
                    "filter the armature down to relevant bones for skinning",
        items=_get_bone_preset_items,
        update=_apply_bone_preset,
        default=0,
    )


def unregister():
    # Restore any armature bone hide state we modified
    for arm in bpy.data.objects:
        if arm.type == 'ARMATURE' and "_cg3h_saved_hide" in arm:
            try:
                saved = json.loads(arm.get("_cg3h_saved_hide", "{}"))
                for bone in arm.data.bones:
                    bone.hide = saved.get(bone.name, False)
                del arm["_cg3h_saved_hide"]
            except Exception:
                pass  # restore is best-effort during addon unload

    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for prop in ("cg3h_entries", "cg3h_character", "cg3h_original_meshes",
                 "cg3h_manifest_json", "cg3h_bone_preset"):
        if hasattr(bpy.types.Scene, prop):
            delattr(bpy.types.Scene, prop)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
