# PikachuNovel v2.0.7

- 新增 Windows 桌面版常驻“更新”按钮；检测到新版本时显示红点提醒。
- 新增一键安全自动更新：下载 `PikachuNovel.exe` 与 `PikachuNovel.exe.sha256`，校验 SHA256 后替换并自动重启；启动异常时尝试回滚。
- 修复 Windows 路径分隔符导致“生成内容”区域无法显示已生成文件的问题，统一 Web UI 文件树路径为 `/`。
- 保留现有小说工作区数据，不参与程序更新替换。
