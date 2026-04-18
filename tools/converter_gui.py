"""
CG3H Mod Builder — GUI

Three tabs:
  Create — pick a character, configure export options, create a mod workspace
  Build  — point to a mod workspace, build for H2M, install to r2modman
  Mods   — manage installed CG3H mods, merge order, rebuild

Requires: numpy, pygltflib, lz4  (pip install numpy pygltflib lz4)
"""

import contextlib
import glob
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import cg3h_build
except ImportError:
    cg3h_build = None

try:
    import pygltflib
except ImportError:
    pygltflib = None


# -- Constants ----------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EXPORTER   = os.path.join(SCRIPT_DIR, "gr2_to_gltf.py")
IMPORTER   = os.path.join(SCRIPT_DIR, "gltf_to_gr2.py")

DEFAULT_OUTPUT = os.path.join(os.path.expanduser("~"), "Documents", "CG3H_Mods")

from cg3h_constants import STEAM_PATHS, CG3H_BUILDER_DEPENDENCY, find_game_path




# -- App ----------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("CG3H Mod Builder")
        self.root.minsize(900, 650)

        self._config_path = os.path.join(SCRIPT_DIR, ".cg3h_config.json")
        self._config = self._load_config()

        self.game_path = tk.StringVar(value=self._config.get("game_path", find_game_path()))
        self._all_names: list[str] = []
        self._status = tk.StringVar(value="Ready")

        self._create_running = False
        self._build_running = False

        self._build_ui()
        self.game_path.trace_add("write", lambda *_: self._scan())
        self._scan()

    # -- Paths ----------------------------------------------------------------

    def _gpk_dir(self):
        return os.path.join(self.game_path.get(), "Content", "GR2", "_Optimized")

    def _dll_path(self):
        return os.path.join(self.game_path.get(), "Ship", "granny2_x64.dll")

    def _find_r2modman_dir(self):
        """Find the r2modman ReturnOfModding directory."""
        candidates = [
            os.path.expandvars(
                r"%APPDATA%\r2modmanPlus-local\HadesII\profiles\Default\ReturnOfModding"),
        ]
        for c in candidates:
            if os.path.isdir(c):
                return c
        return None

    def _populate_r2modman_cache(self, r2_dir, mod_id, version, mod_dir, build_dir):
        """Mirror the deployed plugin + icon + manifest into r2modman's
        cache so the profile UI renders the tile with an icon and
        display name.  r2modman derives cache root from the profile
        layout — one level up from the ReturnOfModding dir."""
        profiles_idx = r2_dir.lower().find(os.sep + "profiles" + os.sep)
        if profiles_idx < 0:
            return  # unfamiliar layout — skip silently
        r2_root = r2_dir[:profiles_idx]
        cache_dir = os.path.join(r2_root, "cache", mod_id, version)
        if os.path.isdir(cache_dir):
            shutil.rmtree(cache_dir)
        os.makedirs(cache_dir, exist_ok=True)
        # Don't pre-create plugins/<mod_id>/ or plugins_data/<mod_id>/ —
        # shutil.copytree below will create them, and a pre-existing
        # destination makes it error on Windows.

        # Top-level manifest the r2modman UI reads for title/author/etc.
        # Prefer the one the build pipeline already generated.
        manifest_src = os.path.join(build_dir, "manifest.json")
        if not os.path.isfile(manifest_src):
            manifest_src = os.path.join(mod_dir, "manifest.json")
        if os.path.isfile(manifest_src):
            shutil.copy2(manifest_src, os.path.join(cache_dir, "manifest.json"))

        # Icon: build/ > workspace > default CG3H icon (repo root).
        for candidate in (
            os.path.join(build_dir, "icon.png"),
            os.path.join(mod_dir, "icon.png"),
            os.path.join(SCRIPT_DIR, "..", "icon.png"),
        ):
            if os.path.isfile(candidate):
                shutil.copy2(candidate, os.path.join(cache_dir, "icon.png"))
                break

        # Mirror the deployed plugin + plugins_data under the cache so
        # r2modman can reinstall from cache on profile rebuild.
        for subdir in ("plugins", "plugins_data"):
            src = os.path.join(r2_dir, subdir, mod_id)
            if os.path.isdir(src):
                dst = os.path.join(cache_dir, subdir, mod_id)
                shutil.copytree(src, dst)

    # -- Config persistence ----------------------------------------------------

    def _load_config(self):
        try:
            if os.path.isfile(self._config_path):
                with open(self._config_path) as f:
                    return json.load(f)
        except Exception:
            pass  # corrupt/unreadable config falls back to defaults
        return {}

    def _save_config(self):
        try:
            with open(self._config_path, 'w') as f:
                json.dump(self._config, f, indent=2)
        except Exception:
            pass  # config save is best-effort

    # -- Top-level UI ---------------------------------------------------------

    def _build_ui(self):
        # Game path bar
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(fill=tk.X)
        ttk.Label(bar, text="Game directory:").pack(side=tk.LEFT)
        ttk.Entry(bar, textvariable=self.game_path, width=64).pack(
            side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        ttk.Button(bar, text="Browse\u2026", command=self._browse_game).pack(side=tk.LEFT)
        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(fill=tk.X)

        # Notebook
        self._nb = ttk.Notebook(self.root)
        self._nb.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        self._build_create_tab()
        self._build_build_tab()
        self._build_mods_tab()

        self._nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # Status bar
        ttk.Label(
            self.root, textvariable=self._status,
            relief=tk.SUNKEN, anchor=tk.W, padding=(6, 2),
        ).pack(fill=tk.X, side=tk.BOTTOM)

    # =========================================================================
    # Tab 1: Create
    # =========================================================================

    def _build_create_tab(self):
        tab = ttk.Frame(self._nb, padding=8)
        self._nb.add(tab, text="  Create  ")

        top = ttk.LabelFrame(tab, text="Create New Mod Workspace", padding=12)
        top.pack(fill=tk.X)

        # Character selection — search box + listbox
        row = ttk.Frame(top)
        row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(row, text="Character:", width=14, anchor=tk.W).pack(side=tk.LEFT)
        self._create_char = tk.StringVar()
        self._create_char_entry = ttk.Entry(row, textvariable=self._create_char, width=36)
        self._create_char_entry.pack(side=tk.LEFT, padx=6)
        self._create_char_entry.bind('<KeyRelease>', self._filter_characters)

        char_list_frame = ttk.Frame(top)
        char_list_frame.pack(fill=tk.X, pady=(0, 4), padx=(112, 0))
        char_sb = ttk.Scrollbar(char_list_frame, orient=tk.VERTICAL)
        self._create_char_lb = tk.Listbox(
            char_list_frame, height=6, yscrollcommand=char_sb.set,
            exportselection=False, font=("Consolas", 9))
        char_sb.config(command=self._create_char_lb.yview)
        self._create_char_lb.pack(side=tk.LEFT, fill=tk.X, expand=True)
        char_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._create_char_lb.bind('<<ListboxSelect>>', self._on_char_select)

        # Options
        opt_frame = ttk.LabelFrame(top, text="Options", padding=8)
        opt_frame.pack(fill=tk.X, pady=(4, 4))

        self._create_textures = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_frame, text="Include textures (embeds PNG in GLB + saves DDS)",
                        variable=self._create_textures).pack(anchor=tk.W, pady=2)

        self._create_animations = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_frame, text="Include animations (slow -- can take several minutes)",
                        variable=self._create_animations).pack(anchor=tk.W, pady=2)

        anim_row = ttk.Frame(opt_frame)
        anim_row.pack(fill=tk.X, pady=(0, 4), padx=(20, 0))
        ttk.Label(anim_row, text="Animation filter:", foreground="#555").pack(side=tk.LEFT)
        self._create_anim_filter = tk.StringVar()
        ttk.Entry(anim_row, textvariable=self._create_anim_filter, width=24).pack(
            side=tk.LEFT, padx=4)
        ttk.Label(anim_row, text="e.g. Idle, Attack  (blank = all)",
                  foreground="#888", font=("", 8)).pack(side=tk.LEFT)

        # Mesh entry checkboxes (populated when character is selected)
        self._create_entries_frame = ttk.LabelFrame(opt_frame, text="Mesh entries", padding=4)
        self._create_entries_frame.pack(fill=tk.X, pady=(0, 2))
        self._create_entry_vars = {}  # {entry_name: BooleanVar}
        ttk.Label(self._create_entries_frame, text="Select a character to see entries",
                  foreground="#888").pack(anchor=tk.W)

        # Output directory
        out_row = ttk.Frame(top)
        out_row.pack(fill=tk.X, pady=(4, 4))
        ttk.Label(out_row, text="Output:", width=14, anchor=tk.W).pack(side=tk.LEFT)
        self._create_output = tk.StringVar(value=DEFAULT_OUTPUT)
        ttk.Entry(out_row, textvariable=self._create_output, width=40).pack(
            side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        ttk.Button(out_row, text="Browse\u2026",
                   command=lambda: self._browse_dir(self._create_output)).pack(side=tk.LEFT)

        # Create button + progress
        btn_row = ttk.Frame(top)
        btn_row.pack(fill=tk.X, pady=(8, 4))
        self._create_btn = ttk.Button(
            btn_row, text="Create Mod Workspace", command=self._create_workspace)
        self._create_btn.pack(side=tk.LEFT)
        self._create_status_label = ttk.Label(btn_row, text="", foreground="#555")
        self._create_status_label.pack(side=tk.LEFT, padx=12)

        self._create_progress = ttk.Progressbar(top, mode="indeterminate")
        self._create_progress.pack(fill=tk.X)

        # Log
        self._create_log = self._make_log(tab)

    def _create_workspace(self):
        character = self._create_char.get().strip()
        if not character:
            messagebox.showwarning("No character", "Select a character from the dropdown.")
            return

        out_base = self._create_output.get().strip() or DEFAULT_OUTPUT
        # Workspace folder: output/Character/
        workspace = os.path.join(out_base, character)

        dll = self._dll_path()
        if not os.path.isfile(dll):
            messagebox.showerror("DLL not found",
                                 f"granny2_x64.dll not found at:\n{dll}\n\nCheck game directory.")
            return

        os.makedirs(workspace, exist_ok=True)
        self._log_clear(self._create_log)
        self._create_running = True
        self._create_btn.config(state=tk.DISABLED)
        self._create_status_label.config(text="Exporting...", foreground="#555")
        self._create_progress.start(12)

        threading.Thread(
            target=self._create_worker,
            args=(character, workspace, dll),
            daemon=True,
        ).start()

    def _create_worker(self, character, workspace, dll):
        gpk_dir = self._gpk_dir()

        # Step 1: Export character to GLB
        glb_path = os.path.join(workspace, f"{character}.glb")
        cmd = [
            sys.executable, EXPORTER, character,
            "--gpk-dir", gpk_dir,
            "--dll", dll,
            "-o", glb_path,
        ]
        if self._create_textures.get():
            cmd.append("--textures")
        if self._create_animations.get():
            cmd.append("--animations")
            anim_filter = self._create_anim_filter.get().strip()
            if anim_filter:
                cmd += ["--anim-filter", anim_filter]
        # Pass selected mesh entries (if checkboxes exist and not all checked)
        selected_entries = [e for e, v in self._create_entry_vars.items() if v.get()]
        all_entries = list(self._create_entry_vars.keys())
        if selected_entries and selected_entries != all_entries:
            cmd += ["--mesh-entry", ",".join(selected_entries)]

        self._log_write_ui(self._create_log,
                           f"Exporting {character} to {workspace}...\n\n")

        ok = False
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
            )
            for line in proc.stdout:
                self._log_write_ui(self._create_log, line)
            proc.wait()
            ok = proc.returncode == 0
            if not ok:
                self._log_write_ui(self._create_log,
                                   f"\nExport exited with code {proc.returncode}\n")
        except Exception as exc:
            self._log_write_ui(self._create_log, f"\nERROR: {exc}\n")

        if not ok:
            self._ui(lambda: self._create_finish(False, workspace))
            return

        # Step 2: Generate mod.json
        try:
            if selected_entries:
                mesh_entries = selected_entries
            else:
                mesh_entries = [f"{character}_Mesh"]

            saved_author = self._config.get("author", "Modder")
            mod_json = {
                "format": "cg3h-mod/1.0",
                "metadata": {
                    "name": character,
                    "author": saved_author,
                    "version": "1.0.0",
                    "description": f"Mod for {character}",
                },
                "type": "mesh_replace",
                "target": {
                    "character": character,
                    "mesh_entries": mesh_entries,
                },
                "assets": {
                    "glb": f"{character}.glb",
                },
            }
            mod_json_path = os.path.join(workspace, "mod.json")
            with open(mod_json_path, "w") as f:
                json.dump(mod_json, f, indent=2)
            self._log_write_ui(self._create_log,
                               "\nGenerated mod.json\n")
        except Exception as exc:
            self._log_write_ui(self._create_log, f"\nmod.json error: {exc}\n")

        # Copy default CG3H icon to workspace
        icon_src = os.path.join(SCRIPT_DIR, "..", "icon.png")
        if os.path.isfile(icon_src):
            shutil.copy2(icon_src, os.path.join(workspace, "icon.png"))

        self._ui(lambda: self._create_finish(True, workspace))

    def _create_finish(self, success, workspace):
        self._create_running = False
        self._create_btn.config(state=tk.NORMAL)
        self._create_progress.stop()

        if success:
            self._create_status_label.config(text="Workspace created!", foreground="#070")
            self._status.set(f"Workspace ready: {workspace}")
            self._log_write_ui(self._create_log,
                               f"\nDone! Open the .glb in Blender, edit, then use the Build tab.\n"
                               f"Workspace: {workspace}\n")
        else:
            self._create_status_label.config(text="Export failed", foreground="#a00")
            self._status.set("Create failed -- check the log")

    # =========================================================================
    # Tab 2: Build
    # =========================================================================

    def _build_build_tab(self):
        tab = ttk.Frame(self._nb, padding=8)
        self._nb.add(tab, text="  Build  ")

        top = ttk.LabelFrame(tab, text="Build Mod Package", padding=12)
        top.pack(fill=tk.X)

        ttk.Label(top, text=(
            "Point to a mod workspace directory containing mod.json.\n"
            "The tool will build an H2M-compatible mod package."
        ), foreground="#555").pack(anchor=tk.W, pady=(0, 8))

        # Mod workspace picker
        row = ttk.Frame(top)
        row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(row, text="Mod workspace:", width=14, anchor=tk.W).pack(side=tk.LEFT)
        self._build_mod_dir = tk.StringVar()
        ttk.Entry(row, textvariable=self._build_mod_dir, width=42).pack(
            side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        ttk.Button(row, text="Browse\u2026",
                   command=self._browse_build_dir).pack(side=tk.LEFT)

        # Mod info display
        self._build_info = ttk.Label(top, text="", foreground="#555")
        self._build_info.pack(anchor=tk.W, pady=(2, 6))

        # Mod name + Author (used for Thunderstore packaging and r2modman install)
        meta_frame = ttk.Frame(top)
        meta_frame.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(meta_frame, text="Mod name:", width=14, anchor=tk.W).pack(side=tk.LEFT)
        self._build_mod_name = tk.StringVar()
        ttk.Entry(meta_frame, textvariable=self._build_mod_name, width=28).pack(
            side=tk.LEFT, padx=6)

        author_frame = ttk.Frame(top)
        author_frame.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(author_frame, text="Author:", width=14, anchor=tk.W).pack(side=tk.LEFT)
        self._build_author = tk.StringVar(value=self._config.get("author", "Modder"))
        ttk.Entry(author_frame, textvariable=self._build_author, width=20).pack(
            side=tk.LEFT, padx=6)

        # Options
        self._build_thunderstore = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Also create Thunderstore ZIP",
                        variable=self._build_thunderstore).pack(anchor=tk.W, pady=(0, 2))

        self._build_install_r2 = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Install to r2modman after build",
                        variable=self._build_install_r2).pack(anchor=tk.W, pady=(0, 6))

        # New mesh entry routing (populated when mod workspace is scanned)
        self._build_routing_frame = ttk.LabelFrame(top, text="New Mesh Entry Routing", padding=4)
        self._build_routing_vars = {}  # {mesh_name: {entry_name: BooleanVar}}

        # Build button + status
        btn_row = ttk.Frame(top)
        btn_row.pack(fill=tk.X, pady=(4, 0))
        self._build_btn = ttk.Button(btn_row, text="Build",
                                     command=self._build_mod, state=tk.DISABLED)
        self._build_btn.pack(side=tk.LEFT)
        self._build_status_label = ttk.Label(btn_row, text="", foreground="#555")
        self._build_status_label.pack(side=tk.LEFT, padx=12)

        self._build_progress = ttk.Progressbar(top, mode="indeterminate")
        self._build_progress.pack(fill=tk.X, pady=(6, 0))

        # Log
        self._build_log = self._make_log(tab)

    def _browse_build_dir(self):
        p = filedialog.askdirectory(title="Select mod workspace (containing mod.json)")
        if p:
            self._build_mod_dir.set(p)
            self._scan_build_dir(p)

    def _scan_build_dir(self, p):
        """Read mod.json and show summary."""
        mod_json_path = os.path.join(p, "mod.json")
        if not os.path.isfile(mod_json_path):
            self._build_info.config(text="No mod.json found in this directory",
                                    foreground="#a00")
            self._build_btn.config(state=tk.DISABLED)
            return
        try:
            with open(mod_json_path) as f:
                m = json.load(f)
        except Exception as e:
            self._build_info.config(text=f"Error reading mod.json: {e}",
                                    foreground="#a00")
            self._build_btn.config(state=tk.DISABLED)
            return

        meta = m.get("metadata", {})
        name = meta.get("name", m.get("name", "?"))
        author = meta.get("author", "?")
        mod_type = m.get("type", "?")
        character = m.get("target", {}).get("character", m.get("character", "?"))
        version = meta.get("version", m.get("version", "?"))

        # Populate mod name from mod.json, author from saved preference
        if name and name != "?":
            self._build_mod_name.set(name)
        saved_author = self._config.get("author", "")
        if saved_author:
            self._build_author.set(saved_author)
        elif author and author != "?":
            self._build_author.set(author)

        # Detect operations — run auto-detection to get accurate type
        # (mod.json may have stale type from initial create)
        if cg3h_build and hasattr(cg3h_build, "_sync_mod_json"):
            try:
                cg3h_build._sync_mod_json(p)
                # Re-read after sync
                with open(mod_json_path) as f:
                    m = json.load(f)
                meta = m.get("metadata", {})
                mod_type = m.get("type", "?")
            except Exception:
                pass  # sync is best-effort; fall back to whatever's already in mod.json

        ops_str = mod_type
        if isinstance(mod_type, list):
            ops_str = ", ".join(mod_type)
        if cg3h_build and hasattr(cg3h_build, "_infer_operations"):
            try:
                ops = cg3h_build._infer_operations(m)
                if ops:
                    ops_str = ", ".join(sorted(ops))
            except Exception:
                pass  # operation inference is best-effort, OK to skip

        # Show what will actually be built
        display_name = self._build_mod_name.get() or name
        display_author = self._build_author.get() or author
        self._build_info.config(
            text=f'"{display_name}" by {display_author}  |  {character}  |  {ops_str}  |  v{version}',
            foreground="#555",
        )
        if not self._build_running:
            self._build_btn.config(state=tk.NORMAL)

        # Detect new meshes for entry routing
        self._build_routing_frame.pack_forget()
        for w in self._build_routing_frame.winfo_children():
            w.destroy()
        self._build_routing_vars.clear()

        manifest_path = os.path.join(p, "manifest.json")
        glb_name = m.get("assets", {}).get("glb", "")
        glb_path = os.path.join(p, glb_name) if glb_name else ""
        mesh_entries = m.get("target", {}).get("mesh_entries", [])

        if len(mesh_entries) > 1 and os.path.isfile(manifest_path) and os.path.isfile(glb_path):
            try:
                with open(manifest_path) as f:
                    manifest = json.load(f)
                manifest_mesh_names = {mm["name"] for mm in manifest.get("meshes", [])}

                if pygltflib is None:
                    return
                gltf = pygltflib.GLTF2().load(glb_path)
                new_meshes = [m.name for m in gltf.meshes if m.name not in manifest_mesh_names]

                if new_meshes:
                    self._build_routing_frame.pack(fill=tk.X, pady=(4, 0))
                    # Load existing routing from mod.json
                    existing_routing = m.get("target", {}).get("new_mesh_routing", {})

                    for mesh_name in new_meshes:
                        mesh_frame = ttk.LabelFrame(self._build_routing_frame,
                                                     text=mesh_name, padding=2)
                        mesh_frame.pack(fill=tk.X, pady=(0, 2))
                        self._build_routing_vars[mesh_name] = {}
                        existing = existing_routing.get(mesh_name, mesh_entries)
                        for entry in mesh_entries:
                            var = tk.BooleanVar(value=(entry in existing))
                            self._build_routing_vars[mesh_name][entry] = var
                            ttk.Checkbutton(mesh_frame, text=entry,
                                            variable=var).pack(side=tk.LEFT, padx=4)
            except Exception:
                pass  # GLB inspection failed — skip routing UI for new meshes

    def _build_mod(self):
        if cg3h_build is None:
            messagebox.showerror(
                "Module not found",
                "cg3h_build module could not be imported.\n"
                "Ensure cg3h_build.py is available on the Python path.",
            )
            return

        mod_dir = self._build_mod_dir.get().strip()
        if not mod_dir or not os.path.isdir(mod_dir):
            messagebox.showwarning("No mod directory", "Browse to a mod workspace first.")
            return
        if not os.path.isfile(os.path.join(mod_dir, "mod.json")):
            messagebox.showerror("No mod.json", f"mod.json not found in:\n{mod_dir}")
            return

        # Save author preference
        author = self._build_author.get().strip() or "Modder"
        self._config["author"] = author
        self._save_config()

        # Update mod.json with current mod name, author, and routing before building
        mod_name = self._build_mod_name.get().strip()
        try:
            mod_json_path = os.path.join(mod_dir, "mod.json")
            with open(mod_json_path) as f:
                m = json.load(f)
            if mod_name:
                m.setdefault("metadata", {})["name"] = mod_name
            m.setdefault("metadata", {})["author"] = author

            # Write new_mesh_routing from GUI checkboxes.  Always write
            # an entry per new mesh — even when it covers every
            # mesh_entries value — because the runtime per-mesh
            # visibility gate (rom.data.set_mesh_visible) looks the mesh
            # name up via this map.  Empty selection is the only case we
            # skip; a full-coverage list is valid and required.
            if self._build_routing_vars:
                routing = {}
                for mesh_name, entry_vars in self._build_routing_vars.items():
                    selected = [e for e, v in entry_vars.items() if v.get()]
                    if selected:
                        routing[mesh_name] = selected
                if routing:
                    m.setdefault("target", {})["new_mesh_routing"] = routing
                elif "new_mesh_routing" in m.get("target", {}):
                    del m["target"]["new_mesh_routing"]

            with open(mod_json_path, 'w') as f:
                json.dump(m, f, indent=2)
        except Exception as e:
            print(f"  WARNING: failed to update mod.json: {e}")

        self._log_clear(self._build_log)
        self._build_running = True
        self._build_btn.config(state=tk.DISABLED)
        self._build_status_label.config(text="Building...", foreground="#555")
        self._build_progress.start(12)

        threading.Thread(
            target=self._build_worker,
            args=(mod_dir,),
            daemon=True,
        ).start()

    def _build_worker(self, mod_dir):
        game_dir = self.game_path.get()
        output_path = None
        ts_path = None

        self._log_write_ui(self._build_log, f"Building mod from: {mod_dir}\n\n")

        # Capture stdout/stderr
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), \
                 contextlib.redirect_stderr(buf):
                output_path = cg3h_build.build_mod(mod_dir, game_dir=game_dir)
        except Exception as e:
            self._log_write_ui(self._build_log, f"\nBuild failed: {e}\n")
            self._ui(lambda: self._build_finish(False, mod_dir))
            return
        finally:
            captured = buf.getvalue()
            if captured:
                self._log_write_ui(self._build_log, captured)

        if not output_path:
            self._log_write_ui(self._build_log, "\nBuild returned no output\n")
            self._ui(lambda: self._build_finish(False, mod_dir))
            return

        self._log_write_ui(self._build_log, f"\nBuild output: {output_path}\n")

        # Thunderstore packaging
        if self._build_thunderstore.get():
            self._log_write_ui(self._build_log, "\nCreating Thunderstore ZIP...\n")
            try:
                buf2 = io.StringIO()
                with contextlib.redirect_stdout(buf2), \
                     contextlib.redirect_stderr(buf2):
                    ts_path = cg3h_build.package_thunderstore(mod_dir)
                captured2 = buf2.getvalue()
                if captured2:
                    self._log_write_ui(self._build_log, captured2)
                if ts_path:
                    self._log_write_ui(self._build_log,
                                       f"Thunderstore ZIP: {ts_path}\n")
            except Exception as e:
                self._log_write_ui(self._build_log,
                                   f"Thunderstore packaging failed: {e}\n")

        # Install to r2modman
        if self._build_install_r2.get():
            self._install_to_r2modman(mod_dir)

        self._ui(lambda: self._build_finish(True, mod_dir))

    def _install_to_r2modman(self, mod_dir):
        """Install build output to r2modman as a proper managed mod."""
        r2_dir = self._find_r2modman_dir()
        if not r2_dir:
            self._log_write_ui(self._build_log,
                               "\nWARNING: r2modman ReturnOfModding directory not found\n")
            return

        # Read mod.json for the mod id
        try:
            with open(os.path.join(mod_dir, "mod.json")) as f:
                m = json.load(f)
            meta = m.get("metadata", {})
            author = meta.get("author", "Unknown")
            name = meta.get("name", "UnnamedMod")
            version = meta.get("version", "1.0.0")
            description = meta.get("description", "")
            mod_id = f"{author}-{name}".replace(" ", "")
        except Exception:
            mod_id = os.path.basename(mod_dir)
            author, name, version, description = "Unknown", mod_id, "1.0.0", ""

        # Copy build output to ReturnOfModding
        build_dir = os.path.join(mod_dir, "build")
        for subdir in ("plugins_data", "plugins"):
            src = os.path.join(build_dir, subdir, mod_id)
            dst = os.path.join(r2_dir, subdir, mod_id)
            if os.path.isdir(src):
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)

        # Copy mod.json to plugins_data so Mods tab can find it
        mod_json_src = os.path.join(mod_dir, "mod.json")
        mod_json_dst = os.path.join(r2_dir, "plugins_data", mod_id, "mod.json")
        if os.path.isfile(mod_json_src):
            shutil.copy2(mod_json_src, mod_json_dst)

        # Populate r2modman's cache so the mod tile shows an icon + name in
        # the profile UI.  Without this, r2modman renders the mod with a
        # broken-image placeholder and the namespace-hyphen-name slug
        # instead of the authored display name.  Mirrors the layout
        # r2modman creates when it installs a mod from Thunderstore:
        #
        #     cache/<Namespace>-<Name>/<version>/
        #         icon.png
        #         manifest.json
        #         plugins/<mod_id>/...
        #         plugins_data/<mod_id>/...
        try:
            self._populate_r2modman_cache(r2_dir, mod_id, version, mod_dir, build_dir)
        except Exception as e:
            self._log_write_ui(self._build_log,
                               f"\n(cache population failed: {e} — "
                               f"mod still installed; icon will be missing)\n")

        # Register in r2modman's mods.yml so it appears in the mod list
        try:
            mods_yml_path = os.path.normpath(os.path.join(r2_dir, "..", "mods.yml"))

            # Read existing entries, preserving them as raw text blocks
            existing_text = ""
            if os.path.isfile(mods_yml_path):
                with open(mods_yml_path) as f:
                    existing_text = f.read()

            # Remove old entry for this mod if present
            lines = existing_text.split("\n")
            filtered = []
            skip = False
            for i, line in enumerate(lines):
                if line.startswith("- manifestVersion:"):
                    if skip:
                        skip = False
                    # Peek ahead for name field
                    block = "\n".join(lines[i:i+5])
                    if f"name: {mod_id}" in block:
                        skip = True
                        continue
                if skip and not line.startswith("- "):
                    continue
                if skip and line.startswith("- "):
                    skip = False
                filtered.append(line)

            # Parse version
            parts = version.split(".")
            v_major = int(parts[0]) if len(parts) > 0 else 1
            v_minor = int(parts[1]) if len(parts) > 1 else 0
            v_patch = int(parts[2]) if len(parts) > 2 else 0

            # Append new entry
            entry = (
                f"- manifestVersion: 1\n"
                f"  name: {mod_id}\n"
                f"  authorName: {author}\n"
                f"  websiteUrl: ''\n"
                f"  displayName: {name}\n"
                f"  description: {description}\n"
                f"  gameVersion: ''\n"
                f"  networkMode: ''\n"
                f"  packageType: ''\n"
                f"  installMode: ''\n"
                f"  installedAtTime: {int(time.time() * 1000)}\n"
                f"  loaders: []\n"
                f"  dependencies:\n"
                f"    - {CG3H_BUILDER_DEPENDENCY}\n"
                f"  incompatibilities: []\n"
                f"  optionalDependencies: []\n"
                f"  versionNumber:\n"
                f"    major: {v_major}\n"
                f"    minor: {v_minor}\n"
                f"    patch: {v_patch}\n"
                f"  enabled: true\n"
            )

            result = "\n".join(filtered).rstrip() + "\n" + entry
            with open(mods_yml_path, "w") as f:
                f.write(result)

            self._log_write_ui(self._build_log,
                               f"\nInstalled to r2modman: {mod_id}\n")
        except Exception as e:
            self._log_write_ui(self._build_log,
                               f"\nInstalled files but failed to register in mods.yml: {e}\n")

    def _build_finish(self, success, mod_dir):
        self._build_running = False
        self._build_progress.stop()

        # Re-enable button if valid
        if mod_dir and os.path.isfile(os.path.join(mod_dir, "mod.json")):
            self._build_btn.config(state=tk.NORMAL)

        if success:
            self._build_status_label.config(text="Build complete!", foreground="#070")
            self._status.set("Build done")
        else:
            self._build_status_label.config(text="Build failed", foreground="#a00")
            self._status.set("Build failed -- check the log")

    # =========================================================================
    # Tab 3: Mods
    # =========================================================================

    def _build_mods_tab(self):
        tab = ttk.Frame(self._nb, padding=8)
        self._nb.add(tab, text="  Mods  ")

        # Runtime status
        status_frame = ttk.LabelFrame(tab, text="CG3HBuilder Runtime", padding=8)
        status_frame.pack(fill=tk.X)
        self._runtime_status = ttk.Label(status_frame, text="", foreground="#555")
        self._runtime_status.pack(anchor=tk.W)

        # Installed mods list
        list_frame = ttk.LabelFrame(tab, text="Installed CG3H Mods", padding=8)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        # Mod listbox
        lb_frame = ttk.Frame(list_frame)
        lb_frame.pack(fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(lb_frame, orient=tk.VERTICAL)
        self._mods_lb = tk.Listbox(
            lb_frame, selectmode=tk.BROWSE, yscrollcommand=sb.set,
            exportselection=False, font=("Consolas", 9),
        )
        sb.config(command=self._mods_lb.yview)
        self._mods_lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._mods_lb.bind("<<ListboxSelect>>", lambda _: self._mods_on_select())

        # Conflict / info display
        self._mods_info = ttk.Label(list_frame, text="", foreground="#555",
                                    wraplength=600, justify=tk.LEFT)
        self._mods_info.pack(anchor=tk.W, pady=(4, 0))

        # Buttons
        btn_frame = ttk.Frame(list_frame)
        btn_frame.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(btn_frame, text="Refresh", command=self._mods_refresh).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="Open Folder", command=self._mods_edit).pack(
            side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Remove", command=self._mods_remove).pack(
            side=tk.LEFT, padx=4)

        # Internal data
        self._mods_data: list[dict] = []
        self._mods_groups: dict[str, list] = {}

    def _on_tab_changed(self, event):
        idx = self._nb.index("current")
        if idx == 2:
            self._mods_refresh()

    def _mods_refresh(self):
        """Scan r2modman plugins_data for installed CG3H mods."""
        self._mods_lb.delete(0, tk.END)
        self._mods_data.clear()
        self._mods_groups.clear()
        self._mods_info.config(text="")

        r2_dir = self._find_r2modman_dir()
        if not r2_dir:
            self._runtime_status.config(
                text="r2modman ReturnOfModding directory not found",
                foreground="#a00")
            self._mods_lb.insert(tk.END, "(r2modman not found)")
            return

        plugins_data_dir = os.path.join(r2_dir, "plugins_data")
        plugins_dir = os.path.join(r2_dir, "plugins")

        # Check CG3HBuilder runtime
        builder_plugin = os.path.join(plugins_dir, "CG3HBuilder", "main.lua")
        builder_exe = os.path.join(plugins_data_dir, "CG3HBuilder", "cg3h_builder.exe")
        if os.path.isfile(builder_plugin) and os.path.isfile(builder_exe):
            self._runtime_status.config(
                text="CG3HBuilder installed and ready", foreground="#070")
        elif os.path.isfile(builder_plugin):
            self._runtime_status.config(
                text="CG3HBuilder plugin found but cg3h_builder.exe missing",
                foreground="#a50")
        else:
            self._runtime_status.config(
                text="CG3HBuilder not installed — mesh mods will not work",
                foreground="#a00")

        # Scan plugins_data for CG3H mods
        mods = []
        if os.path.isdir(plugins_data_dir):
            for entry in sorted(os.listdir(plugins_data_dir)):
                if entry == "CG3HBuilder":
                    continue
                data_path = os.path.join(plugins_data_dir, entry)
                if not os.path.isdir(data_path):
                    continue
                mod_json_path = os.path.join(data_path, "mod.json")
                if not os.path.isfile(mod_json_path):
                    continue
                try:
                    with open(mod_json_path) as f:
                        mod = json.load(f)
                except (json.JSONDecodeError, OSError):
                    continue
                if not mod.get("format", "").startswith("cg3h-mod"):
                    continue
                plugin_path = os.path.join(plugins_dir, entry)
                mods.append({
                    "id": entry,
                    "mod_json_path": mod_json_path,
                    "plugin_path": plugin_path,
                    "data_path": data_path,
                    "mod": mod,
                })

        self._mods_data = mods

        # Group by character
        for m in mods:
            char = m["mod"].get("target", {}).get("character", "")
            if char:
                self._mods_groups.setdefault(char, []).append(m)

        if not mods:
            self._mods_lb.insert(tk.END, "(no CG3H mods installed)")
            return

        # Check GPK build status per character
        gpk_status = {}
        for char, group in self._mods_groups.items():
            gpk_path = os.path.join(plugins_data_dir, "CG3HBuilder", f"{char}.gpk")
            gpk_status[char] = "built" if os.path.isfile(gpk_path) else "needs build"

        for m in mods:
            mod = m["mod"]
            meta = mod.get("metadata", {})
            name = meta.get("name", m["id"])
            author = meta.get("author", "?")
            char = mod.get("target", {}).get("character", "?")
            mod_type = mod.get("type", "?")
            if isinstance(mod_type, list):
                mod_type = "+".join(mod_type)

            # Check if plugin stub exists
            has_plugin = os.path.isdir(m.get("plugin_path", ""))
            marker = "\u25cf" if has_plugin else "\u25cb"

            # GPK status
            build = gpk_status.get(char, "?")
            build_tag = "" if build == "built" else "  [needs build]"

            # Multi-mod indicator
            multi = ""
            if char in self._mods_groups and len(self._mods_groups[char]) > 1:
                multi = f"  [merge: {len(self._mods_groups[char])}]"

            line = f"{marker} {name} ({char})  [{mod_type}]  by {author}{build_tag}{multi}"
            self._mods_lb.insert(tk.END, line)

        self._status.set(f"Found {len(mods)} CG3H mod(s)")

    def _mods_on_select(self):
        """Show info about selected mod."""
        sel = self._mods_lb.curselection()
        if not sel or sel[0] >= len(self._mods_data):
            self._mods_info.config(text="")
            return

        m = self._mods_data[sel[0]]
        mod = m["mod"]
        meta = mod.get("metadata", {})
        char = mod.get("target", {}).get("character", "?")
        desc = meta.get("description", "")
        version = meta.get("version", "?")
        data_path = m.get("data_path", "")

        # Check for conflicts
        conflict_text = ""
        if char in self._mods_groups and len(self._mods_groups[char]) > 1:
            others = [o["id"] for o in self._mods_groups[char] if o["id"] != m["id"]]
            conflict_text = f"  |  Conflicts with: {', '.join(others)}"

        info = f"v{version}  |  {char}  |  {desc}{conflict_text}\nPath: {data_path}"
        self._mods_info.config(text=info)

    def _mods_selected(self):
        """Return the selected mod dict or None."""
        sel = self._mods_lb.curselection()
        if not sel or sel[0] >= len(self._mods_data):
            return None
        return self._mods_data[sel[0]]

    def _mods_edit(self):
        """Open the mod workspace folder."""
        m = self._mods_selected()
        if not m:
            messagebox.showinfo("No selection", "Select a mod first.")
            return
        data_path = m.get("data_path", "")
        if os.path.isdir(data_path):
            os.startfile(data_path)
        else:
            messagebox.showwarning("Not found", f"Directory not found:\n{data_path}")

    def _mods_remove(self):
        """Remove the selected mod from r2modman (keeps workspace)."""
        m = self._mods_selected()
        if not m:
            messagebox.showinfo("No selection", "Select a mod first.")
            return

        if not messagebox.askyesno("Confirm removal",
                                   f"Remove {m['id']} from r2modman?\n"
                                   "(The mod workspace is NOT deleted.)"):
            return

        removed = False
        for path_key in ("plugin_path", "data_path"):
            p = m.get(path_key, "")
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
                removed = True
            # Also check .disabled variant
            if os.path.isdir(p + ".disabled"):
                shutil.rmtree(p + ".disabled", ignore_errors=True)
                removed = True

        if removed:
            self._mods_refresh()
            self._status.set(f"Removed: {m['id']}")
        else:
            messagebox.showinfo("Nothing removed", "Could not find installed files.")


    # =========================================================================
    # Shared helpers
    # =========================================================================

    def _make_log(self, parent) -> tk.Text:
        box = ttk.LabelFrame(parent, text="Output log", padding=4)
        box.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        sb = ttk.Scrollbar(box, orient=tk.VERTICAL)
        log = tk.Text(
            box, state=tk.DISABLED, wrap=tk.WORD,
            yscrollcommand=sb.set,
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
            font=("Consolas", 9), relief=tk.FLAT,
        )
        sb.config(command=log.yview)
        log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        return log

    def _scan(self):
        gpk_dir = self._gpk_dir()
        if not os.path.isdir(gpk_dir):
            self._all_names = []
            self._status.set("GPK directory not found -- check game path.")
        else:
            self._all_names = sorted(
                os.path.splitext(os.path.basename(f))[0]
                for f in glob.glob(os.path.join(gpk_dir, "*.gpk"))
            )
            self._status.set(
                f"Found {len(self._all_names)} models in .../Content/GR2/_Optimized/"
            )
        # Update Create tab character list
        if hasattr(self, "_create_char_lb"):
            self._create_char_lb.delete(0, tk.END)
            for n in self._all_names:
                self._create_char_lb.insert(tk.END, n)

    def _filter_characters(self, event=None):
        """Filter character list as user types."""
        typed = self._create_char.get().lower()
        self._create_char_lb.delete(0, tk.END)
        names = self._all_names if not typed else [
            n for n in self._all_names if typed in n.lower()]
        for n in names:
            self._create_char_lb.insert(tk.END, n)
        # Auto-select first match
        if names:
            self._create_char_lb.select_set(0)
            self._create_char_lb.see(0)

    def _on_char_select(self, event=None):
        """Set character entry from listbox selection and populate entry checkboxes."""
        sel = self._create_char_lb.curselection()
        if not sel:
            return
        character = self._create_char_lb.get(sel[0])
        self._create_char.set(character)
        self._populate_entry_checkboxes(character)

    def _populate_entry_checkboxes(self, character):
        """Populate mesh entry checkboxes for the selected character."""
        # Clear existing checkboxes
        for w in self._create_entries_frame.winfo_children():
            w.destroy()
        self._create_entry_vars.clear()

        # Find mesh entries from the GPK
        gpk_dir = self._gpk_dir()
        gpk_path = os.path.join(gpk_dir, f"{character}.gpk")
        if not os.path.isfile(gpk_path):
            ttk.Label(self._create_entries_frame, text="GPK not found",
                      foreground="#a00").pack(anchor=tk.W)
            return

        try:
            with open(gpk_path, 'rb') as f:
                data = f.read()
            count = struct.unpack_from('<I', data, 4)[0]
            pos = 8
            mesh_entries = []
            for _ in range(count):
                nl = data[pos]
                pos += 1
                name = data[pos:pos+nl].decode()
                pos += nl
                cs = struct.unpack_from('<I', data, pos)[0]
                pos += 4
                if name.endswith('_Mesh'):
                    mesh_entries.append(name)
                pos += cs
        except Exception:
            ttk.Label(self._create_entries_frame, text="Could not read GPK",
                      foreground="#a00").pack(anchor=tk.W)
            return

        if len(mesh_entries) <= 1:
            ttk.Label(self._create_entries_frame, text=f"Single entry: {mesh_entries[0] if mesh_entries else 'none'}",
                      foreground="#555").pack(anchor=tk.W)
            return

        for entry in mesh_entries:
            var = tk.BooleanVar(value=True)
            self._create_entry_vars[entry] = var
            ttk.Checkbutton(self._create_entries_frame, text=entry,
                            variable=var).pack(anchor=tk.W)

    # -- Browse dialogs -------------------------------------------------------

    def _browse_game(self):
        p = filedialog.askdirectory(
            title="Select Hades II game directory",
            initialdir=self.game_path.get() or STEAM_PATHS[0],
        )
        if p:
            self.game_path.set(p)

    def _browse_dir(self, var: tk.StringVar):
        p = filedialog.askdirectory(title="Select directory",
                                    initialdir=var.get() or DEFAULT_OUTPUT)
        if p:
            var.set(p)

    # -- Thread-safe UI helpers -----------------------------------------------

    def _ui(self, fn):
        self.root.after(0, fn)

    def _log_write_ui(self, log: tk.Text, text: str):
        self._ui(lambda t=text, l=log: self._log_write(l, t))

    def _log_write(self, log: tk.Text, text: str):
        log.config(state=tk.NORMAL)
        log.insert(tk.END, text)
        log.see(tk.END)
        log.config(state=tk.DISABLED)

    def _log_clear(self, log: tk.Text):
        log.config(state=tk.NORMAL)
        log.delete("1.0", tk.END)
        log.config(state=tk.DISABLED)


# -- Entry point --------------------------------------------------------------

def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
