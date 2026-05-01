"""Folder-mirror walker for v3.12 `texture_variant` mods.

A `texture_variant` mod ships its texture overrides under a top-level
`textures/` folder whose internal layout mirrors the game's PKG entry
paths.  Builder walks `textures/` recursively, treats each relative
path as a PKG entry path (forward-slash → backslash), and emits one
override descriptor per file.

Example mod layout::

    MyMelinoeRedDress/
        mod.json                            (type: texture_variant)
        preview.png                         (auto- or hand-supplied)
        textures/
            GR2/Melinoe_Body_BC.png         → GR2\\Melinoe_Body_BC.png
            UI/Portraits/Melinoe.png        → UI\\Portraits\\Melinoe.png

The walker is intentionally path-agnostic — `GR2\\…` and `UI\\…` go
through the same emit path.  No portrait-specific logic.
"""
from __future__ import annotations

import os


_SUPPORTED_EXTS = {".png"}


def walk_texture_overrides(mod_dir):
    """Walk `<mod_dir>/textures/` and yield one descriptor per file.

    Each descriptor is a dict with::

        {
            "pkg_entry":   "GR2\\Melinoe_Body_BC.png",   # game-side path
            "source_path": "<abs path to the file on disk>",
            "rel_path":    "GR2/Melinoe_Body_BC.png",    # for logging / UI
        }

    Files with extensions outside `_SUPPORTED_EXTS` are silently
    skipped (lets modders drop a `.psd` working copy alongside the
    exported PNG without it leaking into the build).  Hidden files
    (dotfile prefix) are skipped too.

    Yields nothing if `<mod_dir>/textures/` does not exist — a
    `texture_variant` mod that ships only metadata is technically
    valid (no-op at runtime).
    """
    textures_root = os.path.join(mod_dir, "textures")
    if not os.path.isdir(textures_root):
        return
    for dirpath, dirnames, filenames in os.walk(textures_root):
        # Stable order so cache keys + manifests are deterministic.
        dirnames.sort()
        for fname in sorted(filenames):
            if fname.startswith("."):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _SUPPORTED_EXTS:
                continue
            abs_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(abs_path, textures_root)
            # PKG entry paths use backslashes; normalize whatever the
            # host OS gave us.  Drop the file extension — the game's
            # PKG manifest stores entries by base name (no .png), so
            # `textures/GR2/Melinoe_Color512.png` overrides
            # `GR2\Melinoe_Color512`.
            pkg_entry_with_ext = rel_path.replace(os.sep, "\\") \
                                         .replace("/", "\\")
            pkg_entry = os.path.splitext(pkg_entry_with_ext)[0]
            yield {
                "pkg_entry": pkg_entry,
                "source_path": abs_path,
                "rel_path": rel_path.replace(os.sep, "/"),
            }


def collect_overrides(mod_dir):
    """List form of `walk_texture_overrides` — convenience for callers
    that need to count / sort / validate the full set."""
    return list(walk_texture_overrides(mod_dir))
