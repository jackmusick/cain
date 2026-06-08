#!/usr/bin/env python3
"""
Build a single double-clickable executable of the Save Editor with PyInstaller.

  python3 build.py

Output: dist/SaveEditor(.exe)   — one file, double-clickable, opens the native
Qt desktop window. The MPQ and save path are chosen by the user at runtime and
remembered in native app settings; no game files are bundled.

Requirements (install once):
  pip install PySide6 pyinstaller

Cross-platform: run this on each target OS to get that OS's binary
(Windows .exe, Linux ELF, macOS .app) — PyInstaller does not cross-compile.
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(HERE, "native", "app.py")


def main():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("PyInstaller not installed. Run: pip install pyinstaller PySide6")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--clean",
        "--onefile",
        "--name", "SaveEditor",
        "--windowed" if os.name == "nt" else "--console",
        # bundle the whole package so core/ + gui/ imports resolve
        "--paths", HERE,
        ENTRY,
    ]
    print("running:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=HERE)
    out = os.path.join(HERE, "dist", "SaveEditor" + (".exe" if os.name == "nt" else ""))
    print(f"\nBuilt: {out}")
    print("Double-click it (or run from a terminal). Pick your MPQ and save on first run.")


if __name__ == "__main__":
    main()
