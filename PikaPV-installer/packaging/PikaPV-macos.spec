# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

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
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="PikaPV",
)

app = BUNDLE(
    coll,
    name="PikaPV.app",
    icon=None,
    bundle_identifier="org.caqm.pikapv",
    version=VERSION,
)
