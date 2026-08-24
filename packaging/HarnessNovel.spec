# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


project_root = Path(SPECPATH).parent
webview_datas, webview_binaries, webview_hiddenimports = collect_all("webview")
hiddenimports = sorted(set(
    webview_hiddenimports
    + collect_submodules("uvicorn")
    + ["novel_cli"]
))

a = Analysis(
    [str(project_root / "start_desktop.pyw")],
    pathex=[str(project_root)],
    binaries=webview_binaries,
    datas=[
        (str(project_root / "webui" / "static"), "webui/static"),
        (str(project_root / "core" / "prompts"), "core/prompts"),
    ] + webview_datas,
    hiddenimports=hiddenimports,
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
    name="HarnessNovel",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(project_root / "packaging" / "HarnessNovel.ico"),
)
