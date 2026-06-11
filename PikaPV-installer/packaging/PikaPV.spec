# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"

datas = [
    (str(SRC / "templates"), "templates"),
    (str(SRC / "static"), "static"),
    (str(SRC / "default_settings.yaml"), "."),
    (str(SRC / "speedprofile_settings.yaml"), "."),
]

a = Analysis(
    [str(SRC / "web_app.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=["pikapv_backend", "yaml", "waitress"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PikaPV",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="PikaPV",
)

