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
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "File > Import/Export",
    "description": "Import and export Hades II 3D models (.gpk)",
    "category": "Import-Export",
}

import bpy
import os
import subprocess
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

        bpy.ops.import_scene.gltf(filepath=tmp_glb)

        try:
            os.unlink(tmp_glb)
        except OSError:
            pass

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

        # Generate mod.json
        import json
        mod_json = {
            "format": "cg3h-mod/1.0",
            "metadata": {
                "name": mod_name,
                "author": author,
                "version": "1.0.0",
                "description": f"{mod_name} for {character}",
            },
            "type": "mesh_replace",
            "target": {
                "character": character,
                "mesh_entries": [f"{character}_Mesh"],
            },
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
                manifest_src = os.path.splitext(manifest_glb)[0] + "_manifest.json"
                if os.path.isfile(manifest_src):
                    import shutil
                    shutil.copy2(manifest_src, os.path.join(workspace, "manifest.json"))
                    os.unlink(manifest_src)
            except Exception:
                pass
            finally:
                if os.path.isfile(manifest_glb):
                    os.unlink(manifest_glb)

        # Build Thunderstore ZIP via cg3h_build
        builder_exe = _exe_path("cg3h_importer.exe")
        # cg3h_importer is gltf_to_gr2 — we need cg3h_build for packaging
        # Use the bundled exporter's Python to call cg3h_build if available,
        # otherwise just create the workspace and let the user build manually
        zip_path = None
        try:
            import sys as _sys
            addon = _addon_dir()
            if addon not in _sys.path:
                _sys.path.insert(0, addon)
            import cg3h_build
            game_dir = _prefs().game_path
            cg3h_build.build_mod(workspace, game_dir=game_dir)
            cg3h_build.package_thunderstore(workspace)
            mod_id = f"{author}-{mod_name}".replace(" ", "")
            zip_name = f"{mod_id}-1.0.0.zip"
            zip_path = os.path.join(workspace, zip_name)
        except Exception as build_err:
            pass

        if zip_path and os.path.isfile(zip_path):
            self.report({'INFO'}, f"Mod ready: {zip_path}")
        else:
            self.report({'INFO'}, f"Workspace created: {workspace} (build manually with CG3H GUI)")
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
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
