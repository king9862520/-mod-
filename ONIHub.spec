# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

pyside_datas, pyside_binaries, pyside_hiddenimports = collect_all("PySide6")

a = Analysis(
    ["main.py"],
    pathex=["src"],
    binaries=pyside_binaries,
    datas=pyside_datas,
    hiddenimports=pyside_hiddenimports + [
        "onihub.app", "onihub.worker", "onihub.storage", "onihub.database",
        "onihub.models", "onihub.paths", "onihub.workshop", "onihub.steam",
        "onihub.steam_bridge", "onihub.legacy_installer", "onihub.indexer",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ONIHub",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
