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

输出文件位于 `release\HarnessNovel.exe` 和 `release\HarnessNovel.exe.sha256`。`-Clean` 只清理项目内的 `release` 与 `build\pyinstaller` 生成目录。

构建后可启动隔离的测试配置并验证窗口、健康接口和静态资源：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\validate_windows_build.ps1
```
