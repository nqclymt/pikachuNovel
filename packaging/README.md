# 桌面版构建

## macOS：M 系列芯片

目标为 Apple Silicon（arm64），当前构建配置以 macOS 14 或更新系统为基线。必须使用 Mac 上的原生 arm64 Python 3.12；Windows 不能通过 PyInstaller 交叉生成可运行的 `.app`。

在 Mac 的项目根目录执行：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install '.[desktop]' -r packaging/requirements-macos.txt
python scripts/build_macos.py
```

脚本会先生成 `.app`、检查 arm64 架构和临时签名，然后用隔离配置启动桌面程序，验证提示词、页面资源、后台创建工作区任务和中文日志；通过后再生成安装文件。此检查不调用大模型 API，也不能替代在真实 Mac 上检查窗口显示和完整写作流程。

输出目录为 `release/macos-arm64/`，包含：

- `PikachuNovel.app`
- `PikachuNovel-<版本>-macOS-arm64.dmg`，打开后将应用拖到 Applications
- `PikachuNovel-<版本>-macOS-arm64.app.zip`
- DMG 和 ZIP 对应的 `.sha256` 校验文件

默认小说目录为 `~/Documents/my-novels`；已有目录配置和 `HARNESS_NOVEL_HOME` 优先生效。Mac 版支持在 Finder 中定位章节，目前需要手动替换应用进行更新。

也可使用 `.github/workflows/build-macos.yml`，在 GitHub 的 M1 runner 上自动测试和构建。推送 `codex/macos-arm64` 分支会触发构建；工作流进入默认分支后，也可从 Actions 页手动运行。成功后从该次运行的 `PikachuNovel-macOS-arm64` artifact 下载 DMG、ZIP 和校验文件，保留 14 天。该流程不创建 Release。

当前配置使用 PyInstaller 的 ad-hoc 临时签名，没有 Apple Developer ID 签名和公证，适合先做功能测试；下载到其他 Mac 后可能被 Gatekeeper 阻止。面向用户正式分发前需要另外配置 Developer ID 签名、公证，并进行真机验证。

参考：[PyInstaller 平台限制](https://pyinstaller.org/en/stable/)、[macOS 架构及签名](https://pyinstaller.org/en/stable/feature-notes.html#macos-multi-arch-support)、[GitHub macOS runner](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)。

## Windows

在项目根目录安装构建依赖：

```powershell
python -m pip install -r packaging\requirements-windows.txt
```

执行干净构建：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -Clean
```

如果系统中的 `python` 指向 Microsoft Store 占位别名，请明确传入 Python：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -Clean `
  -PythonExecutable "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
```

输出文件位于 `release\PikachuNovel.exe` 和 `release\PikachuNovel.exe.sha256`。`-Clean` 只清理项目内的 `release` 与 `build\pyinstaller` 生成目录。

EXE 图标来源是 `packaging\PikachuNovel.ico`。项目同时保留 `packaging\PikachuNovel.png` 源图；替换 PNG 后应先转换为包含 16、24、32、48、64、128、256 像素的 Windows `.ico`，再执行构建。单纯把 PNG 改名为 `.ico` 无效。

程序启动时会在后台检查 GitHub 最新稳定版。Windows 打包 EXE 检测到新版本后，可直接点击顶部“更新”：程序只接受同一 GitHub Release 中的 `PikachuNovel.exe` 与 `PikachuNovel.exe.sha256`，下载后先校验 SHA256，再退出当前进程、替换 EXE 并自动重启。若新版本启动后立即退出，更新辅助程序会尝试恢复并重启旧版本。源码/Python 运行模式不会自动覆盖文件，而是保留打开 Release 页的手动更新方式。

发布新版本时，同时更新 `webui\version.py` 中的 `APP_VERSION`、`setup.py` 版本号，并创建同名 Git tag；否则更新提示中的当前版本号会不准确。

构建后可启动隔离的测试配置并验证窗口、健康接口和静态资源：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\validate_windows_build.ps1
```
