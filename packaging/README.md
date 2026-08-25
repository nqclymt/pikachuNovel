# Windows 构建

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

程序启动时会在后台检查 GitHub 最新稳定版。发现新版本后会询问是否打开发布页；确认后在系统浏览器下载新 EXE，再替换旧文件即可。

发布新版本时，同时更新 `webui\version.py` 中的 `APP_VERSION`、`setup.py` 版本号，并创建同名 Git tag；否则更新提示中的当前版本号会不准确。

构建后可启动隔离的测试配置并验证窗口、健康接口和静态资源：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\validate_windows_build.ps1
```
