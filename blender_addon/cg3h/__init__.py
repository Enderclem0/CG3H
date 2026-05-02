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
    "version": (3, 9, 0),
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

import base64
import bpy
import importlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty
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
    # Fallback paths — keep in sync with tools/cg3h_constants.py::_FALLBACK_PATHS.
    # (The Blender addon lives in a separate Python environment and can't
    # import tools/, so we duplicate the list here.)
    for p in [
        r"C:\Program Files (x86)\Steam\steamapps\common\Hades II",
        r"C:\Program Files\Steam\steamapps\common\Hades II",
        r"D:\Steam\steamapps\common\Hades II",
        r"D:\SteamLibrary\steamapps\common\Hades II",
        r"E:\SteamLibrary\steamapps\common\Hades II",
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


def _new_actions(context):
    """Return Blender Actions whose names DON'T match any stock animation
    in the cached manifest — i.e. the actions that will be exported as
    `animation_add` entries.  Empty list if the manifest is missing
    (the modder hasn't imported the character yet) since we can't tell
    stock from new without it."""
    manifest = _get_manifest(context)
    if not manifest:
        return []
    stock = set(
        (manifest.get('animations') or {}).get('hashes', {}).keys()
    )
    if not stock:
        return []
    return [a for a in bpy.data.actions if a.name not in stock]


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

        # Capture the baseline positions file the exporter writes alongside
        # the manifest.  cg3h_build.py::_is_mesh_changed uses it for a
        # position-tolerance edit check; without it, every Blender-authored
        # mesh_replace looks "unchanged" and auto-detection collapses to
        # vertex-count comparison.  We base64 it onto a scene prop because
        # it's a binary .npz and Blender StringProperty is utf-8 only; the
        # export operator decodes + writes it next to the exported GLB.
        baseline_path = os.path.join(os.path.dirname(tmp_glb), ".baseline_positions.npz")
        if os.path.isfile(baseline_path):
            try:
                with open(baseline_path, "rb") as bf:
                    context.scene.cg3h_baseline_b64 = base64.b64encode(bf.read()).decode("ascii")
            except Exception as e:
                self.report({'WARNING'}, f"Baseline read failed: {e}")
            try:
                os.unlink(baseline_path)
            except OSError:
                pass

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

        # Seed each new (non-stock) action with a `cg3h_loop` custom
        # property so the dialog can render a checkbox bound to it.
        # Stored on the Action so it persists with the .blend.
        for action in _new_actions(context):
            if "cg3h_loop" not in action:
                action["cg3h_loop"] = False

        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "character")
        layout.prop(self, "mod_name")
        layout.prop(self, "author")
        layout.prop(self, "output_dir")

        # New animations: one row per non-stock Action with a loop
        # checkbox.  Skipped silently if the modder hasn't imported
        # the character yet (no manifest → can't tell stock from new).
        new_actions = _new_actions(context)
        if new_actions:
            box = layout.box()
            box.label(text="New animations (animation_add):")
            for action in new_actions:
                row = box.row()
                row.prop(action, '["cg3h_loop"]', text="Loop")
                row.label(text=action.name)

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

        # v3.11: auto-prefix new Blender actions with the author slug.
        # Two reasons:
        #   1. Cross-mod alias collision avoidance — every modder shipping
        #      "Dance" would clash in the engine's global Animation table.
        #   2. Visibility — the modder writes Lua code that calls
        #      SetAnimation({Name = "Author_Dance"}); seeing the prefixed
        #      name in their Blender outliner makes that obvious.
        # Apply only to actions whose names DON'T match a stock animation
        # entry (those are intentional animation_patch overrides) and
        # DON'T already start with the author slug.
        manifest = _get_manifest(context) or {}
        stock_anims = set(
            (manifest.get('animations') or {}).get('hashes', {}).keys()
        )
        author_slug = "".join(c for c in author if c.isalnum())
        if author_slug and stock_anims:
            renamed = []
            prefix = author_slug + "_"
            for action in bpy.data.actions:
                if action.name in stock_anims:
                    continue  # intentional stock override (animation_patch)
                if action.name.startswith(prefix):
                    continue  # already prefixed (re-export)
                new_name = prefix + action.name
                # Sanity: don't collide with another action's existing name
                if new_name in bpy.data.actions:
                    continue
                old_name = action.name
                action.name = new_name
                renamed.append((old_name, new_name))
            if renamed:
                msg = (f"Auto-prefixed {len(renamed)} action(s) with "
                       f"'{author_slug}_' to avoid cross-mod collisions:")
                self.report({'INFO'}, msg)
                for old, new in renamed[:5]:
                    print(f"[CG3H]   {old}  →  {new}")
                if len(renamed) > 5:
                    print(f"[CG3H]   ... and {len(renamed) - 5} more")
        elif not stock_anims:
            # No manifest cached → can't tell stock from custom; skip
            # the auto-rename rather than risk renaming a stock-matching
            # action.  The modder can re-import from CG3H to populate
            # the manifest.
            pass

        # v3.13: auto-generate sibling shadow + outline meshes per
        # accessory whose `cg3h_gen_shadow` / `cg3h_gen_outline` flags
        # are set.  Returns a list of duplicates to delete post-export.
        auto_gen_dupes = self._build_auto_gen_siblings(context)

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

        # Emit the baseline positions the importer captured onto the scene,
        # so cg3h_build.py's position-tolerance edit check has a reference
        # to compare against.  Without this, the build stage has no way to
        # tell a vertex-reshape apart from an untouched stock mesh (vertex
        # count is the only signal left), and the mesh_replace type-detect
        # collapses.
        baseline_b64 = context.scene.get("cg3h_baseline_b64", "")
        if baseline_b64:
            try:
                with open(os.path.join(workspace, ".baseline_positions.npz"), "wb") as bf:
                    bf.write(base64.b64decode(baseline_b64))
            except Exception as e:
                self.report({'WARNING'}, f"Baseline write failed: {e}")

        # Build mesh entry list and per-mesh routing from CG3H panel properties
        entries_str = context.scene.get("cg3h_entries", "")
        all_entries = entries_str.split(",") if entries_str else [f"{character}_Mesh"]
        all_entries = [e for e in all_entries if e]  # filter empties

        # Guard: `"".split(",")` returns `['']` (a 1-elem list with an
        # empty string) — NOT `[]`.  Without the filter, the empty-string
        # value poisons the `obj.name in original_meshes` check below for
        # any mesh whose name happens to be the empty string (none, in
        # practice), and more importantly hides the intent.
        original_meshes = set(
            n for n in context.scene.get("cg3h_original_meshes", "").split(",") if n
        )

        # Auto-detect mod type from the selected meshes.  A stock-named
        # mesh whose vertex count differs from the manifest is an edit
        # (mesh_replace); count-match is treated as untouched reference
        # geometry (the build stage re-checks against baseline and will
        # heal a stale mesh_replace tag either way).  Any non-stock mesh
        # is a new addition (mesh_add).
        manifest = _get_manifest(context) or {}
        manifest_vc = {m['name']: m.get('vertex_count')
                       for m in manifest.get('meshes', [])}

        has_replace = False
        has_add = False
        new_mesh_routing = {}
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            if obj.name in original_meshes:
                expected_vc = manifest_vc.get(obj.name)
                cur_vc = len(obj.data.vertices) if obj.data else 0
                if expected_vc is not None and cur_vc != expected_vc:
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

        # Auto-gen siblings have done their job — they're in the GLB
        # (use_selection picked them up) and in new_mesh_routing
        # (the loop above walked them).  Now pull them out of the
        # scene so the modder's working state matches what they had
        # pre-export.  Errors are non-fatal — the artefacts are
        # already written.
        for dupe in auto_gen_dupes:
            try:
                bpy.data.objects.remove(dupe, do_unlink=True)
            except Exception as e:
                print(f"[CG3H]   WARNING: could not remove auto-gen "
                      f"duplicate {dupe.name}: {e}")

        # Pre-declare each non-stock Action as an animation_add entry
        # carrying the modder's loop flag.  Builder's _sync_mod_json
        # fills in the rest (granny_name, clone_from, source_glb_action)
        # without overwriting fields we set here.  Action names here
        # already reflect the author-prefix renaming above.
        new_anim_entries = []
        for action in _new_actions(context):
            new_anim_entries.append({
                "logical_name": action.name,
                "loop": bool(action.get("cg3h_loop", False)),
            })
        if new_anim_entries:
            target["new_animations"] = new_anim_entries

        # Detect animation tracks in the exported GLB.  The gltf
        # exporter writes them into the JSON chunk; we just read the
        # header rather than depend on pygltflib (which Blender doesn't
        # bundle).  Empty file or parse failure → 0 (silently fall back
        # to mesh-only classification).
        n_animations = 0
        try:
            with open(glb_path, "rb") as gf:
                if gf.read(4) == b"glTF":
                    gf.read(8)  # version + total length
                    json_len = struct.unpack("<I", gf.read(4))[0]
                    gf.read(4)  # chunk type ('JSON')
                    glb_json = json.loads(gf.read(json_len).decode("utf-8"))
                    n_animations = len(glb_json.get("animations") or [])
        except Exception:
            pass  # GLB unreadable; treat as no animations

        # type field shape: single string when only one, list when
        # multiple.  v3.10: if no mesh edits but the GLB carries
        # animation tracks, the user authored an animation-only mod —
        # tag it animation_patch instead of falling back to the legacy
        # mesh_replace default.
        types_list = []
        if has_add:
            types_list.append("mesh_add")
        if has_replace:
            types_list.append("mesh_replace")
        if n_animations > 0 and not has_add and not has_replace:
            # Pure animation-only mod.
            types_list = ["animation_patch"]
        elif n_animations > 0 and (has_add or has_replace):
            # Mixed: include animation_patch alongside the mesh op so
            # cg3h_build's _sync_mod_json doesn't have to heal it later.
            types_list.append("animation_patch")
        if not types_list:
            types_list = ["mesh_replace"]  # legacy fallback
        mod_type = types_list if len(types_list) > 1 else types_list[0]

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
                    shutil.copy2(manifest_src, os.path.join(workspace, "manifest.json"))
                    os.unlink(manifest_src)
            except Exception as e:
                print(f"[CG3H] Manifest sync skipped: {e}")
            finally:
                if os.path.isfile(manifest_glb):
                    os.unlink(manifest_glb)

        # Build the unzipped mod package.  We deliberately omit
        # `--package` so cg3h_build leaves the result as a plain
        # folder under <workspace>/build/plugins[_data]/<mod_id>/...
        # — modders can inspect every produced file (the stripped
        # GLB, the .pkg, mod.json, manifest) before deciding to
        # publish.  When they're ready, they zip the build/
        # contents themselves for Thunderstore upload.
        build_dir = None
        build_succeeded = False
        build_script = os.path.join(_addon_dir(), "cg3h_build.py")
        if os.path.isfile(build_script):
            system_python = shutil.which("python") or shutil.which("python3") or shutil.which("py")
            if system_python:
                try:
                    result = subprocess.run(
                        [system_python, build_script, workspace],
                        capture_output=True, text=True, timeout=120,
                    )
                    if result.returncode == 0:
                        build_succeeded = True
                        build_dir = os.path.join(workspace, "build")
                    else:
                        print(f"CG3H build failed:\n{result.stderr or result.stdout}")
                except Exception as build_err:
                    print(f"CG3H build error: {build_err}")

        # v3.13: after a successful build, sweep the intermediate
        # source files (GLB, mod.json, manifest, baseline, icon) —
        # they already shaped the build/ output and will be
        # regenerated on the next export from the .blend.  Keep
        # build/ itself so the modder can inspect what shipped.
        if build_succeeded:
            sweep_files = [
                glb_path,                                       # source GLB
                os.path.join(workspace, "mod.json"),
                os.path.join(workspace, "manifest.json"),
                os.path.join(workspace, ".baseline_positions.npz"),
                os.path.join(workspace, "icon.png"),
            ]
            for path in sweep_files:
                try:
                    if os.path.isfile(path):
                        os.unlink(path)
                except Exception as e:
                    print(f"[CG3H] Could not remove {path}: {e}")

        if build_dir and os.path.isdir(build_dir):
            self.report({'INFO'}, f"Mod ready: {build_dir}")
        else:
            self.report({'INFO'}, f"Workspace created: {workspace} (build manually with CG3H GUI)")
        return {'FINISHED'}

    def _build_auto_gen_siblings(self, context):
        """v3.13: duplicate selected meshes with `cg3h_gen_shadow` /
        `cg3h_gen_outline` set, apply the appropriate modifier (Decimate
        for shadow, Displace along normal for outline), rename with the
        engine-recognised suffix (`<orig>ShadowMesh_MeshShape` /
        `<orig>Outline_MeshShape`), and add to the active selection so
        the GLB export picks them up.

        Returns the list of created duplicates so the caller can delete
        them post-export and leave the modder's scene clean.

        The Armature modifier on the original mesh transfers to the
        duplicate (Blender duplicates the modifier stack).  We apply
        only Decimate / Displace by name — Armature stays unapplied so
        runtime skinning still works.
        """
        dupes = []
        # Snapshot selection — _build runs late in execute() so ctx
        # is the same set the export will use.
        candidates = [obj for obj in context.selected_objects
                      if obj.type == 'MESH']
        for src in candidates:
            gen_shadow = bool(getattr(src, "cg3h_gen_shadow", False))
            gen_outline = bool(getattr(src, "cg3h_gen_outline", False))
            if not (gen_shadow or gen_outline):
                continue
            base_name = src.name.replace("_MeshShape", "")

            if gen_shadow:
                ratio = float(getattr(src, "cg3h_shadow_ratio", 0.30))
                dupe = self._duplicate_with_modifier(
                    context, src,
                    new_name=f"{base_name}ShadowMesh_MeshShape",
                    modifier_type='DECIMATE',
                    modifier_attrs={'ratio': max(0.05, min(1.0, ratio))})
                if dupe is not None:
                    dupes.append(dupe)
                    print(f"[CG3H]   auto-gen shadow: "
                          f"{src.name} → {dupe.name} (decimate {ratio:.2f})")

            if gen_outline:
                push = float(getattr(src, "cg3h_outline_push", 0.01))
                push = max(0.001, min(0.5, push))
                dupe = self._build_outline_dupe(
                    context, src,
                    new_name=f"{base_name}Outline_MeshShape",
                    push=push)
                if dupe is not None:
                    dupes.append(dupe)
                    print(f"[CG3H]   auto-gen outline: "
                          f"{src.name} → {dupe.name} "
                          f"(push {push*100:.2f}%)")
        return dupes

    @staticmethod
    def _build_outline_dupe(context, src, new_name, push):
        """Duplicate `src` and push every vertex outward along a
        face-derived unit normal by `push` × the mesh's mesh-data
        bbox diagonal.

        Bypasses Blender's Displace modifier on purpose.  Displace
        with direction='NORMAL' uses whatever vertex normals are
        stored on the mesh, and CG3H imports normals from the GR2
        verbatim (the engine encodes direction, not unit length, so
        magnitudes can be ≠1).  bmesh's `f.normal` also turned out
        to NOT be unit length on imported meshes — it's a hint
        attribute that `bm.normal_update()` doesn't reliably
        override.  Computing face normals from raw vertex positions
        with numpy is the only reliable path.
        """
        import numpy as np

        # Use the same scaffolding as _duplicate_with_modifier for the
        # selection / active dance.
        prev_active = context.view_layer.objects.active
        prev_selected = list(context.selected_objects)
        try:
            bpy.ops.object.select_all(action='DESELECT')
            src.select_set(True)
            context.view_layer.objects.active = src
            bpy.ops.object.duplicate(linked=False)
            dupe = context.view_layer.objects.active
        finally:
            bpy.ops.object.select_all(action='DESELECT')
            for o in prev_selected:
                try:
                    o.select_set(True)
                except Exception:
                    pass
            context.view_layer.objects.active = prev_active

        if dupe is src or dupe is None:
            return None
        if new_name in bpy.data.objects:
            print(f"[CG3H]   skip auto-gen: {new_name!r} already exists")
            try:
                bpy.data.objects.remove(dupe, do_unlink=True)
            except Exception:
                pass
            return None
        dupe.name = new_name
        if dupe.data:
            dupe.data.name = new_name

        # Split topology at sharp edges before computing normals so
        # the outline doesn't poke through the source at creases.
        # The classic inverted-hull failure mode: at the brim-of-a-hat
        # crease, the smooth-averaged vertex normal bisects the two
        # face normals — pushing the outline vertex outward along
        # that bisector lands it INSIDE the source mesh on one side
        # of the crease (visible as a black sliver where the outline
        # peeks through the source).  Edge-splitting separates the
        # vertex into one copy per face at sharp edges, so each copy
        # gets that face's own normal and the displacement is
        # crisp — same trick Blender's Edge Split modifier uses for
        # inverted-hull outlines.
        import bmesh
        import math
        SHARP_ANGLE = math.radians(30.0)
        bm = bmesh.new()
        bm.from_mesh(dupe.data)
        sharp = [e for e in bm.edges
                 if len(e.link_faces) == 2
                 and e.calc_face_angle() > SHARP_ANGLE]
        if sharp:
            bmesh.ops.split_edges(bm, edges=sharp)
        bm.to_mesh(dupe.data)
        bm.free()

        # Do everything in OBJECT space, then convert back.  Why:
        # CG3H imports often parent the mesh under an armature with
        # a non-uniform bind scale — Moros's hat ships with
        # matrix_world ≈ scale(10, 100, 10), so a uniform mesh-data
        # displacement of `s` becomes visible (10s, 100s, 10s) and
        # the outline ends up grossly stretched in one axis.  Working
        # in object space keeps the visible displacement isotropic.
        mesh = dupe.data
        n_verts = len(mesh.vertices)
        local = np.empty((n_verts, 3), dtype=np.float64)
        mesh.vertices.foreach_get("co", local.ravel())

        mw = np.array(dupe.matrix_world, dtype=np.float64)        # 4×4
        mw_inv = np.array(dupe.matrix_world.inverted(), dtype=np.float64)
        local_h = np.column_stack([local, np.ones(n_verts)])      # → homogeneous
        obj = (mw @ local_h.T).T[:, :3]                           # object-space verts

        obj_diag = float(np.linalg.norm(obj.max(axis=0) - obj.min(axis=0)))
        strength = obj_diag * push  # visible displacement target

        # Face normals from object-space positions (cross-product +
        # divide by length → unit length by construction).
        v_normals = np.zeros_like(obj)
        for poly in mesh.polygons:
            idx = list(poly.vertices)
            if len(idx) < 3:
                continue
            v0 = obj[idx[0]]
            for i in range(1, len(idx) - 1):
                v1 = obj[idx[i]]
                v2 = obj[idx[i + 1]]
                fn = np.cross(v1 - v0, v2 - v0)
                fl = np.linalg.norm(fn)
                if fl < 1e-12:
                    continue
                fn = fn / fl
                v_normals[idx[0]]   += fn
                v_normals[idx[i]]   += fn
                v_normals[idx[i+1]] += fn
        lens = np.linalg.norm(v_normals, axis=1, keepdims=True)
        unit = np.where(lens > 1e-9, v_normals / np.maximum(lens, 1e-9), 0.0)

        # Outward-orient the normals using the mesh centroid as a
        # proxy for "inside".  CG3H GR2 imports can land in either
        # winding convention — Moros's hat comes in CW from the
        # outside, so the raw cross-product points INWARD and the
        # outline ended up smaller than the source instead of
        # bigger.  Flip any normal whose dot with (vertex-centroid)
        # is negative.  For convex-ish accessories (hats, glasses,
        # capes) this is reliable; deeply concave shapes might get
        # a few verts wrong at the concavities, but those are edge
        # cases for the auto-gen outline use.
        centroid = obj.mean(axis=0)
        outward = obj - centroid
        flip_mask = np.sum(unit * outward, axis=1) < 0
        unit[flip_mask] *= -1

        new_obj = obj + unit * strength

        # Convert displaced positions back to mesh-data space and
        # write them in.  The full matrix-inverse handles the
        # anisotropic scale correctly: visible displacement stays
        # uniform regardless of the bind pose stretch.
        new_obj_h = np.column_stack([new_obj, np.ones(n_verts)])
        new_local = (mw_inv @ new_obj_h.T).T[:, :3]
        mesh.vertices.foreach_set("co", new_local.astype(np.float64).ravel())
        mesh.update()

        max_disp_obj = float(np.linalg.norm(new_obj - obj, axis=1).max())
        print(f"[CG3H]     outline applied: obj_diag={obj_diag:.3f} "
              f"strength={strength:.4f} max_disp_obj={max_disp_obj:.4f}")

        # Match the post-conditions of _duplicate_with_modifier so the
        # rest of the export flow (cleanup, routing) treats this dupe
        # the same way.
        try:
            dupe.cg3h_gen_shadow = False
            dupe.cg3h_gen_outline = False
        except Exception:
            pass
        try:
            dupe.select_set(True)
        except Exception:
            pass
        return dupe

    @staticmethod
    def _bbox_diagonal(obj):
        """Local bounding-box diagonal length (object scale included),
        used to scale the outline Displace strength so accessories of
        any size get a proportional outline."""
        import math
        coords = [(c[0], c[1], c[2]) for c in obj.bound_box]
        mn = [min(c[i] for c in coords) for i in range(3)]
        mx = [max(c[i] for c in coords) for i in range(3)]
        sx, sy, sz = obj.scale
        diag = math.sqrt(((mx[0] - mn[0]) * sx) ** 2
                         + ((mx[1] - mn[1]) * sy) ** 2
                         + ((mx[2] - mn[2]) * sz) ** 2)
        return diag if diag > 0 else 1.0

    @staticmethod
    def _duplicate_with_modifier(context, src, new_name,
                                 modifier_type, modifier_attrs):
        """Make a duplicate of `src`, add a modifier with the given
        type + attrs, apply just that modifier (leaving any other
        modifiers like Armature untouched), rename, and add to the
        active selection.  Returns the new Object or None on failure.
        """
        # Avoid creating duplicates with already-existing siblings —
        # the modder may have authored the outline / shadow mesh by
        # hand and we'd shadow it.
        if new_name in bpy.data.objects:
            print(f"[CG3H]   skip auto-gen: {new_name!r} already exists")
            return None

        # Duplicate.  bpy.ops.object.duplicate operates on selection
        # in the active view layer — temporarily isolate `src`.
        prev_active = context.view_layer.objects.active
        prev_selected = list(context.selected_objects)
        try:
            bpy.ops.object.select_all(action='DESELECT')
            src.select_set(True)
            context.view_layer.objects.active = src
            bpy.ops.object.duplicate(linked=False)
            dupe = context.view_layer.objects.active
        finally:
            # Restore prior selection so the rest of execute() sees
            # the original picks; we'll re-add the dupe to selection
            # below before returning.
            bpy.ops.object.select_all(action='DESELECT')
            for o in prev_selected:
                try:
                    o.select_set(True)
                except Exception:
                    pass
            context.view_layer.objects.active = prev_active

        if dupe is src or dupe is None:
            return None
        dupe.name = new_name
        if dupe.data:
            dupe.data.name = new_name

        mod = dupe.modifiers.new(name=f"cg3h_{modifier_type.lower()}",
                                 type=modifier_type)
        for k, v in modifier_attrs.items():
            try:
                setattr(mod, k, v)
            except Exception as e:
                print(f"[CG3H]   WARNING: setting {k}={v} on {modifier_type} "
                      f"failed: {e}")

        # Apply just this modifier.  Apply requires the dupe to be
        # the active object.
        prev_active2 = context.view_layer.objects.active
        try:
            context.view_layer.objects.active = dupe
            bpy.ops.object.modifier_apply(modifier=mod.name)
        except Exception as e:
            print(f"[CG3H]   WARNING: apply {modifier_type} on "
                  f"{dupe.name} raised: {e}")
            try:
                bpy.data.objects.remove(dupe, do_unlink=True)
            except Exception:
                pass
            context.view_layer.objects.active = prev_active2
            return None
        context.view_layer.objects.active = prev_active2

        # Reset the auto-gen flags on the duplicate so it doesn't try
        # to re-generate from itself on a re-export (defensive — the
        # dupe gets deleted post-export anyway, but if something keeps
        # it around we don't want recursive auto-gen).
        try:
            dupe.cg3h_gen_shadow = False
            dupe.cg3h_gen_outline = False
        except Exception:
            pass

        # Add to selection so the GLB export picks it up alongside
        # the other selected meshes.
        try:
            dupe.select_set(True)
        except Exception:
            pass
        return dupe

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
        is_multi_entry = len(entries) > 1
        if is_multi_entry:
            layout.label(text=f"Character: {character} ({len(entries)} entries)")
        else:
            layout.label(text=f"Character: {character} (single entry: "
                              f"{entries[0] if entries else '(none)'})")
        layout.separator()

        obj = context.active_object
        if not obj or obj.type != 'MESH':
            layout.label(text="Select a mesh to see options")
            return

        # Original (imported) meshes are routed by manifest — no
        # per-mesh entry pick, no auto-gen (stock outline/shadow
        # already exist).  Surface bone-preset and skinning helpers
        # only.
        original_meshes = [n for n in context.scene.get("cg3h_original_meshes", "").split(",") if n]
        is_original = obj.name in original_meshes
        layout.label(text=f"{obj.name}", icon='MESH_DATA')
        if is_original:
            layout.label(text="Original mesh — routed via manifest", icon='INFO')

        # Multi-entry routing checkboxes only shown for new meshes
        # AND multi-entry characters (single-entry chars route
        # everything to the one entry; nothing to pick).
        if not is_original and is_multi_entry:
            first_prop = f"cg3h_entry_{entries[0]}"
            if first_prop not in obj:
                layout.operator("cg3h.init_mesh_entries",
                                text="Enable Entry Selection",
                                icon='CHECKMARK')
                return
            for entry in entries:
                prop_name = f"cg3h_entry_{entry}"
                if prop_name in obj:
                    layout.prop(obj, f'["{prop_name}"]', text=entry)

        # v3.13 — auto-gen sibling outline + shadow meshes at export
        # time.  Engine renders a mesh in the outline / shadow pass
        # only when its name has the right suffix and mesh_type byte
        # is set (engine fills mesh_type from the name at GR2 load).
        # A mesh_add accessory ships as one mesh and so falls out of
        # both passes — the auto-gen closes that gap by duplicating
        # the modder's mesh, applying a modifier (Decimate for shadow,
        # Displace for outline), renaming with the right suffix, then
        # exporting alongside.  Duplicates are deleted post-export.
        # Skipped for original meshes — they already have stock
        # outline / shadow siblings in the GR2.
        if is_original:
            return
        layout.separator()
        gen_box = layout.box()
        gen_box.label(text="Auto-gen shadow + outline", icon='MOD_SOLIDIFY')
        # Shadow row — proper RNA props render as native checkbox + slider.
        row = gen_box.row(align=True)
        row.prop(obj, "cg3h_gen_shadow", text="Shadow")
        sub = row.row()
        sub.enabled = obj.cg3h_gen_shadow
        sub.prop(obj, "cg3h_shadow_ratio", text="ratio")
        # Outline row.
        row = gen_box.row(align=True)
        row.prop(obj, "cg3h_gen_outline", text="Outline")
        sub = row.row()
        sub.enabled = obj.cg3h_gen_outline
        sub.prop(obj, "cg3h_outline_push", text="push")

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
    bpy.types.Scene.cg3h_baseline_b64 = StringProperty(default="")
    bpy.types.Scene.cg3h_bone_preset = EnumProperty(
        name="Bone Preset",
        description="Hide/show bones by entry or template — pick a preset to "
                    "filter the armature down to relevant bones for skinning",
        items=_get_bone_preset_items,
        update=_apply_bone_preset,
        default=0,
    )
    # v3.13: per-mesh auto-gen flags + parameters.  Registering these as
    # proper RNA properties (instead of plain custom dict entries) so
    # Blender renders them with native bool/float widgets — bare custom
    # properties would render as a non-interactive number field.
    bpy.types.Object.cg3h_gen_shadow = BoolProperty(
        name="Generate shadow",
        description="At export time, duplicate this mesh, decimate it, "
                    "rename to <Mesh>ShadowMesh_MeshShape, and ship it "
                    "alongside so the engine renders it in the shadow pass",
        default=False,
    )
    bpy.types.Object.cg3h_shadow_ratio = FloatProperty(
        name="Decimate ratio",
        description="Target vertex ratio for the auto-generated shadow "
                    "mesh (stock shadows are roughly 0.25–0.40 of the "
                    "main mesh's poly count)",
        default=0.30, min=0.05, max=1.0, step=5, precision=2,
    )
    bpy.types.Object.cg3h_gen_outline = BoolProperty(
        name="Generate outline",
        description="At export time, duplicate this mesh, push vertices "
                    "outward along their normals, rename to "
                    "<Mesh>Outline_MeshShape, and ship alongside so the "
                    "engine renders it in the outline pass",
        default=False,
    )
    bpy.types.Object.cg3h_outline_push = FloatProperty(
        name="Push (% of bbox)",
        description="Outward displacement of the outline mesh, as a "
                    "fraction of the source mesh's bounding-box diagonal "
                    "(stock outlines push ~0.7%)",
        default=0.01, min=0.001, max=0.5, step=1, precision=4,
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
                 "cg3h_manifest_json", "cg3h_baseline_b64", "cg3h_bone_preset"):
        if hasattr(bpy.types.Scene, prop):
            delattr(bpy.types.Scene, prop)
    for prop in ("cg3h_gen_shadow", "cg3h_shadow_ratio",
                 "cg3h_gen_outline", "cg3h_outline_push"):
        if hasattr(bpy.types.Object, prop):
            delattr(bpy.types.Object, prop)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
