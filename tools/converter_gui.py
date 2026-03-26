"""
Hades II Model Converter — GUI
Browse available character models and export them to glTF 2.0 (.glb).
Requires: numpy, pygltflib, lz4  (pip install numpy pygltflib lz4)
"""

import glob
import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ── Constants ─────────────────────────────────────────────────────────────────

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
CONVERTER    = os.path.join(SCRIPT_DIR, "gr2_to_gltf.py")

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
        self.root.minsize(960, 620)

        self.game_path   = tk.StringVar(value=find_game_path())
        self.output_path = tk.StringVar()
        self.all_lods    = tk.BooleanVar(value=False)
        self.debug_scan  = tk.BooleanVar(value=False)

        self._all_names: list[str] = []
        self._running   = False
        self._proc: subprocess.Popen | None = None

        self._build_ui()
        self.game_path.trace_add("write", lambda *_: self._scan())
        self._scan()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Game-path bar ──
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(fill=tk.X)
        ttk.Label(bar, text="Game directory:").pack(side=tk.LEFT)
        ttk.Entry(bar, textvariable=self.game_path, width=64).pack(
            side=tk.LEFT, padx=6, fill=tk.X, expand=True
        )
        ttk.Button(bar, text="Browse…", command=self._browse_game).pack(side=tk.LEFT)
        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(fill=tk.X)

        # ── Paned workspace ──
        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        # Left panel — file list
        left = ttk.Frame(paned, padding=4)
        paned.add(left, weight=1)
        self._build_file_panel(left)

        # Right panel — options + log
        right = ttk.Frame(paned, padding=4)
        paned.add(right, weight=2)
        self._build_options_panel(right)
        self._build_log_panel(right)

        # ── Status bar ──
        self._status = tk.StringVar(value="Ready")
        ttk.Label(
            self.root, textvariable=self._status,
            relief=tk.SUNKEN, anchor=tk.W, padding=(6, 2)
        ).pack(fill=tk.X, side=tk.BOTTOM)

    def _build_file_panel(self, parent):
        ttk.Label(parent, text="Available models", font=("", 10, "bold")).pack(anchor=tk.W)

        # Filter
        frow = ttk.Frame(parent)
        frow.pack(fill=tk.X, pady=(4, 2))
        ttk.Label(frow, text="Filter:").pack(side=tk.LEFT)
        self._filter = tk.StringVar()
        self._filter.trace_add("write", lambda *_: self._refresh_list())
        ttk.Entry(frow, textvariable=self._filter).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=4
        )

        # Listbox
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL)
        self._lb = tk.Listbox(
            frame, selectmode=tk.EXTENDED,
            yscrollcommand=sb.set, exportselection=False,
            activestyle="none", font=("Consolas", 9)
        )
        sb.config(command=self._lb.yview)
        self._lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._lb.bind("<<ListboxSelect>>", lambda _: self._update_export_btn())

        # Select/clear + count
        brow = ttk.Frame(parent)
        brow.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(brow, text="Select all", command=self._select_all).pack(side=tk.LEFT)
        ttk.Button(brow, text="Clear",      command=self._clear_sel ).pack(side=tk.LEFT, padx=4)
        self._count_lbl = ttk.Label(brow, text="")
        self._count_lbl.pack(side=tk.RIGHT)

    def _build_options_panel(self, parent):
        box = ttk.LabelFrame(parent, text="Export options", padding=10)
        box.pack(fill=tk.X)

        # Output directory
        orow = ttk.Frame(box)
        orow.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(orow, text="Output directory:").pack(side=tk.LEFT)
        ttk.Entry(orow, textvariable=self.output_path, width=44).pack(
            side=tk.LEFT, padx=6, fill=tk.X, expand=True
        )
        ttk.Button(orow, text="Browse…", command=self._browse_output).pack(side=tk.LEFT)

        # Checkboxes
        ttk.Checkbutton(
            box, text="Export all LODs (includes lower-resolution duplicates)",
            variable=self.all_lods
        ).pack(anchor=tk.W, pady=2)
        ttk.Checkbutton(
            box, text="Debug scan (print BoneBinding scan trace in log)",
            variable=self.debug_scan
        ).pack(anchor=tk.W, pady=2)

        # Export button + progress
        self._export_btn = ttk.Button(
            box, text="Export selected (0)",
            command=self._export, state=tk.DISABLED
        )
        self._export_btn.pack(pady=(8, 4))

        self._progress = ttk.Progressbar(box, mode="determinate")
        self._progress.pack(fill=tk.X)

    def _build_log_panel(self, parent):
        box = ttk.LabelFrame(parent, text="Output log", padding=4)
        box.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        sb = ttk.Scrollbar(box, orient=tk.VERTICAL)
        self._log = tk.Text(
            box, state=tk.DISABLED, wrap=tk.WORD,
            yscrollcommand=sb.set,
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
            font=("Consolas", 9), relief=tk.FLAT
        )
        sb.config(command=self._log.yview)
        self._log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

    # ── Game scanning ─────────────────────────────────────────────────────────

    def _gpk_dir(self):
        return os.path.join(self.game_path.get(), "Content", "GR2", "_Optimized")

    def _dll_path(self):
        return os.path.join(self.game_path.get(), "Ship", "granny2_x64.dll")

    def _scan(self):
        gpk_dir = self._gpk_dir()
        if not os.path.isdir(gpk_dir):
            self._all_names = []
            self._status.set("GPK directory not found — check game path.")
        else:
            self._all_names = sorted(
                os.path.splitext(os.path.basename(f))[0]
                for f in glob.glob(os.path.join(gpk_dir, "*.gpk"))
            )
            self._status.set(
                f"Found {len(self._all_names)} models in …/Content/GR2/_Optimized/"
            )
        self._refresh_list()

    def _refresh_list(self):
        q = self._filter.get().lower()
        self._visible = [n for n in self._all_names if q in n.lower()]
        self._lb.delete(0, tk.END)
        for name in self._visible:
            self._lb.insert(tk.END, name)
        self._count_lbl.config(text=f"{len(self._all_names)} files")
        self._update_export_btn()

    # ── Selection helpers ─────────────────────────────────────────────────────

    def _select_all(self):
        self._lb.select_set(0, tk.END)
        self._update_export_btn()

    def _clear_sel(self):
        self._lb.select_clear(0, tk.END)
        self._update_export_btn()

    def _selected_names(self) -> list[str]:
        return [self._visible[i] for i in self._lb.curselection()]

    def _update_export_btn(self):
        n = len(self._lb.curselection())
        self._export_btn.config(
            text=f"Export selected ({n})",
            state=tk.NORMAL if n > 0 and not self._running else tk.DISABLED,
        )

    # ── Browse dialogs ────────────────────────────────────────────────────────

    def _browse_game(self):
        p = filedialog.askdirectory(
            title="Select Hades II game directory",
            initialdir=self.game_path.get() or STEAM_PATHS[0],
        )
        if p:
            self.game_path.set(p)

    def _browse_output(self):
        p = filedialog.askdirectory(title="Select output directory")
        if p:
            self.output_path.set(p)

    # ── Export ────────────────────────────────────────────────────────────────

    def _export(self):
        names = self._selected_names()
        if not names:
            return

        out_dir = self.output_path.get().strip()
        if not out_dir:
            out_dir = filedialog.askdirectory(title="Select output directory")
            if not out_dir:
                return
            self.output_path.set(out_dir)

        dll = self._dll_path()
        if not os.path.isfile(dll):
            messagebox.showerror(
                "DLL not found",
                f"granny2_x64.dll not found at:\n{dll}\n\nCheck the game directory.",
            )
            return

        os.makedirs(out_dir, exist_ok=True)
        self._log_clear()
        self._set_running(True)
        self._progress["maximum"] = len(names)
        self._progress["value"]   = 0

        threading.Thread(
            target=self._export_worker,
            args=(names, out_dir, dll),
            daemon=True,
        ).start()

    def _export_worker(self, names: list[str], out_dir: str, dll: str):
        gpk_dir = self._gpk_dir()
        ok = errors = 0

        for i, name in enumerate(names):
            self._ui(lambda n=name, t=len(names), i=i:
                self._status.set(f"Exporting {n}  ({i+1}/{t})…")
            )
            self._log_append(f"\n{'─'*52}\n  [{i+1}/{len(names)}]  {name}\n{'─'*52}\n")

            cmd = [
                sys.executable, CONVERTER, name,
                "--gpk-dir", gpk_dir,
                "--dll",     dll,
                "-o",        os.path.join(out_dir, f"{name}.glb"),
            ]
            if self.all_lods.get():
                cmd.append("--all-lods")
            if self.debug_scan.get():
                cmd.append("--debug")

            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
                for line in proc.stdout:
                    self._log_append(line)
                proc.wait()
                if proc.returncode == 0:
                    ok += 1
                else:
                    errors += 1
                    self._log_append(f"  ✗  exited with code {proc.returncode}\n")
            except Exception as exc:
                errors += 1
                self._log_append(f"  ✗  {exc}\n")

            self._ui(lambda v=i+1: self._progress.config(value=v))

        self._ui(lambda: self._finish(ok, errors, out_dir))

    def _finish(self, ok: int, errors: int, out_dir: str):
        self._set_running(False)
        self._status.set(
            f"Done — {ok} exported, {errors} failed.  Output: {out_dir}"
        )
        if errors == 0:
            messagebox.showinfo(
                "Export complete",
                f"Exported {ok} model(s) to:\n{out_dir}",
            )
        else:
            messagebox.showwarning(
                "Export complete with errors",
                f"{ok} succeeded, {errors} failed.\nCheck the log for details.",
            )

    def _set_running(self, state: bool):
        self._running = state
        self._update_export_btn()
        if not state:
            self._progress["value"] = 0

    # ── Thread-safe UI helpers ────────────────────────────────────────────────

    def _ui(self, fn):
        self.root.after(0, fn)

    def _log_append(self, text: str):
        self._ui(lambda t=text: self._log_write(t))

    def _log_write(self, text: str):
        self._log.config(state=tk.NORMAL)
        self._log.insert(tk.END, text)
        self._log.see(tk.END)
        self._log.config(state=tk.DISABLED)

    def _log_clear(self):
        self._log.config(state=tk.NORMAL)
        self._log.delete("1.0", tk.END)
        self._log.config(state=tk.DISABLED)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
