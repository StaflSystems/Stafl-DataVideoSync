# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Data Video Sync.
Build:  pyinstaller data_video_sync.spec
"""

block_cipher = None

a = Analysis(
    ["data_video_sync.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        "cantools",
        "cantools.database",
        "cantools.database.can",
        "cantools.database.can.database",
        "can",
        "can.io",
        "can.io.blf",
        "can.interfaces",
        "numpy",
        "cv2",
        "PyQt5",
        "PyQt5.QtCore",
        "PyQt5.QtGui",
        "PyQt5.QtWidgets",
        "PyQt5.sip",
        "matplotlib",
        "matplotlib.backends.backend_qt5agg",
        "matplotlib.figure",
        "openpyxl",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "test", "xmlrpc"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Data_Video_Sync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
