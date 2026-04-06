"""
CG3H — Custom Geometry 3D for Hades II (Blender Addon)

Adds File > Import/Export menu entries for Hades II models:
  Import: .gpk file → Blender scene (meshes + armature)
  Export: Blender scene → patched .gpk file

Requires the CG3H tools directory to be configured in addon preferences.
The tools directory must contain gr2_to_gltf.py, gltf_to_gr2.py, and
granny_types.py. The game's granny2_x64.dll is auto-detected from the
game path set in preferences.
"""

bl_info = {
    "name": "CG3H — Hades II Model Tools",
    "author": "Enderclem",
    "version": (3, 0, 0),
    "blender": (4, 0, 0),
    "location": "File > Import/Export",
    "description": "Import and export Hades II 3D models (.gpk)",
    "category": "Import-Export",
}

import bpy
import math
import os
import subprocess
import sys
import tempfile
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper


# ── Default paths ─────────────────────────────────────────────────────────────

_STEAM_PATHS = [
    r"C:\Program Files (x86)\Steam\steamapps\common\Hades II",
    r"C:\Program Files\Steam\steamapps\common\Hades II",
    r"D:\Steam\steamapps\common\Hades II",
    r"D:\SteamLibrary\steamapps\common\Hades II",
    r"E:\SteamLibrary\steamapps\common\Hades II",
]


def _find_game_path():
    for p in _STEAM_PATHS:
        if os.path.isdir(p):
            return p
    return ""


def _find_tools_dir():
    # Check relative to this addon file (if installed alongside tools/)
    addon_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(addon_dir, "..", "..", "tools"),  # repo layout
        os.path.join(addon_dir, "tools"),
    ]
    for c in candidates:
        if os.path.isfile(os.path.join(c, "gr2_to_gltf.py")):
            return os.path.normpath(c)
    return ""


# ── Preferences ───────────────────────────────────────────────────────────────

class CG3HPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    game_path: StringProperty(
        name="Hades II Game Directory",
        description="Root of the Hades II installation (contains Ship/ and Content/)",
        subtype='DIR_PATH',
        default=_find_game_path(),
    )
    tools_path: StringProperty(
        name="CG3H Tools Directory",
        description="Directory containing gr2_to_gltf.py, gltf_to_gr2.py, etc.",
        subtype='DIR_PATH',
        default=_find_tools_dir(),
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "game_path")
        layout.prop(self, "tools_path")

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

        if not self.tools_path or not os.path.isfile(
                os.path.join(self.tools_path, "gr2_to_gltf.py")):
            issues.append("Tools directory invalid (gr2_to_gltf.py not found)")

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

def _get_characters(self, context):
    """Build enum items list from available .gpk files."""
    gpk_dir = _gpk_dir()
    if not os.path.isdir(gpk_dir):
        return [("NONE", "No models found", "Set game path in addon preferences")]
    names = sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(gpk_dir) if f.endswith(".gpk")
    )
    if not names:
        return [("NONE", "No models found", "")]
    return [(n, n, f"{n}.gpk") for n in names]


# ── Import Operator ───────────────────────────────────────────────────────────

class CG3H_OT_Import(bpy.types.Operator, ImportHelper):
    """Import a Hades II model (.gpk) into the scene"""
    bl_idname = "import_scene.cg3h_gpk"
    bl_label = "Import Hades II Model"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".gpk"
    filter_glob: StringProperty(default="*.gpk", options={'HIDDEN'}, maxlen=255)

    textures: BoolProperty(
        name="Import Textures",
        description="Extract and apply textures from .pkg archives",
        default=True,
    )
    animations: BoolProperty(
        name="Import Animations",
        description="Extract and import animation data",
        default=False,
    )
    anim_filter: StringProperty(
        name="Animation Filter",
        description="Only import animations matching this pattern (e.g. 'Idle*')",
        default="",
    )

    def execute(self, context):
        prefs = _prefs()
        tools = prefs.tools_path
        exporter = os.path.join(tools, "gr2_to_gltf.py")

        if not os.path.isfile(exporter):
            self.report({'ERROR'}, f"gr2_to_gltf.py not found at: {tools}")
            return {'CANCELLED'}

        gpk_path = self.filepath
        gpk_dir = os.path.dirname(gpk_path)
        name = os.path.splitext(os.path.basename(gpk_path))[0]
        dll = _dll_path()

        if not os.path.isfile(dll):
            self.report({'ERROR'}, f"granny2_x64.dll not found. Set game path in addon preferences.")
            return {'CANCELLED'}

        # Export to a temp .glb, then import into Blender
        tmp_glb = tempfile.mktemp(suffix=".glb")

        cmd = [
            sys.executable, exporter, name,
            "--gpk-dir", gpk_dir,
            "--dll", dll,
            "-o", tmp_glb,
        ]
        if self.textures:
            cmd.append("--textures")
        if self.animations:
            cmd.append("--animations")
        if self.anim_filter:
            cmd += ["--anim-filter", self.anim_filter]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=600,
                cwd=os.path.join(prefs.game_path, "Ship"),
            )
            if result.returncode != 0:
                self.report({'ERROR'}, f"Export failed:\n{result.stderr or result.stdout}")
                return {'CANCELLED'}
        except subprocess.TimeoutExpired:
            self.report({'ERROR'}, "Export timed out (>10min)")
            return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f"Export error: {e}")
            return {'CANCELLED'}

        if not os.path.isfile(tmp_glb):
            self.report({'ERROR'}, "Export produced no output file")
            return {'CANCELLED'}

        # Import the .glb into Blender
        bpy.ops.import_scene.gltf(filepath=tmp_glb)

        # Apply coordinate space rotation (Hades uses Y-up, Blender uses Z-up)
        # glTF import handles this automatically, but apply -90 X to match game view
        for obj in context.selected_objects:
            if obj.type == 'ARMATURE':
                obj.rotation_euler[0] = math.radians(-90)

        # Clean up temp file
        try:
            os.unlink(tmp_glb)
        except OSError:
            pass

        self.report({'INFO'}, f"Imported {name} ({len(context.selected_objects)} objects)")
        return {'FINISHED'}


# ── Export Operator ───────────────────────────────────────────────────────────

class CG3H_OT_Export(bpy.types.Operator, ExportHelper):
    """Export selected meshes back to a Hades II .gpk"""
    bl_idname = "export_scene.cg3h_gpk"
    bl_label = "Export Hades II Model"
    bl_options = {'REGISTER'}

    filename_ext = ".gpk"
    filter_glob: StringProperty(default="*.gpk", options={'HIDDEN'}, maxlen=255)

    character: EnumProperty(
        name="Original Character",
        description="Which character's GPK to patch (provides skeleton + topology)",
        items=_get_characters,
    )
    positional: BoolProperty(
        name="Positional Matching",
        description="Match meshes by index instead of name (use when variant names differ)",
        default=False,
    )
    save_gr2: BoolProperty(
        name="Also save .gr2",
        description="Write the raw .gr2 file alongside the .gpk",
        default=False,
    )
    allow_topology_change: BoolProperty(
        name="Allow Topology Change",
        description="Allow different vertex/face counts from the original mesh",
        default=True,
    )
    manifest: StringProperty(
        name="Manifest",
        description="Path to manifest.json for multi-file mod builds (auto-detected from export folder)",
        subtype='FILE_PATH',
        default="",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "character")
        layout.prop(self, "positional")
        layout.prop(self, "allow_topology_change")
        layout.prop(self, "save_gr2")
        layout.prop(self, "manifest")
        layout.separator()
        layout.label(text="Export from Blender with Normals OFF.", icon='INFO')
        layout.label(text="New meshes must be parented to the armature.", icon='INFO')

    def execute(self, context):
        prefs = _prefs()
        tools = prefs.tools_path
        importer = os.path.join(tools, "gltf_to_gr2.py")

        if not os.path.isfile(importer):
            self.report({'ERROR'}, f"gltf_to_gr2.py not found at: {tools}")
            return {'CANCELLED'}

        if self.character == "NONE":
            self.report({'ERROR'}, "No character selected. Set game path in addon preferences.")
            return {'CANCELLED'}

        dll = _dll_path()
        gpk_dir = _gpk_dir()
        gpk_path = os.path.join(gpk_dir, f"{self.character}.gpk")
        sdb_path = os.path.join(gpk_dir, f"{self.character}.sdb")

        for path, label in [(dll, "DLL"), (gpk_path, "GPK"), (sdb_path, "SDB")]:
            if not os.path.isfile(path):
                self.report({'ERROR'}, f"{label} not found: {path}")
                return {'CANCELLED'}

        # Export selected objects to a temp .glb (no normals to avoid vertex splitting)
        tmp_glb = tempfile.mktemp(suffix=".glb")

        bpy.ops.export_scene.gltf(
            filepath=tmp_glb,
            use_selection=True,
            export_format='GLB',
            export_normals=False,
            export_tangents=False,
            export_colors=False,
            export_yup=True,
        )

        if not os.path.isfile(tmp_glb):
            self.report({'ERROR'}, "glTF export produced no file. Select meshes + armature first.")
            return {'CANCELLED'}

        # Run gltf_to_gr2.py
        output_gpk = self.filepath
        cmd = [
            sys.executable, importer, tmp_glb,
            "--gpk", gpk_path,
            "--sdb", sdb_path,
            "--dll", dll,
            "--output-gpk", output_gpk,
            "--strict",
        ]
        if self.positional:
            cmd.append("--positional")
        if self.allow_topology_change:
            cmd.append("--allow-topology-change")

        # Auto-detect manifest.json in the export folder if not explicitly set
        manifest_path = self.manifest
        if not manifest_path:
            candidate = os.path.join(os.path.dirname(output_gpk), "manifest.json")
            if os.path.isfile(candidate):
                manifest_path = candidate
        if manifest_path and os.path.isfile(manifest_path):
            cmd += ["--manifest", manifest_path]

        if self.save_gr2:
            gr2_path = os.path.splitext(output_gpk)[0] + ".gr2"
            cmd += ["--output-gr2", gr2_path]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=600,
                cwd=os.path.join(prefs.game_path, "Ship"),
            )
            if result.returncode != 0:
                err = result.stderr or result.stdout
                # Extract the most useful line
                lines = [l for l in err.strip().split('\n') if l.strip()]
                msg = lines[-1] if lines else "Unknown error"
                self.report({'ERROR'}, f"Import failed: {msg}")
                return {'CANCELLED'}
        except subprocess.TimeoutExpired:
            self.report({'ERROR'}, "Import timed out (>10min)")
            return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f"Import error: {e}")
            return {'CANCELLED'}
        finally:
            try:
                os.unlink(tmp_glb)
            except OSError:
                pass

        self.report({'INFO'}, f"Exported to {output_gpk}")
        return {'FINISHED'}


# ── Build H2M Operator ───────────────────────────────────────────────────────

class CG3H_OT_BuildH2M(bpy.types.Operator):
    """Build a Hades II mod package (.h2m) for distribution"""
    bl_idname = "cg3h.build_h2m"
    bl_label = "Build H2M Package"
    bl_options = {'REGISTER'}

    mod_dir: StringProperty(
        name="Mod Directory",
        description="Root directory of the mod project (contains modified .gpk files)",
        subtype='DIR_PATH',
    )
    mod_name: StringProperty(
        name="Mod Name",
        description="Display name for the mod",
        default="My Hades II Mod",
    )
    author: StringProperty(
        name="Author",
        description="Author name for the mod metadata",
        default="",
    )
    thunderstore: BoolProperty(
        name="Thunderstore Format",
        description="Also generate a Thunderstore-compatible package",
        default=False,
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "mod_dir")
        layout.prop(self, "mod_name")
        layout.prop(self, "author")
        layout.prop(self, "thunderstore")

    def execute(self, context):
        prefs = _prefs()
        tools = prefs.tools_path
        build_script = os.path.join(tools, "cg3h_build.py")

        if not os.path.isfile(build_script):
            self.report({'ERROR'}, f"cg3h_build.py not found at: {tools}")
            return {'CANCELLED'}

        if not self.mod_dir or not os.path.isdir(self.mod_dir):
            self.report({'ERROR'}, "Mod directory is not set or does not exist")
            return {'CANCELLED'}

        cmd = [
            sys.executable, build_script,
            self.mod_dir,
        ]
        if self.thunderstore:
            cmd.append("--package")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                err = result.stderr or result.stdout
                lines = [l for l in err.strip().split('\n') if l.strip()]
                msg = lines[-1] if lines else "Unknown error"
                self.report({'ERROR'}, f"Build failed: {msg}")
                return {'CANCELLED'}
        except subprocess.TimeoutExpired:
            self.report({'ERROR'}, "Build timed out (>120s)")
            return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f"Build error: {e}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Built mod package in {self.mod_dir}")
        return {'FINISHED'}


# ── CG3H Menu ────────────────────────────────────────────────────────────────

class CG3H_MT_Menu(bpy.types.Menu):
    bl_idname = "CG3H_MT_menu"
    bl_label = "CG3H"

    def draw(self, context):
        layout = self.layout
        layout.operator(CG3H_OT_Import.bl_idname, text="Import Hades II Model")
        layout.operator(CG3H_OT_Export.bl_idname, text="Export Hades II Model")
        layout.separator()
        layout.operator(CG3H_OT_BuildH2M.bl_idname, text="Build H2M Package")


def menu_func_cg3h(self, context):
    self.layout.menu(CG3H_MT_Menu.bl_idname)


# ── Registration ──────────────────────────────────────────────────────────────

def menu_func_import(self, context):
    self.layout.operator(CG3H_OT_Import.bl_idname, text="Hades II Model (.gpk)")


def menu_func_export(self, context):
    self.layout.operator(CG3H_OT_Export.bl_idname, text="Hades II Model (.gpk)")


classes = [
    CG3HPreferences,
    CG3H_OT_Import,
    CG3H_OT_Export,
    CG3H_OT_BuildH2M,
    CG3H_MT_Menu,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)
    bpy.types.TOPBAR_MT_editor_menus.append(menu_func_cg3h)


def unregister():
    bpy.types.TOPBAR_MT_editor_menus.remove(menu_func_cg3h)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
