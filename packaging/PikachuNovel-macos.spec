# -*- mode: python ; coding: utf-8 -*-
"""Native Apple Silicon bundle; invoke through scripts/build_macos.py."""

import runpy
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

project_root = Path(SPECPATH).parent
version = runpy.run_path(str(project_root / "webui" / "version.py"))["APP_VERSION"]
webview_datas, webview_binaries, webview_hiddenimports = collect_all("webview")

a = Analysis(
    [str(project_root / "start_desktop.pyw")],
    pathex=[str(project_root)],
    binaries=webview_binaries,
    datas=[
        (str(project_root / "webui" / "static"), "webui/static"),
        (str(project_root / "core" / "prompts"), "core/prompts"),
        (str(project_root / "core" / "system_prompt.md"), "core"),
        (str(project_root / "core" / "agents.md"), "core"),
    ] + webview_datas,
    hiddenimports=sorted(set(
        webview_hiddenimports + collect_submodules("uvicorn")
        + ["novel_cli", "webui.app", "webui.desktop", "webview.platforms.cocoa"]
    )),
    excludes=["webview.platforms.winforms", "webview.platforms.gtk", "webview.platforms.qt"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PikachuNovel",
    console=False,
    strip=False,
    upx=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,  # PyInstaller applies ad-hoc signing for local testing.
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="PikachuNovel")
app = BUNDLE(
    coll,
    name="PikachuNovel.app",
    icon=str(project_root / "packaging" / "PikachuNovel.png"),
    bundle_identifier="com.pikachunovel.desktop",
    version=version,
    info_plist={
        "CFBundleDisplayName": "PikachuNovel",
        "CFBundleShortVersionString": version,
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    },
)
