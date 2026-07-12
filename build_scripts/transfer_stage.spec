# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Transfer Stage Control application.

Build::

    pyinstaller --clean --noconfirm build_scripts\\transfer_stage.spec

Output: ``dist\\TransferStageControl.exe``
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(SPECPATH).parent  # SPECPATH is set by PyInstaller to the .spec dir

# Find conda env DLLs needed at runtime
_LIBRARY_BIN = None
for _candidate in [
    Path(sys.prefix) / 'Library' / 'bin',
    Path(sys.base_prefix) / 'Library' / 'bin',
]:
    if _candidate.is_dir():
        _LIBRARY_BIN = _candidate
        break

_BUNDLED_DLLS = []
if _LIBRARY_BIN is not None:
    for _dll_name in ['tcl86t.dll', 'tk86t.dll', 'liblzma.dll', 'libbz2.dll', 'libexpat.dll', 'ffi-8.dll']:
        _dll_path = _LIBRARY_BIN / _dll_name
        if _dll_path.is_file():
            _BUNDLED_DLLS.append((str(_dll_path), '.'))

a = Analysis(
    [str(PROJECT_ROOT / 'main.py')],
    pathex=[str(PROJECT_ROOT)],
    binaries=_BUNDLED_DLLS,
    datas=[
        (str(PROJECT_ROOT / 'icon' / 'icon.png'), 'icon'),
    ],
    hiddenimports=[
        'serial.tools.list_ports_windows',
        'serial.tools.list_ports_common',
        'tkinter',
        'tkinter.ttk',
        'queue',
        'logging',
        'json',
        'threading',
        'ctypes',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib',
        'numpy',
        'scipy',
        'pandas',
        'PIL',
        'cv2',
        'pygame',
        'wx',
        'PyQt5',
        'PyQt6',
        'PySide2',
        'PySide6',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='TransferStageControl',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / 'icon' / 'icon.png'),
    version=str(PROJECT_ROOT / 'build_scripts' / 'version_info.txt'),
)
