# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path


# _tkinter.pyd must resolve Tcl/Tk from this project environment. Without
# this, an active system Conda can make PyInstaller bundle mismatched DLLs.
runtime_library_bin = Path(sys.prefix) / "Library" / "bin"
os.environ["PATH"] = os.pathsep.join(
    (str(runtime_library_bin), os.environ.get("PATH", ""))
)


a = Analysis(
    # The EXE packages only the desktop shell. Conversion remains in the
    # external src/pdf2md_cli.py so GUI, agents, and terminals share one CLI.
    ['../src/pdf2md_gui.py'],
    pathex=[],
    binaries=[],
    datas=[('../assets/pdf2md-icon.png', 'assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PDF2MD',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='../assets/pdf2md-icon.ico',
)
