# PikachuNovel v2.0.4

本版本将桌面 Agent 正式更名为 PikachuNovel，并增强模型任务控制、错误恢复、文本编码兼容和软件更新体验。

## 本版改进

- 桌面程序更名为 `PikachuNovel.exe`，并使用 `packaging/PikachuNovel.ico` 作为可替换图标源。
- 启动后自动检查 GitHub 最新稳定版；发现新版本时可确认并打开安全的 Release 下载页。
- 模型调用失败采用有限次数重试，显示当前尝试次数，支持停止任务并保留已完成进度。
- HTTP 524 服务商超时会遵守 `Retry-After`，等待时间限制在 120 秒内，并显示可操作的中文提示。
- 支持检测并导入 CC Switch 中的 Codex 服务商配置，减少 Base URL、模型名和 API Key 手工填写错误。
- 修复 Windows 单文件版后台任务中文日志乱码，构建验证会拒绝含替换字符的日志。
- 统一 TXT、写作指南、创作方向、目标世界资料和工作台预览的 UTF-8、UTF-16/32、GB18030/GBK、Big5 编码识别。
- 完善首次源码运行、模型配置、任务暂停/停止/继续、升级与故障排查教程。

## 使用方法

1. 下载 `PikachuNovel.exe` 和 `PikachuNovel.exe.sha256`。
2. 可按教程校验 SHA256，然后双击 `PikachuNovel.exe`。
3. 在工作台配置模型 API，或直接从 CC Switch 导入并验证配置。
4. 旧版作品和配置会继续保留；替换 EXE 不会删除已有工作区。

Windows 10/11 需要 Microsoft Edge WebView2 Runtime，系统通常已经安装。
