"""CG3H Builder — Standalone entry point for PyInstaller exe."""
import sys, os
_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)
if sys.stdout and sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from cg3h_build import main
if __name__ == '__main__':
    main()
