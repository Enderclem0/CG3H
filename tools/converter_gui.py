"""
Hades II Model Converter — GUI

Three tabs:
  Export  — pick character(s) from the game list -> export .glb files
  Import  — pick character + a Blender .glb -> produce a patched .gpk
  Install — backup originals and install modded .gpk files into the game

Requires: numpy, pygltflib, lz4  (pip install numpy pygltflib lz4)
"""

import glob
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

# ── Constants ─────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EXPORTER   = os.path.join(SCRIPT_DIR, "gr2_to_gltf.py")
IMPORTER   = os.path.join(SCRIPT_DIR, "gltf_to_gr2.py")

DEFAULT_OUTPUT = os.path.join(os.path.expanduser("~"), "Documents", "Hades2Mods")

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


# ── App ───────────────────────────────────────────────────────────────────────

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Hades II Model Converter")
        self.root.minsize(1000, 700)

        self.game_path = tk.StringVar(value=find_game_path())
        self._all_names: list[str] = []
        self._status = tk.StringVar(value="Ready")

        self._exp_running = False
        self._imp_running = False

        self._build_ui()
        self.game_path.trace_add("write", lambda *_: self._scan())
        self._scan()

    # ── Paths ─────────────────────────────────────────────────────────────────

    def _gpk_dir(self):
        return os.path.join(self.game_path.get(), "Content", "GR2", "_Optimized")

    def _dll_path(self):
        return os.path.join(self.game_path.get(), "Ship", "granny2_x64.dll")

    def _backup_dir(self):
        return os.path.join(self.game_path.get(), "Content", "GR2", "_Optimized", "_backups")

    # ── Top-level UI ──────────────────────────────────────────────────────────

    def _build_ui(self):
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(fill=tk.X)
        ttk.Label(bar, text="Game directory:").pack(side=tk.LEFT)
        ttk.Entry(bar, textvariable=self.game_path, width=64).pack(
            side=tk.LEFT, padx=6, fill=tk.X, expand=True
        )
        ttk.Button(bar, text="Browse\u2026", command=self._browse_game).pack(side=tk.LEFT)
        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(fill=tk.X)

        self._nb = ttk.Notebook(self.root)
        self._nb.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        self._build_export_tab()
        self._build_import_tab()
        self._build_install_tab()

        ttk.Label(
            self.root, textvariable=self._status,
            relief=tk.SUNKEN, anchor=tk.W, padding=(6, 2),
        ).pack(fill=tk.X, side=tk.BOTTOM)

    # ── Export tab ────────────────────────────────────────────────────────────

    def _build_export_tab(self):
        tab = ttk.Frame(self._nb, padding=4)
        self._nb.add(tab, text="  Export  GR2 \u2192 GLB  ")

        paned = ttk.PanedWindow(tab, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(paned, padding=4)
        paned.add(left, weight=1)
        self._exp_lb, self._exp_filter, self._exp_visible = \
            self._make_file_list(left, multi=True, on_select=self._exp_update_btn)

        brow = ttk.Frame(left)
        brow.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(brow, text="Select all",
                   command=lambda: (self._exp_lb.select_set(0, tk.END),
                                    self._exp_update_btn())).pack(side=tk.LEFT)
        ttk.Button(brow, text="Clear",
                   command=lambda: (self._exp_lb.select_clear(0, tk.END),
                                    self._exp_update_btn())).pack(side=tk.LEFT, padx=4)
        self._exp_count = ttk.Label(brow, text="")
        self._exp_count.pack(side=tk.RIGHT)

        right = ttk.Frame(paned, padding=4)
        paned.add(right, weight=2)

        box = ttk.LabelFrame(right, text="Export options", padding=10)
        box.pack(fill=tk.X)

        orow = ttk.Frame(box)
        orow.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(orow, text="Output directory:").pack(side=tk.LEFT)
        self.exp_output = tk.StringVar(value=os.path.join(DEFAULT_OUTPUT, "exports"))
        ttk.Entry(orow, textvariable=self.exp_output, width=40).pack(
            side=tk.LEFT, padx=6, fill=tk.X, expand=True
        )
        ttk.Button(orow, text="Browse\u2026",
                   command=lambda: self._browse_dir(self.exp_output)).pack(side=tk.LEFT)

        self.exp_animations = tk.BooleanVar(value=False)
        self.exp_anim_filter = tk.StringVar()
        self.exp_debug_scan = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Include animations (slow \u2014 can take several minutes per model)",
                        variable=self.exp_animations).pack(anchor=tk.W, pady=2)
        anim_row = ttk.Frame(box)
        anim_row.pack(fill=tk.X, pady=(0, 4), padx=(20, 0))
        ttk.Label(anim_row, text="Filter:", foreground="#555").pack(side=tk.LEFT)
        ttk.Entry(anim_row, textvariable=self.exp_anim_filter, width=24).pack(
            side=tk.LEFT, padx=4)
        ttk.Label(anim_row, text="e.g. Idle, Attack, NoWeapon  (blank = all)",
                  foreground="#888", font=("", 8)).pack(side=tk.LEFT)
        self.exp_textures = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Include textures (embeds PNG in GLB + saves original DDS)",
                        variable=self.exp_textures).pack(anchor=tk.W, pady=2)
        ttk.Checkbutton(box, text="Debug scan (print BoneBinding trace in log)",
                        variable=self.exp_debug_scan).pack(anchor=tk.W, pady=2)

        self._exp_btn = ttk.Button(box, text="Export selected (0)",
                                   command=self._export, state=tk.DISABLED)
        self._exp_btn.pack(pady=(8, 4))
        self._exp_progress = ttk.Progressbar(box, mode="determinate")
        self._exp_progress.pack(fill=tk.X)

        self._exp_log = self._make_log(right)

    # ── Import tab ────────────────────────────────────────────────────────────

    def _build_import_tab(self):
        tab = ttk.Frame(self._nb, padding=4)
        self._nb.add(tab, text="  Import  GLB \u2192 GPK  ")

        paned = ttk.PanedWindow(tab, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(paned, padding=4)
        paned.add(left, weight=1)
        ttk.Label(left, text="Character to patch", font=("", 10, "bold")).pack(anchor=tk.W)
        ttk.Label(left, text="(select the original you want to replace)",
                  foreground="#888").pack(anchor=tk.W)
        _, self._imp_filter, self._imp_visible = \
            self._make_file_list(left, multi=False,
                                 on_select=self._imp_update_btn,
                                 lb_attr="_imp_lb",
                                 filter_attr="_imp_filter")
        self._imp_count = ttk.Label(left, text="")
        self._imp_count.pack(anchor=tk.E)

        right = ttk.Frame(paned, padding=4)
        paned.add(right, weight=2)

        box = ttk.LabelFrame(right, text="Import options", padding=10)
        box.pack(fill=tk.X)

        # GLB file
        row = ttk.Frame(box)
        row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(row, text="Modified .glb:", width=16, anchor=tk.W).pack(side=tk.LEFT)
        self.imp_glb = tk.StringVar()
        ttk.Entry(row, textvariable=self.imp_glb, width=38).pack(
            side=tk.LEFT, padx=6, fill=tk.X, expand=True
        )
        ttk.Button(row, text="Browse\u2026", command=self._browse_glb).pack(side=tk.LEFT)

        # Output directory
        row2 = ttk.Frame(box)
        row2.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(row2, text="Output directory:", width=16, anchor=tk.W).pack(side=tk.LEFT)
        self.imp_output = tk.StringVar(value=DEFAULT_OUTPUT)
        ttk.Entry(row2, textvariable=self.imp_output, width=38).pack(
            side=tk.LEFT, padx=6, fill=tk.X, expand=True
        )
        ttk.Button(row2, text="Browse\u2026",
                   command=lambda: self._browse_dir(self.imp_output)).pack(side=tk.LEFT)

        # Entry name override
        row3 = ttk.Frame(box)
        row3.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(row3, text="GPK entry name:", width=16, anchor=tk.W).pack(side=tk.LEFT)
        self.imp_entry_name = tk.StringVar()
        ttk.Entry(row3, textvariable=self.imp_entry_name, width=28).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Label(row3, text="(leave blank for auto-detect)",
                  foreground="#888").pack(side=tk.LEFT)

        # Checkboxes
        self.imp_positional  = tk.BooleanVar(value=False)
        self.imp_topology    = tk.BooleanVar(value=False)
        self.imp_patch_anims = tk.BooleanVar(value=False)
        self.imp_save_gr2    = tk.BooleanVar(value=False)

        ttk.Checkbutton(
            box,
            text="Allow topology changes \u2014 subdivide, decimate, sculpt (EXPERIMENTAL)",
            variable=self.imp_topology,
        ).pack(anchor=tk.W, pady=(2, 0))
        ttk.Label(box, text="    Enables different vertex/triangle counts between GLB and GR2.\n"
                  "    Without this, vertex count must match exactly.",
                  foreground="#888", font=("", 8)).pack(anchor=tk.W, pady=(0, 6))

        ttk.Checkbutton(
            box,
            text="Patch animations \u2014 import modified animation data from GLB",
            variable=self.imp_patch_anims,
        ).pack(anchor=tk.W, pady=(2, 0))
        anim_patch_row = ttk.Frame(box)
        anim_patch_row.pack(fill=tk.X, pady=(0, 6), padx=(20, 0))
        ttk.Label(anim_patch_row, text="Filter:", foreground="#555").pack(side=tk.LEFT)
        self.imp_anim_filter = tk.StringVar()
        ttk.Entry(anim_patch_row, textvariable=self.imp_anim_filter, width=28).pack(
            side=tk.LEFT, padx=4)
        ttk.Label(anim_patch_row, text="required \u2014 e.g. NoWeapon_Base_Idle_00",
                  foreground="#888", font=("", 8)).pack(side=tk.LEFT)

        ttk.Checkbutton(
            box,
            text="Positional matching \u2014 pair meshes by index instead of name",
            variable=self.imp_positional,
        ).pack(anchor=tk.W, pady=(2, 0))
        ttk.Label(box, text="    Use when GLB and GPK have different variant names "
                  "(e.g. Melinoe vs MelinoeOverlook)",
                  foreground="#888", font=("", 8)).pack(anchor=tk.W, pady=(0, 6))
        ttk.Checkbutton(box, text="Also save raw .gr2 alongside the output .gpk",
                        variable=self.imp_save_gr2).pack(anchor=tk.W, pady=2)

        self._imp_btn = ttk.Button(box, text="Import into selected character",
                                   command=self._import, state=tk.DISABLED)
        self._imp_btn.pack(pady=(8, 4))
        self._imp_progress = ttk.Progressbar(box, mode="indeterminate")
        self._imp_progress.pack(fill=tk.X)

        self._imp_log = self._make_log(right)

    # ── Install tab ───────────────────────────────────────────────────────────

    def _build_install_tab(self):
        tab = ttk.Frame(self._nb, padding=8)
        self._nb.add(tab, text="  Install / Restore  ")

        top = ttk.LabelFrame(tab, text="Install mod from export folder", padding=12)
        top.pack(fill=tk.X)

        ttk.Label(top, text=(
            "Point to a character's export folder (with manifest.json).\n"
            "The tool will import the edited GLB as a .gpk and install\n"
            "modified textures (DDS or PNG) in one step."
        ), foreground="#555").pack(anchor=tk.W, pady=(0, 8))

        # Export folder picker
        row = ttk.Frame(top)
        row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(row, text="Export folder:", width=14, anchor=tk.W).pack(side=tk.LEFT)
        self.inst_export_dir = tk.StringVar()
        ttk.Entry(row, textvariable=self.inst_export_dir, width=42).pack(
            side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        ttk.Button(row, text="Browse\u2026",
                   command=self._browse_install_dir).pack(side=tk.LEFT)

        self._inst_info = ttk.Label(top, text="", foreground="#555")
        self._inst_info.pack(anchor=tk.W, pady=(2, 4))

        # Options
        opt_frame = ttk.Frame(top)
        opt_frame.pack(fill=tk.X, pady=(0, 4))
        self.inst_mesh = tk.BooleanVar(value=True)
        self.inst_textures = tk.BooleanVar(value=True)
        self.inst_topology = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_frame, text="Install mesh (.gpk)",
                        variable=self.inst_mesh).pack(anchor=tk.W)
        ttk.Checkbutton(opt_frame, text="Install textures (DDS/PNG)",
                        variable=self.inst_textures).pack(anchor=tk.W)
        ttk.Checkbutton(opt_frame, text="Allow topology change (vertex count mismatch)",
                        variable=self.inst_topology).pack(anchor=tk.W)

        btn_row = ttk.Frame(top)
        btn_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(btn_row, text="Install mod",
                   command=self._install_from_folder).pack(side=tk.LEFT)
        self._inst_status = ttk.Label(btn_row, text="", foreground="#070")
        self._inst_status.pack(side=tk.LEFT, padx=12)

        self._inst_log = self._make_log(top)

        # ── Installed mods section ──
        ttk.Separator(tab, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=12)

        bot = ttk.LabelFrame(tab, text="Installed mods", padding=12)
        bot.pack(fill=tk.BOTH, expand=True)

        ttk.Label(bot, text=(
            "Each installed mod tracks all changes (mesh + textures).\n"
            "Uninstall restores all original files at once."
        ), foreground="#555").pack(anchor=tk.W, pady=(0, 6))

        list_frame = ttk.Frame(bot)
        list_frame.pack(fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL)
        self._restore_lb = tk.Listbox(
            list_frame, selectmode=tk.EXTENDED, yscrollcommand=sb.set,
            exportselection=False, font=("Consolas", 9),
        )
        sb.config(command=self._restore_lb.yview)
        self._restore_lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        rbtns = ttk.Frame(bot)
        rbtns.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(rbtns, text="Refresh", command=self._refresh_backups).pack(side=tk.LEFT)
        ttk.Button(rbtns, text="Uninstall selected", command=self._restore_selected).pack(
            side=tk.LEFT, padx=8)
        self._restore_status = ttk.Label(rbtns, text="", foreground="#070")
        self._restore_status.pack(side=tk.LEFT, padx=8)

        # Auto-refresh when tab is shown
        self._nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    # ── Shared widget factories ───────────────────────────────────────────────

    def _make_file_list(self, parent, multi, on_select,
                        lb_attr="_exp_lb", filter_attr="_exp_filter"):
        ttk.Label(parent, text="Available models",
                  font=("", 10, "bold")).pack(anchor=tk.W)

        frow = ttk.Frame(parent)
        frow.pack(fill=tk.X, pady=(4, 2))
        ttk.Label(frow, text="Filter:").pack(side=tk.LEFT)

        filter_var = tk.StringVar()
        visible: list[str] = []

        ttk.Entry(frow, textvariable=filter_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=4
        )

        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL)
        lb = tk.Listbox(
            frame,
            selectmode=tk.EXTENDED if multi else tk.BROWSE,
            yscrollcommand=sb.set,
            exportselection=False,
            activestyle="none",
            font=("Consolas", 9),
        )
        sb.config(command=lb.yview)
        lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        lb.bind("<<ListboxSelect>>", lambda _: on_select())

        setattr(self, lb_attr, lb)
        setattr(self, filter_attr, filter_var)

        filter_var.trace_add("write", lambda *_: self._refresh_list(
            filter_var, lb, visible, on_select,
            getattr(self, "_exp_count", None) if lb_attr == "_exp_lb" else self._imp_count
        ))

        return lb, filter_var, visible

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

    # ── Game scanning ─────────────────────────────────────────────────────────

    def _scan(self):
        gpk_dir = self._gpk_dir()
        if not os.path.isdir(gpk_dir):
            self._all_names = []
            self._status.set("GPK directory not found \u2014 check game path.")
        else:
            self._all_names = sorted(
                os.path.splitext(os.path.basename(f))[0]
                for f in glob.glob(os.path.join(gpk_dir, "*.gpk"))
            )
            self._status.set(
                f"Found {len(self._all_names)} models in \u2026/Content/GR2/_Optimized/"
            )
        self._refresh_list(self._exp_filter, self._exp_lb,
                           self._exp_visible if hasattr(self, '_exp_visible') else [],
                           self._exp_update_btn, self._exp_count)
        self._refresh_list(self._imp_filter, self._imp_lb,
                           self._imp_visible if hasattr(self, '_imp_visible') else [],
                           self._imp_update_btn, self._imp_count)

    def _refresh_list(self, filter_var, lb, visible_list, on_select, count_lbl):
        q = filter_var.get().lower()
        filtered = [n for n in self._all_names if q in n.lower()]
        visible_list.clear()
        visible_list.extend(filtered)
        lb.delete(0, tk.END)
        for name in filtered:
            lb.insert(tk.END, name)
        if count_lbl:
            count_lbl.config(text=f"{len(self._all_names)} files")
        on_select()

    # ── Selection helpers ─────────────────────────────────────────────────────

    def _exp_selected(self) -> list[str]:
        return [self._exp_visible[i] for i in self._exp_lb.curselection()]

    def _imp_selected(self):
        sel = self._imp_lb.curselection()
        return self._imp_visible[sel[0]] if sel else None

    def _exp_update_btn(self):
        n = len(self._exp_lb.curselection())
        self._exp_btn.config(
            text=f"Export selected ({n})",
            state=tk.NORMAL if n > 0 and not self._exp_running else tk.DISABLED,
        )

    def _imp_update_btn(self):
        sel = self._imp_lb.curselection()
        self._imp_btn.config(
            state=tk.NORMAL if sel and not self._imp_running else tk.DISABLED,
        )

    # ── Browse dialogs ────────────────────────────────────────────────────────

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

    def _browse_glb(self):
        p = filedialog.askopenfilename(
            title="Select modified .glb file",
            filetypes=[("glTF Binary", "*.glb"), ("All files", "*.*")],
        )
        if p:
            self.imp_glb.set(p)

    def _browse_mod_gpk(self):
        p = filedialog.askopenfilename(
            title="Select modded .gpk file",
            initialdir=DEFAULT_OUTPUT,
            filetypes=[("GPK Archives", "*.gpk"), ("All files", "*.*")],
        )
        if p:
            self.inst_gpk.set(p)
            # Auto-detect character name: strip _mod suffix
            base = os.path.splitext(os.path.basename(p))[0]
            for suffix in ("_mod", "_patched", "_modded"):
                if base.endswith(suffix):
                    base = base[:-len(suffix)]
                    break
            self.inst_character.set(base)

    def _browse_install_dir(self):
        p = filedialog.askdirectory(title="Select character export folder (with manifest.json)")
        if p:
            self.inst_export_dir.set(p)
            self._scan_install_dir(p)

    def _scan_install_dir(self, p):
        """Read manifest and show summary of what can be installed."""
        manifest_path = os.path.join(p, 'manifest.json')
        if not os.path.isfile(manifest_path):
            self._inst_info.config(text="No manifest.json found", foreground="#a00")
            return
        import json
        with open(manifest_path) as f:
            m = json.load(f)
        parts = []
        character = m.get('character', '?')
        # Check for GLB
        glb = m.get('glb', '')
        glb_path = os.path.join(p, glb)
        if glb and os.path.isfile(glb_path):
            meshes = m.get('meshes', [])
            parts.append(f"mesh ({len(meshes)} parts)")
        # Check for textures
        textures = m.get('textures', {})
        tex_ready = 0
        for tn, ti in textures.items():
            dds = os.path.join(p, ti.get('dds_file', f'{tn}.dds'))
            png = os.path.join(p, f'{tn}.png')
            if os.path.isfile(dds) or os.path.isfile(png):
                tex_ready += 1
        if tex_ready:
            parts.append(f"{tex_ready} texture(s)")
        # Check for animations
        anims = m.get('animations', {})
        if anims.get('count', 0):
            parts.append(f"{anims['count']} animations")
        summary = ', '.join(parts) if parts else 'nothing found'
        self._inst_info.config(text=f"{character}: {summary}", foreground="#555")

    # _replace_texture removed — integrated into _install_from_folder

    # ── Export logic ──────────────────────────────────────────────────────────

    def _export(self):
        names = self._exp_selected()
        if not names:
            return

        out_dir = self.exp_output.get().strip()
        if not out_dir:
            out_dir = filedialog.askdirectory(title="Select output directory")
            if not out_dir:
                return
            self.exp_output.set(out_dir)

        dll = self._dll_path()
        if not os.path.isfile(dll):
            messagebox.showerror(
                "DLL not found",
                f"granny2_x64.dll not found at:\n{dll}\n\nCheck the game directory.",
            )
            return

        os.makedirs(out_dir, exist_ok=True)
        self._log_clear(self._exp_log)
        self._exp_running = True
        self._exp_update_btn()
        self._exp_progress["maximum"] = len(names)
        self._exp_progress["value"]   = 0

        threading.Thread(
            target=self._export_worker,
            args=(names, out_dir, dll),
            daemon=True,
        ).start()

    def _export_worker(self, names, out_dir, dll):
        gpk_dir = self._gpk_dir()
        ok = errors = 0
        max_workers = min(os.cpu_count() or 8, len(names))

        # Pre-build texture index for fast parallel lookups
        if self.exp_textures.get() and len(names) > 1:
            try:
                content_dir = os.path.dirname(os.path.dirname(gpk_dir))
                pkg_dir = os.path.join(content_dir, "Packages", "1080p")
                idx_path = os.path.join(pkg_dir, '_texture_index.json')
                if os.path.isdir(pkg_dir) and not os.path.isfile(idx_path):
                    self._log_write_ui(self._exp_log,
                                       "  Building texture index (one-time)...\n")
                    from pkg_texture import save_texture_index
                    save_texture_index(pkg_dir)
                    self._log_write_ui(self._exp_log, "  Texture index built!\n")
            except Exception as e:
                self._log_write_ui(self._exp_log,
                                   f"  Texture index build failed: {e}\n")

        cpus = os.cpu_count() or 4
        has_anims = self.exp_animations.get()

        # When animations are enabled, limit outer concurrency so each
        # subprocess can use multiple cores for animation decoding.
        # Non-animation characters finish in seconds and free their slots,
        # so the heavy animation ones naturally absorb the freed CPU.
        if has_anims and len(names) > 1:
            max_workers = min(max_workers, max(2, cpus // 4))
        anim_w = 0  # auto — each subprocess decides based on available cores

        # Build command list for all exports
        def _build_cmd(name):
            # Each character gets its own subdirectory
            char_dir = os.path.join(out_dir, name)
            os.makedirs(char_dir, exist_ok=True)
            cmd = [
                sys.executable, EXPORTER, name,
                "--gpk-dir", gpk_dir,
                "--dll",     dll,
                "-o",        os.path.join(char_dir, f"{name}.glb"),
            ]
            if self.exp_animations.get():
                cmd.append("--animations")
                anim_filter = self.exp_anim_filter.get().strip()
                if anim_filter:
                    cmd += ["--anim-filter", anim_filter]
                cmd += ["--anim-workers", str(anim_w)]
            if self.exp_textures.get():
                cmd.append("--textures")
            if self.exp_debug_scan.get():
                cmd.append("--debug")
            return cmd

        if max_workers <= 1:
            # Single-character or single-core: sequential (preserves log order)
            for i, name in enumerate(names):
                self._ui(lambda n=name, t=len(names), idx=i:
                    self._status.set(f"Exporting {n}  ({idx+1}/{t})\u2026"))
                self._log_write_ui(
                    self._exp_log,
                    f"\n{'\u2500'*52}\n  [{i+1}/{len(names)}]  {name}\n{'\u2500'*52}\n",
                )
                try:
                    proc = subprocess.Popen(
                        _build_cmd(name), stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, bufsize=1,
                        encoding='utf-8', errors='replace',
                    )
                    for line in proc.stdout:
                        self._log_write_ui(self._exp_log, line)
                    proc.wait()
                    if proc.returncode == 0:
                        ok += 1
                    else:
                        errors += 1
                        self._log_write_ui(self._exp_log,
                                           f"  exited with code {proc.returncode}\n")
                except Exception as exc:
                    errors += 1
                    self._log_write_ui(self._exp_log, f"  ERROR: {exc}\n")
                self._ui(lambda v=i+1: self._exp_progress.config(value=v))
        else:
            # Parallel export: run up to max_workers subprocesses concurrently
            self._log_write_ui(
                self._exp_log,
                f"  Parallel export: {max_workers} workers for {len(names)} characters\n",
            )
            self._ui(lambda: self._status.set(
                f"Exporting {len(names)} characters ({max_workers} parallel)\u2026"))

            import queue as _queue

            active = {}       # name → Popen
            readers = {}      # name → Thread
            output_q = {}     # name → Queue (non-blocking line collection)
            pending = list(names)
            done_count = 0

            def _reader_thread(proc, q):
                """Drain stdout in a background thread so polling never blocks."""
                try:
                    for line in proc.stdout:
                        q.put(line)
                except Exception:
                    pass

            def _launch(name):
                proc = subprocess.Popen(
                    _build_cmd(name), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                    encoding='utf-8', errors='replace',
                )
                active[name] = proc
                q = _queue.Queue()
                output_q[name] = q
                t = threading.Thread(target=_reader_thread, args=(proc, q), daemon=True)
                t.start()
                readers[name] = t

            while pending or active:
                # Launch up to max_workers
                while pending and len(active) < max_workers:
                    name = pending.pop(0)
                    try:
                        _launch(name)
                    except Exception as exc:
                        errors += 1
                        done_count += 1
                        self._log_write_ui(
                            self._exp_log,
                            f"\n{'\u2500'*52}\n  [{done_count}/{len(names)}]  "
                            f"{name}\n{'\u2500'*52}\n  ERROR: {exc}\n",
                        )
                        self._ui(lambda v=done_count: self._exp_progress.config(value=v))

                # Poll active processes (non-blocking)
                finished = []
                for name, proc in active.items():
                    if proc.poll() is not None:
                        finished.append(name)

                for name in finished:
                    proc = active.pop(name)
                    readers[name].join(timeout=2)
                    # Drain remaining lines
                    lines = []
                    q = output_q.pop(name)
                    while not q.empty():
                        lines.append(q.get_nowait())
                    if proc.returncode == 0:
                        ok += 1
                    else:
                        errors += 1
                        lines.append(f"  exited with code {proc.returncode}\n")
                    done_count += 1
                    self._log_write_ui(
                        self._exp_log,
                        f"\n{'\u2500'*52}\n  [{done_count}/{len(names)}]  "
                        f"{name}\n{'\u2500'*52}\n",
                    )
                    for ln in lines:
                        self._log_write_ui(self._exp_log, ln)
                    self._ui(lambda v=done_count:
                             self._exp_progress.config(value=v))
                    self._ui(lambda n=name, dc=done_count, t=len(names):
                             self._status.set(f"Exported {n}  ({dc}/{t})\u2026"))
                    del readers[name]

                if active:
                    import time
                    time.sleep(0.05)

        self._ui(lambda: self._exp_finish(ok, errors, out_dir))

    def _exp_finish(self, ok, errors, out_dir):
        self._exp_running = False
        self._exp_update_btn()
        self._exp_progress["value"] = 0
        self._status.set(f"Export done \u2014 {ok} exported, {errors} failed.  Output: {out_dir}")
        if errors == 0:
            messagebox.showinfo("Export complete",
                                f"Exported {ok} model(s) to:\n{out_dir}")
        else:
            messagebox.showwarning("Export complete with errors",
                                   f"{ok} succeeded, {errors} failed.\nCheck the log.")

    # ── Import logic ──────────────────────────────────────────────────────────

    def _import(self):
        character = self._imp_selected()
        if not character:
            messagebox.showwarning("No character selected",
                                   "Select a character from the list.")
            return

        glb_path = self.imp_glb.get().strip()
        if not glb_path:
            messagebox.showwarning("No .glb file", "Browse to a .glb file first.")
            return
        if not os.path.isfile(glb_path):
            messagebox.showerror("File not found", f"GLB not found:\n{glb_path}")
            return

        out_dir = self.imp_output.get().strip()
        if not out_dir:
            out_dir = filedialog.askdirectory(title="Select output directory")
            if not out_dir:
                return
            self.imp_output.set(out_dir)

        dll = self._dll_path()
        if not os.path.isfile(dll):
            messagebox.showerror(
                "DLL not found",
                f"granny2_x64.dll not found at:\n{dll}\n\nCheck the game directory.",
            )
            return

        gpk_dir = self._gpk_dir()
        gpk_path = os.path.join(gpk_dir, f"{character}.gpk")
        sdb_path = os.path.join(gpk_dir, f"{character}.sdb")
        for p, label in [(gpk_path, ".gpk"), (sdb_path, ".sdb")]:
            if not os.path.isfile(p):
                messagebox.showerror("File not found",
                                     f"Could not find {label} for {character!r}:\n{p}")
                return

        os.makedirs(out_dir, exist_ok=True)
        self._log_clear(self._imp_log)
        self._imp_running = True
        self._imp_update_btn()
        self._imp_progress.start(12)

        threading.Thread(
            target=self._import_worker,
            args=(character, glb_path, gpk_path, sdb_path, dll, out_dir),
            daemon=True,
        ).start()

    def _import_worker(self, character, glb_path, gpk_path, sdb_path, dll, out_dir):
        self._ui(lambda: self._status.set(f"Importing {character} \u2026"))

        out_gpk = os.path.join(out_dir, f"{character}_mod.gpk")

        cmd = [
            sys.executable, IMPORTER, glb_path,
            "--gpk", gpk_path,
            "--sdb", sdb_path,
            "--dll", dll,
            "--output-gpk", out_gpk,
            "--strict",
        ]
        entry_name = self.imp_entry_name.get().strip()
        if entry_name:
            cmd += ["--entry-name", entry_name]
        if self.imp_positional.get():
            cmd.append("--positional")
        if self.imp_topology.get():
            cmd.append("--allow-topology-change")
        if self.imp_patch_anims.get():
            cmd.append("--patch-animations")
            anim_filter = self.imp_anim_filter.get().strip()
            if anim_filter:
                cmd += ["--anim-patch-filter", anim_filter]
        if self.imp_save_gr2.get():
            cmd += ["--output-gr2", os.path.join(out_dir, f"{character}_mod.gr2")]

        ok = False
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding='utf-8', errors='replace',
            )
            for line in proc.stdout:
                self._log_write_ui(self._imp_log, line)
            proc.wait()
            ok = proc.returncode == 0
            if not ok:
                self._log_write_ui(self._imp_log,
                                   f"\n  exited with code {proc.returncode}\n")
        except Exception as exc:
            self._log_write_ui(self._imp_log, f"  ERROR: {exc}\n")

        self._ui(lambda: self._imp_finish(ok, character, out_gpk))

    def _imp_finish(self, ok, character, out_gpk):
        self._imp_running = False
        self._imp_update_btn()
        self._imp_progress.stop()
        if ok:
            self._status.set(f"Import done \u2014 {out_gpk}")
            messagebox.showinfo(
                "Import complete",
                f"Patched GPK written to:\n{out_gpk}\n\n"
                "Go to the Install tab to install it into the game.",
            )
        else:
            self._status.set("Import failed \u2014 check the log for details")
            messagebox.showwarning(
                "Import failed",
                "The import did not complete successfully.\n"
                "Check the output log for details.",
            )

    # ── Install logic ─────────────────────────────────────────────────────────

    def _on_tab_changed(self, event):
        if self._nb.index("current") == 2:
            self._refresh_backups()

    def _install_from_folder(self):
        export_dir = self.inst_export_dir.get().strip()
        if not export_dir or not os.path.isdir(export_dir):
            messagebox.showwarning("No folder", "Select an export folder first.")
            return

        manifest_path = os.path.join(export_dir, 'manifest.json')
        if not os.path.isfile(manifest_path):
            messagebox.showerror("No manifest", f"manifest.json not found in:\n{export_dir}")
            return

        import json
        with open(manifest_path) as f:
            manifest = json.load(f)

        character = manifest.get('character', '')
        if not character:
            messagebox.showerror("Bad manifest", "No 'character' field in manifest.")
            return

        self._log_clear(self._inst_log)
        self._inst_status.config(text=f"Installing {character}...", foreground="#555")

        threading.Thread(
            target=self._install_worker,
            args=(export_dir, manifest, character),
            daemon=True,
        ).start()

    def _install_worker(self, export_dir, manifest, character):
        game = self.game_path.get()
        gpk_dir = self._gpk_dir()
        pkg_dir = os.path.join(game, "Content", "Packages", "1080p")
        dll = self._dll_path()
        results = []
        errors = []
        modified_files = []  # for mod registry

        # ── Step 1: Import mesh (GLB → GPK) ──
        if self.inst_mesh.get():
            glb = manifest.get('glb', '')
            glb_path = os.path.join(export_dir, glb)
            if glb and os.path.isfile(glb_path):
                self._ui(lambda: self._inst_status.config(
                    text=f"Importing {character} mesh...", foreground="#555"))

                gpk_out = os.path.join(export_dir, f"{character}_mod.gpk")
                gpk_orig = os.path.join(gpk_dir, f"{character}.gpk")
                sdb_orig = os.path.join(gpk_dir, f"{character}.sdb")
                if not os.path.isfile(gpk_orig):
                    errors.append(f"Original {character}.gpk not found")
                elif not os.path.isfile(sdb_orig):
                    errors.append(f"Original {character}.sdb not found")
                else:
                    cmd = [
                        sys.executable, IMPORTER, glb_path,
                        "--gpk", gpk_orig,
                        "--sdb", sdb_orig,
                        "--dll", dll,
                        "--output-gpk", gpk_out,
                    ]
                    if self.inst_topology.get():
                        cmd.append("--allow-topology-change")

                    try:
                        proc = subprocess.Popen(
                            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, encoding='utf-8', errors='replace',
                        )
                        output = proc.communicate()[0]
                        self._log_write_ui(self._inst_log, output)
                        if proc.returncode == 0:
                            # Install the GPK
                            target = os.path.join(gpk_dir, f"{character}.gpk")
                            backup_dir = self._backup_dir()
                            os.makedirs(backup_dir, exist_ok=True)
                            backup_path = os.path.join(backup_dir, f"{character}.gpk")
                            if not os.path.isfile(backup_path):
                                shutil.copy2(target, backup_path)
                            shutil.copy2(gpk_out, target)
                            modified_files.append({
                                'type': 'gpk', 'backup': backup_path, 'target': target,
                            })
                            results.append(f"Mesh installed ({character}.gpk)")
                        else:
                            errors.append(f"Mesh import failed (exit code {proc.returncode})")
                    except Exception as e:
                        errors.append(f"Mesh import error: {e}")
            else:
                self._log_write_ui(self._inst_log,
                                   f"  No GLB found ({glb}), skipping mesh\n")

        # ── Step 2: Install textures ──
        if self.inst_textures.get():
            textures = manifest.get('textures', {})
            if textures:
                self._ui(lambda: self._inst_status.config(
                    text=f"Installing {character} textures...", foreground="#555"))

                from pkg_texture import replace_texture, png_to_dds
                backup_dir = os.path.join(pkg_dir, "_backups")
                os.makedirs(backup_dir, exist_ok=True)

                # Backup checksums.txt (once)
                checksums_src = os.path.join(pkg_dir, "checksums.txt")
                checksums_bak = os.path.join(backup_dir, "checksums.txt")
                if os.path.isfile(checksums_src) and not os.path.isfile(checksums_bak):
                    shutil.copy2(checksums_src, checksums_bak)

                for tex_name, tex_info in textures.items():
                    dds_file = tex_info.get('dds_file', f"{tex_name}.dds")
                    dds_path = os.path.join(export_dir, dds_file)
                    png_path = os.path.join(export_dir, f"{tex_name}.png")

                    # PNG path: compress if newer than DDS
                    if os.path.isfile(png_path):
                        if not os.path.isfile(dds_path) or \
                                os.path.getmtime(png_path) > os.path.getmtime(dds_path):
                            self._log_write_ui(
                                self._inst_log,
                                f"  Compressing {tex_name}.png -> DDS...\n")
                            try:
                                dds_bytes = png_to_dds(
                                    png_path,
                                    tex_info.get('format', 0x1C),
                                    tex_info.get('width', 512),
                                    tex_info.get('height', 512),
                                    tex_info.get('mip_count', 6),
                                )
                                with open(dds_path, 'wb') as f:
                                    f.write(dds_bytes)
                            except Exception as e:
                                errors.append(f"{tex_name} PNG compress: {e}")
                                continue

                    if not os.path.isfile(dds_path):
                        continue

                    pkg_name = tex_info.get('pkg', '')
                    pkg_path = os.path.join(pkg_dir, pkg_name)
                    if not os.path.isfile(pkg_path):
                        errors.append(f"{tex_name}: {pkg_name} not found")
                        continue

                    # Backup pkg
                    backup_path = os.path.join(backup_dir, pkg_name)
                    if not os.path.isfile(backup_path):
                        shutil.copy2(pkg_path, backup_path)

                    try:
                        entry_name = tex_info.get('pkg_entry_name', tex_name)
                        if replace_texture(pkg_path, entry_name, dds_path, pkg_path):
                            modified_files.append({
                                'type': 'pkg', 'backup': backup_path, 'target': pkg_path,
                                'texture': tex_name, 'entry_name': entry_name,
                                'dds_path': dds_path,
                            })
                            results.append(f"Texture: {tex_name} -> {pkg_name}")
                        else:
                            errors.append(f"{tex_name}: not found in {pkg_name}")
                    except Exception as e:
                        errors.append(f"{tex_name}: {e}")

        # ── Register mod and summarize ──
        if modified_files:
            self._register_mod(character, modified_files)
        self._ui(self._refresh_backups)
        if errors:
            self._ui(lambda: self._inst_status.config(
                text=f"{len(results)} ok, {len(errors)} failed", foreground="#a00"))
            detail = "\n".join(results + [""] + errors)
            self._log_write_ui(self._inst_log, f"\n{detail}\n")
            self._ui(lambda: messagebox.showwarning(
                "Install complete with errors",
                f"{len(results)} succeeded, {len(errors)} failed.\n\n"
                + "\n".join(errors[:5])))
        elif results:
            self._ui(lambda: self._inst_status.config(
                text=f"Installed! ({len(results)} items)", foreground="#070"))
            self._log_write_ui(self._inst_log,
                               "\n".join(["", "Installed:"] + results + [""]))
            self._ui(lambda: messagebox.showinfo(
                "Mod installed",
                f"Installed {len(results)} item(s) for {character}.\n\n"
                + "\n".join(results) +
                "\n\nLaunch the game to see your changes."))
        else:
            self._ui(lambda: self._inst_status.config(
                text="Nothing to install", foreground="#555"))

    def _pkg_backup_dir(self):
        return os.path.join(self.game_path.get(), "Content", "Packages", "1080p", "_backups")

    def _mods_registry_path(self):
        return os.path.join(self._backup_dir(), "_mods.json")

    def _load_mods_registry(self):
        import json
        p = self._mods_registry_path()
        if os.path.isfile(p):
            with open(p) as f:
                return json.load(f)
        return {}

    def _save_mods_registry(self, registry):
        import json
        os.makedirs(os.path.dirname(self._mods_registry_path()), exist_ok=True)
        with open(self._mods_registry_path(), 'w') as f:
            json.dump(registry, f, indent=2)

    def _register_mod(self, character, modified_files):
        """Record an installed mod. modified_files: list of {type, src_backup, dst_game}"""
        registry = self._load_mods_registry()
        registry[character] = {
            'installed': datetime.now().strftime("%Y-%m-%d %H:%M"),
            'files': modified_files,
        }
        self._save_mods_registry(registry)

    def _unregister_mod(self, character):
        registry = self._load_mods_registry()
        registry.pop(character, None)
        self._save_mods_registry(registry)

    def _refresh_backups(self):
        self._restore_lb.delete(0, tk.END)
        registry = self._load_mods_registry()
        for character, info in sorted(registry.items()):
            ts = info.get('installed', '?')
            files = info.get('files', [])
            parts = []
            if any(f['type'] == 'gpk' for f in files):
                parts.append('mesh')
            tex_count = sum(1 for f in files if f['type'] == 'pkg')
            if tex_count:
                parts.append(f'{tex_count} texture(s)')
            desc = ' + '.join(parts) if parts else 'unknown'
            self._restore_lb.insert(tk.END, f"{character}  ({desc}, installed {ts})")

    def _restore_selected(self):
        sel = self._restore_lb.curselection()
        if not sel:
            messagebox.showwarning("Nothing selected", "Select mod(s) to uninstall.")
            return

        registry = self._load_mods_registry()
        restored = []
        characters_to_remove = []

        for idx in sel:
            text = self._restore_lb.get(idx)
            characters_to_remove.append(text.split("  (")[0].strip())

        # Collect all files to restore and which mods remain
        remaining_mods = {k: v for k, v in registry.items()
                         if k not in characters_to_remove}
        restored_targets = set()

        pkg_restored = False
        for character in characters_to_remove:
            info = registry.get(character)
            if not info:
                continue
            for f in info.get('files', []):
                src = f.get('backup')
                dst = f.get('target')
                if src and dst and os.path.isfile(src):
                    shutil.copy2(src, dst)
                    restored_targets.add(dst)
                    if f.get('type') == 'pkg':
                        pkg_restored = True
            restored.append(character)

        # Restore checksums.txt if any .pkg was restored
        if pkg_restored:
            pkg_backup_dir = self._pkg_backup_dir()
            pkg_dir = os.path.join(self.game_path.get(), "Content", "Packages", "1080p")
            checksums_bak = os.path.join(pkg_backup_dir, "checksums.txt")
            checksums_dst = os.path.join(pkg_dir, "checksums.txt")
            if os.path.isfile(checksums_bak):
                shutil.copy2(checksums_bak, checksums_dst)

        # Re-apply remaining mods that touch any restored file.
        # This prevents uninstalling mod A from breaking mod B when
        # both modify the same .pkg file.
        if remaining_mods and restored_targets:
            from pkg_texture import replace_texture
            reapplied = []
            for mod_char, mod_info in remaining_mods.items():
                for f in mod_info.get('files', []):
                    if f.get('target') in restored_targets and f['type'] == 'pkg':
                        dds_path = f.get('dds_path', '')
                        entry_name = f.get('entry_name', '')
                        pkg_path = f.get('target', '')
                        if dds_path and entry_name and os.path.isfile(dds_path):
                            try:
                                replace_texture(pkg_path, entry_name, dds_path, pkg_path)
                                reapplied.append(f"{mod_char}/{f.get('texture', '?')}")
                            except Exception:
                                pass
            if reapplied:
                self._log_write_ui(self._inst_log,
                                   f"\nRe-applied {len(reapplied)} texture(s) from other mods:\n"
                                   + "\n".join(f"  {r}" for r in reapplied) + "\n")

        for character in characters_to_remove:
            self._unregister_mod(character)

        if restored:
            self._restore_status.config(text=f"Uninstalled {len(restored)} mod(s)")
            self._status.set(f"Uninstalled: {', '.join(restored)}")
            self._refresh_backups()
            messagebox.showinfo(
                "Mods uninstalled",
                f"Restored originals for {len(restored)} mod(s):\n" +
                "\n".join(f"  {n}" for n in restored) +
                "\n\nRestart the game to apply.",
            )

    # ── Thread-safe UI helpers ────────────────────────────────────────────────

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


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
