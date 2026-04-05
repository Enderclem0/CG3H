"""
CG3H Mod Builder — GUI

Three tabs:
  Create — pick a character, configure export options, create a mod workspace
  Build  — point to a mod workspace, build for H2M, install to r2modman
  Mods   — manage installed CG3H mods, merge order, rebuild

Requires: numpy, pygltflib, lz4  (pip install numpy pygltflib lz4)
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import cg3h_build
except ImportError:
    cg3h_build = None

try:
    import mod_merger
except ImportError:
    mod_merger = None

# -- Constants ----------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EXPORTER   = os.path.join(SCRIPT_DIR, "gr2_to_gltf.py")
IMPORTER   = os.path.join(SCRIPT_DIR, "gltf_to_gr2.py")

DEFAULT_OUTPUT = os.path.join(os.path.expanduser("~"), "Documents", "CG3H_Mods")

STEAM_PATHS = [
    r"C:\Program Files (x86)\Steam\steamapps\common\Hades II",
    r"C:\Program Files\Steam\steamapps\common\Hades II",
    r"D:\Steam\steamapps\common\Hades II",
    r"D:\SteamLibrary\steamapps\common\Hades II",
    r"E:\SteamLibrary\steamapps\common\Hades II",
]


def find_game_path():
    for p in STEAM_PATHS:
        if os.path.isdir(p):
            return p
    return ""


# -- App ----------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("CG3H Mod Builder")
        self.root.minsize(900, 650)

        self.game_path = tk.StringVar(value=find_game_path())
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

        # Character dropdown
        row = ttk.Frame(top)
        row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(row, text="Character:", width=14, anchor=tk.W).pack(side=tk.LEFT)
        self._create_char = tk.StringVar()
        self._create_char_combo = ttk.Combobox(
            row, textvariable=self._create_char, state="readonly", width=36)
        self._create_char_combo.pack(side=tk.LEFT, padx=6)

        # Mod name
        row2 = ttk.Frame(top)
        row2.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(row2, text="Mod name:", width=14, anchor=tk.W).pack(side=tk.LEFT)
        self._create_mod_name = tk.StringVar()
        ttk.Entry(row2, textvariable=self._create_mod_name, width=28).pack(
            side=tk.LEFT, padx=6)

        # Author
        row3 = ttk.Frame(top)
        row3.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(row3, text="Author:", width=14, anchor=tk.W).pack(side=tk.LEFT)
        self._create_author = tk.StringVar(value="Modder")
        ttk.Entry(row3, textvariable=self._create_author, width=20).pack(
            side=tk.LEFT, padx=6)

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

        mesh_row = ttk.Frame(opt_frame)
        mesh_row.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(mesh_row, text="Mesh entries:", foreground="#555").pack(side=tk.LEFT)
        self._create_mesh_entry = tk.StringVar()
        ttk.Entry(mesh_row, textvariable=self._create_mesh_entry, width=28).pack(
            side=tk.LEFT, padx=4)
        ttk.Label(mesh_row, text="blank = all, comma-sep to filter",
                  foreground="#888", font=("", 8)).pack(side=tk.LEFT)

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

        mod_name = self._create_mod_name.get().strip()
        if not mod_name:
            messagebox.showwarning("No mod name", "Enter a mod name.")
            return

        author = self._create_author.get().strip() or "Modder"
        out_base = self._create_output.get().strip() or DEFAULT_OUTPUT
        # Workspace folder: output/ModName/
        workspace = os.path.join(out_base, mod_name.replace(" ", ""))

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
            args=(character, mod_name, author, workspace, dll),
            daemon=True,
        ).start()

    def _create_worker(self, character, mod_name, author, workspace, dll):
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
        mesh_entry = self._create_mesh_entry.get().strip()
        if mesh_entry:
            cmd += ["--mesh-entry", mesh_entry]

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
            mesh_entries = [e.strip() for e in mesh_entry.split(",") if e.strip()] \
                if mesh_entry else [f"{character}_Mesh"]
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
                               f"\nGenerated mod.json\n")
        except Exception as exc:
            self._log_write_ui(self._create_log, f"\nmod.json error: {exc}\n")

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

        # Options
        self._build_thunderstore = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Also create Thunderstore ZIP",
                        variable=self._build_thunderstore).pack(anchor=tk.W, pady=(0, 2))

        self._build_install_r2 = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Install to r2modman after build",
                        variable=self._build_install_r2).pack(anchor=tk.W, pady=(0, 6))

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

        # Detect operations if cg3h_build is available
        ops_str = mod_type
        if cg3h_build and hasattr(cg3h_build, "_infer_operations"):
            try:
                ops = cg3h_build._infer_operations(m)
                if ops:
                    ops_str = ", ".join(sorted(ops))
            except Exception:
                pass

        self._build_info.config(
            text=f'"{name}" by {author}  |  {character}  |  {ops_str}  |  v{version}',
            foreground="#555",
        )
        if not self._build_running:
            self._build_btn.config(state=tk.NORMAL)

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
        import io
        import contextlib

        ship_dir = os.path.join(self.game_path.get(), "Ship")
        orig_cwd = os.getcwd()
        output_path = None
        ts_path = None

        try:
            if os.path.isdir(ship_dir):
                os.chdir(ship_dir)

            self._log_write_ui(self._build_log, f"Building mod from: {mod_dir}\n")
            self._log_write_ui(self._build_log, f"Working directory: {os.getcwd()}\n\n")

            # Capture stdout/stderr
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf), \
                     contextlib.redirect_stderr(buf):
                    output_path = cg3h_build.build_mod(mod_dir)
            except Exception:
                pass
            finally:
                captured = buf.getvalue()
                if captured:
                    self._log_write_ui(self._build_log, captured)

            if output_path is None:
                try:
                    output_path = cg3h_build.build_mod(mod_dir)
                except Exception as e:
                    self._log_write_ui(self._build_log, f"\nBuild failed: {e}\n")
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

        finally:
            os.chdir(orig_cwd)

        self._ui(lambda: self._build_finish(True, mod_dir))

    def _install_to_r2modman(self, mod_dir):
        """Copy build output to r2modman ReturnOfModding directory."""
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
            mod_id = f"{author}-{name}".replace(" ", "")
        except Exception:
            mod_id = os.path.basename(mod_dir)

        build_dir = os.path.join(mod_dir, "build")
        for subdir in ("plugins_data", "plugins"):
            src = os.path.join(build_dir, subdir, mod_id)
            dst = os.path.join(r2_dir, subdir, mod_id)
            if os.path.isdir(src):
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)

        self._log_write_ui(self._build_log, f"\nInstalled to r2modman: {r2_dir}\n")

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

        # Installed mods list
        list_frame = ttk.LabelFrame(tab, text="Installed CG3H Mods", padding=8)
        list_frame.pack(fill=tk.BOTH, expand=True)

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
        ttk.Button(btn_frame, text="Edit", command=self._mods_edit).pack(
            side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Rebuild", command=self._mods_rebuild).pack(
            side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Disable", command=self._mods_disable).pack(
            side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Remove", command=self._mods_remove).pack(
            side=tk.LEFT, padx=4)

        # Merge order
        merge_frame = ttk.LabelFrame(tab, text="Merge Order (per character)", padding=8)
        merge_frame.pack(fill=tk.X, pady=(8, 0))

        merge_top = ttk.Frame(merge_frame)
        merge_top.pack(fill=tk.X)

        ttk.Label(merge_top, text="Character:").pack(side=tk.LEFT)
        self._merge_char = tk.StringVar()
        self._merge_char_combo = ttk.Combobox(
            merge_top, textvariable=self._merge_char, state="readonly", width=24)
        self._merge_char_combo.pack(side=tk.LEFT, padx=6)
        self._merge_char_combo.bind("<<ComboboxSelected>>",
                                    lambda _: self._merge_refresh_order())

        merge_list_frame = ttk.Frame(merge_frame)
        merge_list_frame.pack(fill=tk.X, pady=(4, 0))

        self._merge_lb = tk.Listbox(
            merge_list_frame, height=4, font=("Consolas", 9),
            exportselection=False)
        self._merge_lb.pack(side=tk.LEFT, fill=tk.X, expand=True)

        merge_btn_frame = ttk.Frame(merge_list_frame)
        merge_btn_frame.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(merge_btn_frame, text="\u25b2", width=3,
                   command=self._merge_move_up).pack(pady=(0, 2))
        ttk.Button(merge_btn_frame, text="\u25bc", width=3,
                   command=self._merge_move_down).pack()

        merge_action = ttk.Frame(merge_frame)
        merge_action.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(merge_action, text="Rebuild Merged",
                   command=self._merge_rebuild).pack(side=tk.LEFT)
        self._merge_status = ttk.Label(merge_action, text="", foreground="#555")
        self._merge_status.pack(side=tk.LEFT, padx=8)

        # Internal data
        self._mods_data: list[dict] = []
        self._mods_groups: dict[str, list] = {}
        self._mods_priority: dict[str, list] = {}

    def _on_tab_changed(self, event):
        idx = self._nb.index("current")
        if idx == 2:
            self._mods_refresh()

    def _mods_refresh(self):
        """Scan r2modman for installed CG3H mods."""
        self._mods_lb.delete(0, tk.END)
        self._mods_data.clear()
        self._mods_groups.clear()
        self._mods_info.config(text="")

        r2_dir = self._find_r2modman_dir()
        if not r2_dir:
            self._mods_lb.insert(tk.END, "(r2modman ReturnOfModding not found)")
            return

        if mod_merger is None:
            self._mods_lb.insert(tk.END, "(mod_merger module not available)")
            return

        mods = mod_merger.scan_cg3h_mods(r2_dir)
        self._mods_data = mods
        self._mods_groups = mod_merger.group_by_character(mods)
        self._mods_priority = mod_merger.load_priority(r2_dir)

        if not mods:
            self._mods_lb.insert(tk.END, "(no CG3H mods installed)")
            return

        # Check for conflicts per character
        conflict_chars = set()
        for char, group in self._mods_groups.items():
            if len(group) > 1:
                conflict_chars.add(char)

        for m in mods:
            mod = m["mod"]
            meta = mod.get("metadata", {})
            name = meta.get("name", m["id"])
            author = meta.get("author", "?")
            char = mod.get("target", {}).get("character", "?")
            mod_type = mod.get("type", "?")

            # Check if disabled (plugin dir missing or empty)
            plugin_path = m.get("plugin_path", "")
            is_active = os.path.isdir(plugin_path) and bool(os.listdir(plugin_path))
            status = "active" if is_active else "disabled"
            marker = "\u25cf" if is_active else "\u25cb"

            conflict_warn = ""
            if char in conflict_chars:
                conflict_warn = "  [!conflict]"

            line = f"{marker} {name} ({char})  [{mod_type}]  by {author}  -- {status}{conflict_warn}"
            self._mods_lb.insert(tk.END, line)

        # Populate merge character dropdown
        chars_with_multiple = sorted(c for c, g in self._mods_groups.items() if len(g) > 1)
        self._merge_char_combo["values"] = chars_with_multiple
        if chars_with_multiple:
            self._merge_char.set(chars_with_multiple[0])
            self._merge_refresh_order()

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

    def _mods_rebuild(self):
        """Rebuild the selected mod via cg3h_build."""
        m = self._mods_selected()
        if not m:
            messagebox.showinfo("No selection", "Select a mod first.")
            return
        if cg3h_build is None:
            messagebox.showerror("Module not found", "cg3h_build module not available.")
            return

        # Find mod.json location to use as the build source
        mod_json_path = m.get("mod_json_path", "")
        mod_dir = os.path.dirname(mod_json_path) if mod_json_path else ""
        if not mod_dir or not os.path.isfile(mod_json_path):
            messagebox.showwarning("No mod.json", "Cannot find mod.json for this mod.")
            return

        # Switch to Build tab and populate
        self._build_mod_dir.set(mod_dir)
        self._scan_build_dir(mod_dir)
        self._nb.select(1)

    def _mods_disable(self):
        """Disable the selected mod by renaming plugin dir."""
        m = self._mods_selected()
        if not m:
            messagebox.showinfo("No selection", "Select a mod first.")
            return

        plugin_path = m.get("plugin_path", "")
        disabled_path = plugin_path + ".disabled"
        if os.path.isdir(plugin_path):
            try:
                os.rename(plugin_path, disabled_path)
                self._mods_refresh()
                self._status.set(f"Disabled: {m['id']}")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to disable:\n{e}")
        else:
            messagebox.showinfo("Already disabled", "This mod appears to already be disabled.")

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

    # -- Merge order ----------------------------------------------------------

    def _merge_refresh_order(self):
        """Show merge order for the selected character."""
        self._merge_lb.delete(0, tk.END)
        char = self._merge_char.get()
        if not char or char not in self._mods_groups:
            return

        r2_dir = self._find_r2modman_dir()
        if r2_dir and mod_merger:
            self._mods_priority = mod_merger.load_priority(r2_dir)

        group = self._mods_groups.get(char, [])
        mod_ids = [m["id"] for m in group]

        # Use priority order if available, else alphabetical
        order = self._mods_priority.get(char, sorted(mod_ids))
        # Filter to actually installed
        order = [mid for mid in order if mid in mod_ids]
        # Append any not in priority
        for mid in mod_ids:
            if mid not in order:
                order.append(mid)

        for i, mid in enumerate(order):
            self._merge_lb.insert(tk.END, f"{i+1}. {mid}")

    def _merge_move_up(self):
        sel = self._merge_lb.curselection()
        if not sel or sel[0] == 0:
            return
        idx = sel[0]
        self._merge_swap(idx, idx - 1)

    def _merge_move_down(self):
        sel = self._merge_lb.curselection()
        if not sel or sel[0] >= self._merge_lb.size() - 1:
            return
        idx = sel[0]
        self._merge_swap(idx, idx + 1)

    def _merge_swap(self, a, b):
        char = self._merge_char.get()
        if not char:
            return

        # Extract current order from listbox
        order = []
        for i in range(self._merge_lb.size()):
            text = self._merge_lb.get(i)
            # Strip "N. " prefix
            mid = text.split(". ", 1)[1] if ". " in text else text
            order.append(mid)

        order[a], order[b] = order[b], order[a]

        # Save priority
        r2_dir = self._find_r2modman_dir()
        if r2_dir and mod_merger:
            self._mods_priority[char] = order
            mod_merger.save_priority(r2_dir, self._mods_priority)

        # Refresh display
        self._merge_lb.delete(0, tk.END)
        for i, mid in enumerate(order):
            self._merge_lb.insert(tk.END, f"{i+1}. {mid}")
        self._merge_lb.select_set(b)

    def _merge_rebuild(self):
        """Run mod_merger.merge_all for the current r2modman directory."""
        if mod_merger is None:
            messagebox.showerror("Module not found", "mod_merger module not available.")
            return

        r2_dir = self._find_r2modman_dir()
        if not r2_dir:
            messagebox.showwarning("Not found", "r2modman ReturnOfModding not found.")
            return

        self._merge_status.config(text="Merging...", foreground="#555")

        def worker():
            import io
            import contextlib

            game_dir = self.game_path.get()
            orig_cwd = os.getcwd()
            try:
                ship_dir = os.path.join(game_dir, "Ship")
                if os.path.isdir(ship_dir):
                    os.chdir(ship_dir)

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf), \
                     contextlib.redirect_stderr(buf):
                    mod_merger.merge_all(r2_dir, game_dir=game_dir)
                output = buf.getvalue()
                if output:
                    self._ui(lambda: self._merge_status.config(
                        text="Merge complete!", foreground="#070"))
                else:
                    self._ui(lambda: self._merge_status.config(
                        text="Done (nothing to merge)", foreground="#555"))
            except Exception as e:
                self._ui(lambda: self._merge_status.config(
                    text=f"Merge failed: {e}", foreground="#a00"))
            finally:
                os.chdir(orig_cwd)
            self._ui(self._mods_refresh)

        threading.Thread(target=worker, daemon=True).start()

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
        # Update Create tab character dropdown
        if hasattr(self, "_create_char_combo"):
            self._create_char_combo["values"] = self._all_names

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
