"""Shared constants for CG3H tools."""
import os
import re

# Single source of truth for the CG3H release version.  Bump this on
# release; tests/test_core.py::test_version_consistency enforces that
# .github/thunderstore/manifest.json matches.
CG3H_VERSION = "3.13.0"

# Thunderstore dependency string.  The folder name AuthorName-ModName
# is required by Hell2Modding's plugin loader (lua_manager.cpp:89
# counts hyphens and rejects any folder that doesn't match).
CG3H_BUILDER_AUTHOR = "Enderclem"
CG3H_BUILDER_NAME = "CG3HBuilder"
CG3H_BUILDER_FOLDER = f"{CG3H_BUILDER_AUTHOR}-{CG3H_BUILDER_NAME}"
CG3H_BUILDER_DEPENDENCY = f"{CG3H_BUILDER_FOLDER}-{CG3H_VERSION}"
# Upstream Hell2Modding nightly 1.0.92 is the first build that contains
# all the CG3H-required APIs (add_granny_file/add_package_file, the
# draw.cpp bindings, and the static GPU buffer pool size patches).
# Retires the `Enderclem-Hell2ModdingCG3H` fork we shipped for v3.9.0/3.9.1.
H2M_VERSION = "1.0.95"
H2M_DEPENDENCY = f"Hell2Modding-Hell2Modding-{H2M_VERSION}"

HADES2_APP_ID = "1145350"

# Hardcoded fallback paths (checked last)
_FALLBACK_PATHS = [
    r"C:\Program Files (x86)\Steam\steamapps\common\Hades II",
    r"C:\Program Files\Steam\steamapps\common\Hades II",
    r"D:\Steam\steamapps\common\Hades II",
    r"D:\SteamLibrary\steamapps\common\Hades II",
    r"E:\SteamLibrary\steamapps\common\Hades II",
]


def _find_steam_root():
    """Find Steam install path from the Windows registry."""
    try:
        import winreg
        for hive, subkey in [
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
        ]:
            try:
                key = winreg.OpenKey(hive, subkey)
                for val_name in ("SteamPath", "InstallPath"):
                    try:
                        path, _ = winreg.QueryValueEx(key, val_name)
                        if path and os.path.isdir(path):
                            return path
                    except OSError:
                        continue
            except OSError:
                continue
    except ImportError:
        pass  # Not on Windows
    return None


def _parse_library_folders(steam_root):
    """Parse libraryfolders.vdf to find all Steam library paths."""
    vdf_path = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
    if not os.path.isfile(vdf_path):
        return [steam_root]

    libraries = [steam_root]
    try:
        with open(vdf_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        # Match "path" values in the VDF
        for match in re.finditer(r'"path"\s+"([^"]+)"', content):
            path = match.group(1).replace("\\\\", "\\")
            if os.path.isdir(path) and path not in libraries:
                libraries.append(path)
    except Exception:
        pass  # missing or malformed libraryfolders.vdf — return whatever we have
    return libraries


def _find_game_in_libraries(libraries):
    """Search Steam libraries for Hades II."""
    for lib in libraries:
        game_path = os.path.join(lib, "steamapps", "common", "Hades II")
        if os.path.isdir(game_path):
            return game_path
    return None


def find_game_path():
    """Find the Hades II game directory.

    Detection order:
    1. Steam registry → libraryfolders.vdf → scan all libraries
    2. Hardcoded fallback paths
    """
    # Try Steam registry + library scanning
    steam_root = _find_steam_root()
    if steam_root:
        libraries = _parse_library_folders(steam_root)
        game = _find_game_in_libraries(libraries)
        if game:
            return game

    # Fallback: hardcoded paths
    for p in _FALLBACK_PATHS:
        if os.path.isdir(p):
            return p

    return ""


# For backward compatibility — modules that import STEAM_PATHS directly
STEAM_PATHS = _FALLBACK_PATHS
