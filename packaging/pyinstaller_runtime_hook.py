from __future__ import annotations

import os
import sys
from pathlib import Path


def _add_dll_dir(path: Path) -> None:
    if path.exists():
        os.add_dll_directory(str(path))


if hasattr(sys, "_MEIPASS"):
    bundle_root = Path(sys._MEIPASS)
    pyside_dir = bundle_root / "PySide6"
    shiboken_dir = bundle_root / "shiboken6"

    _add_dll_dir(pyside_dir)
    _add_dll_dir(shiboken_dir)
    _add_dll_dir(bundle_root)

    os.environ["PATH"] = os.pathsep.join(
        [str(pyside_dir), str(shiboken_dir), str(bundle_root), os.environ.get("PATH", "")]
    )
