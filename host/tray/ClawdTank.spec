# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for ClawdTank.exe: onedir, windowed (no console), Clawd icon.
#
#   host\tray\build-tray.ps1          (creates the venv, builds the icons, runs this)
#
# bleak 3 talks to Windows through the winrt-* wheels. winrt is a namespace
# package with one .pyd per Windows namespace, which PyInstaller does not find by
# following imports, so everything under it is collected explicitly. "ClawdTank.exe
# --selftest" imports all of it from the built exe and proves it is there.
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct,
    VSVersionInfo,
)

HERE = Path(SPECPATH).resolve()          # host/tray
HOST = HERE.parent                       # host
ASSETS = HOST / "clawd_tank_tray" / "assets"

version = re.search(r'__version__\s*=\s*"([^"]+)"',
                    (HOST / "clawd_tank_tray" / "__init__.py").read_text(encoding="utf-8")).group(1)
nums = (tuple(int(p) for p in version.split(".")) + (0, 0, 0, 0))[:4]

hiddenimports = (
    collect_submodules("winrt")
    + collect_submodules("bleak")
    + collect_submodules("clawd_tank_daemon")
    + collect_submodules("clawd_tank_tray")
    + ["clawd_tank_menubar", "clawd_tank_menubar.hooks", "pystray._win32"]
)
binaries = collect_dynamic_libs("winrt")
datas = [(str(ASSETS), "clawd_tank_tray/assets")]

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=nums, prodvers=nums, mask=0x3F, flags=0x0, OS=0x40004,
                      fileType=0x1, subtype=0x0, date=(0, 0)),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("FileDescription", "Clawd Tank"),
            StringStruct("ProductName", "Clawd Tank"),
            StringStruct("FileVersion", version),
            StringStruct("ProductVersion", version),
            StringStruct("InternalName", "ClawdTank"),
            StringStruct("OriginalFilename", "ClawdTank.exe"),
            StringStruct("LegalCopyright", "Clawd sprites: MIT, Copyright (c) 2026 Marcio Granzotto Rodrigues"),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

a = Analysis(
    [str(HERE / "launch.py")],
    pathex=[str(HOST)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "_pytest", "pytest_asyncio", "rumps", "objc", "setuptools", "pip"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ClawdTank",
    console=False,
    icon=str(ASSETS / "clawd.ico"),
    version=version_info,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ClawdTank", upx=False)
