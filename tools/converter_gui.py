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

        self.exp_all_lods   = tk.BooleanVar(value=False)
        self.exp_animations = tk.BooleanVar(value=False)
        self.exp_anim_filter = tk.StringVar()
        self.exp_debug_scan = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Export all LODs (includes lower-resolution duplicates)",
                        variable=self.exp_all_lods).pack(anchor=tk.W, pady=2)
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

        top = ttk.LabelFrame(tab, text="Install modded .gpk into game", padding=12)
        top.pack(fill=tk.X)

        ttk.Label(top, text=(
            "Select a _mod.gpk file to install. The original will be backed up\n"
            "automatically before replacement. You can restore originals below.\n"
            "Tip: use Steam > Properties > Verify Integrity of Game Files to\n"
            "restore all originals if backups are lost."
        ), foreground="#555").pack(anchor=tk.W, pady=(0, 8))

        # GPK file picker
        row = ttk.Frame(top)
        row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(row, text="Mod .gpk file:", width=14, anchor=tk.W).pack(side=tk.LEFT)
        self.inst_gpk = tk.StringVar()
        ttk.Entry(row, textvariable=self.inst_gpk, width=42).pack(
            side=tk.LEFT, padx=6, fill=tk.X, expand=True
        )
        ttk.Button(row, text="Browse\u2026", command=self._browse_mod_gpk).pack(side=tk.LEFT)

        # Character override
        row2 = ttk.Frame(top)
        row2.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(row2, text="Target character:", width=14, anchor=tk.W).pack(side=tk.LEFT)
        self.inst_character = tk.StringVar()
        ttk.Entry(row2, textvariable=self.inst_character, width=24).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Label(row2, text="(auto-detected from filename if blank)",
                  foreground="#888").pack(side=tk.LEFT)

        btn_row = ttk.Frame(top)
        btn_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(btn_row, text="Install mod", command=self._install_mod).pack(side=tk.LEFT)
        self._inst_status = ttk.Label(btn_row, text="", foreground="#070")
        self._inst_status.pack(side=tk.LEFT, padx=12)

        # ── Texture replacement section ──
        ttk.Separator(tab, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=12)

        tex_box = ttk.LabelFrame(tab, text="Replace texture in Fx.pkg", padding=12)
        tex_box.pack(fill=tk.X)

        ttk.Label(tex_box, text=(
            "Replace a 3D model texture. The DDS must have the same format,\n"
            "dimensions, and mipmap count as the original."
        ), foreground="#555").pack(anchor=tk.W, pady=(0, 8))

        tex_row1 = ttk.Frame(tex_box)
        tex_row1.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(tex_row1, text="DDS file:", width=14, anchor=tk.W).pack(side=tk.LEFT)
        self.tex_dds = tk.StringVar()
        ttk.Entry(tex_row1, textvariable=self.tex_dds, width=42).pack(
            side=tk.LEFT, padx=6, fill=tk.X, expand=True)
        ttk.Button(tex_row1, text="Browse\u2026",
                   command=self._browse_tex_dds).pack(side=tk.LEFT)

        tex_row2 = ttk.Frame(tex_box)
        tex_row2.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(tex_row2, text="Texture name:", width=14, anchor=tk.W).pack(side=tk.LEFT)
        self.tex_name = tk.StringVar()
        ttk.Entry(tex_row2, textvariable=self.tex_name, width=28).pack(
            side=tk.LEFT, padx=6)
        ttk.Label(tex_row2, text="e.g. MelinoeTransform_Color",
                  foreground="#888").pack(side=tk.LEFT)

        tex_btn_row = ttk.Frame(tex_box)
        tex_btn_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(tex_btn_row, text="Replace texture",
                   command=self._replace_texture).pack(side=tk.LEFT)
        self._tex_status = ttk.Label(tex_btn_row, text="", foreground="#070")
        self._tex_status.pack(side=tk.LEFT, padx=12)

        # ── Restore section ──
        ttk.Separator(tab, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=12)

        bot = ttk.LabelFrame(tab, text="Restore originals from backup", padding=12)
        bot.pack(fill=tk.BOTH, expand=True)

        ttk.Label(bot, text=(
            "Backed-up originals are stored in Content/GR2/_Optimized/_backups/.\n"
            "Select one or more to restore."
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
        ttk.Button(rbtns, text="Refresh list", command=self._refresh_backups).pack(side=tk.LEFT)
        ttk.Button(rbtns, text="Restore selected", command=self._restore_selected).pack(
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

    def _browse_tex_dds(self):
        p = filedialog.askopenfilename(
            title="Select modified .dds file",
            filetypes=[("DDS Textures", "*.dds"), ("All files", "*.*")],
        )
        if p:
            self.tex_dds.set(p)

    def _replace_texture(self):
        dds_path = self.tex_dds.get().strip()
        tex_name = self.tex_name.get().strip()
        if not dds_path or not os.path.isfile(dds_path):
            messagebox.showwarning("No DDS file", "Select a .dds file first.")
            return
        if not tex_name:
            messagebox.showwarning("No texture name", "Enter the texture name to replace.")
            return

        game = self.game_path.get()
        fx_pkg = os.path.join(game, "Content", "Packages", "1080p", "Fx.pkg")
        if not os.path.isfile(fx_pkg):
            messagebox.showerror("Fx.pkg not found", f"Not found:\n{fx_pkg}")
            return

        # Backup Fx.pkg
        backup_dir = os.path.join(game, "Content", "Packages", "1080p", "_backups")
        os.makedirs(backup_dir, exist_ok=True)
        backup_path = os.path.join(backup_dir, "Fx.pkg")
        if not os.path.isfile(backup_path):
            self._tex_status.config(text="Backing up Fx.pkg...", foreground="#555")
            self.root.update()
            shutil.copy2(fx_pkg, backup_path)

        self._tex_status.config(text="Replacing texture...", foreground="#555")
        self.root.update()

        try:
            from pkg_texture import replace_texture
            ok = replace_texture(fx_pkg, tex_name, dds_path, fx_pkg)
            if ok:
                self._tex_status.config(text="Texture replaced!", foreground="#070")
                messagebox.showinfo("Texture replaced",
                                    f"Replaced '{tex_name}' in Fx.pkg.\n"
                                    "Launch the game to see your changes.")
            else:
                self._tex_status.config(text="Failed", foreground="#a00")
                messagebox.showwarning("Replace failed",
                                       f"Could not find texture '{tex_name}' in Fx.pkg.")
        except Exception as e:
            self._tex_status.config(text="Error", foreground="#a00")
            messagebox.showerror("Error", str(e))

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

        for i, name in enumerate(names):
            self._ui(lambda n=name, t=len(names), idx=i:
                self._status.set(f"Exporting {n}  ({idx+1}/{t})\u2026"))
            self._log_write_ui(
                self._exp_log,
                f"\n{'\u2500'*52}\n  [{i+1}/{len(names)}]  {name}\n{'\u2500'*52}\n",
            )

            cmd = [
                sys.executable, EXPORTER, name,
                "--gpk-dir", gpk_dir,
                "--dll",     dll,
                "-o",        os.path.join(out_dir, f"{name}.glb"),
            ]
            if self.exp_all_lods.get():
                cmd.append("--all-lods")
            if self.exp_animations.get():
                cmd.append("--animations")
                anim_filter = self.exp_anim_filter.get().strip()
                if anim_filter:
                    cmd += ["--anim-filter", anim_filter]
            if self.exp_textures.get():
                cmd.append("--textures")
            if self.exp_debug_scan.get():
                cmd.append("--debug")

            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
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
                text=True, bufsize=1,
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

    def _install_mod(self):
        gpk_file = self.inst_gpk.get().strip()
        if not gpk_file or not os.path.isfile(gpk_file):
            messagebox.showwarning("No file", "Select a .gpk file to install.")
            return

        character = self.inst_character.get().strip()
        if not character:
            base = os.path.splitext(os.path.basename(gpk_file))[0]
            for suffix in ("_mod", "_patched", "_modded"):
                if base.endswith(suffix):
                    base = base[:-len(suffix)]
                    break
            character = base

        gpk_dir = self._gpk_dir()
        target = os.path.join(gpk_dir, f"{character}.gpk")

        if not os.path.isfile(target):
            messagebox.showerror(
                "Original not found",
                f"No original .gpk found for '{character}':\n{target}\n\n"
                "Check the character name.",
            )
            return

        # Backup original
        backup_dir = self._backup_dir()
        os.makedirs(backup_dir, exist_ok=True)
        backup_path = os.path.join(backup_dir, f"{character}.gpk")
        if not os.path.isfile(backup_path):
            shutil.copy2(target, backup_path)
            msg_backup = f"Backed up original to:\n  {backup_path}\n\n"
        else:
            msg_backup = "Backup already exists (not overwritten).\n\n"

        # Install
        shutil.copy2(gpk_file, target)

        self._inst_status.config(text=f"Installed {character}.gpk")
        self._status.set(f"Installed mod for {character}")
        self._refresh_backups()

        messagebox.showinfo(
            "Mod installed",
            f"{msg_backup}"
            f"Installed:\n  {gpk_file}\n\u2192\n  {target}\n\n"
            "Launch the game to see your changes.",
        )

    def _pkg_backup_dir(self):
        return os.path.join(self.game_path.get(), "Content", "Packages", "1080p", "_backups")

    def _refresh_backups(self):
        self._restore_lb.delete(0, tk.END)
        # GR2 model backups
        backup_dir = self._backup_dir()
        if os.path.isdir(backup_dir):
            for f in sorted(os.listdir(backup_dir)):
                if f.endswith(".gpk"):
                    name = os.path.splitext(f)[0]
                    ts = datetime.fromtimestamp(
                        os.path.getmtime(os.path.join(backup_dir, f))
                    ).strftime("%Y-%m-%d %H:%M")
                    self._restore_lb.insert(tk.END, f"{name}  (model, backed up {ts})")
        # PKG texture backups
        pkg_backup = self._pkg_backup_dir()
        if os.path.isdir(pkg_backup):
            for f in sorted(os.listdir(pkg_backup)):
                if f.endswith(".pkg"):
                    name = os.path.splitext(f)[0]
                    ts = datetime.fromtimestamp(
                        os.path.getmtime(os.path.join(pkg_backup, f))
                    ).strftime("%Y-%m-%d %H:%M")
                    self._restore_lb.insert(tk.END, f"{name}  (texture, backed up {ts})")

    def _restore_selected(self):
        sel = self._restore_lb.curselection()
        if not sel:
            messagebox.showwarning("Nothing selected", "Select backup(s) to restore.")
            return

        backup_dir = self._backup_dir()
        gpk_dir = self._gpk_dir()
        pkg_backup = self._pkg_backup_dir()
        pkg_dir = os.path.join(self.game_path.get(), "Content", "Packages", "1080p")
        restored = []

        for idx in sel:
            text = self._restore_lb.get(idx)
            name = text.split("  (")[0].strip()
            if "texture" in text:
                src = os.path.join(pkg_backup, f"{name}.pkg")
                dst = os.path.join(pkg_dir, f"{name}.pkg")
            else:
                src = os.path.join(backup_dir, f"{name}.gpk")
                dst = os.path.join(gpk_dir, f"{name}.gpk")
            if os.path.isfile(src):
                shutil.copy2(src, dst)
                restored.append(name)

        if restored:
            self._restore_status.config(text=f"Restored {len(restored)} file(s)")
            self._status.set(f"Restored: {', '.join(restored)}")
            messagebox.showinfo(
                "Restore complete",
                f"Restored {len(restored)} original(s):\n" +
                "\n".join(f"  {n}" for n in restored),
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
