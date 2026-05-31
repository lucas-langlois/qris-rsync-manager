# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


project_root = Path(SPECPATH).parent
runtime_hook = project_root / "packaging" / "pyinstaller_runtime_hook.py"


a = Analysis(
    [str(project_root / "app" / "main.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "PySide6",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(runtime_hook)],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# PyInstaller may resolve Qt's ICU dependency from an unrelated conda install
# on PATH. That produced QtCore "specified procedure could not be found" at
# launch. Do not bundle ICU from outside this app; Windows provides compatible
# system ICU DLLs on supported Windows 10/11 systems.
a.binaries = [item for item in a.binaries if not Path(item[0]).name.lower().startswith("icu")]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="QRISRsyncManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
