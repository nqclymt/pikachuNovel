# HarnessNovel v2.0.3

这是首个提供 Windows 单文件桌面程序的版本。下载 `HarnessNovel.exe` 后可直接双击运行，无需单独安装 Python。

## 本版改进

- 提供 Windows 单文件桌面版及 SHA256 校验文件。
- 修复源码首次运行时桌面依赖安装失败后的诊断与重试流程。
- 模型调用改为有限次数重试，并在日志中显示当前尝试次数和等待状态。
- 对话生成任务支持暂停、继续和结束；后台命令任务支持停止。
- 支持从 CC Switch 读取、验证并导入 Codex 供应商配置。
- 补充 Windows 安装、API 配置、任务控制和故障排查教程。

## 使用方法

1. 下载 `HarnessNovel.exe` 和 `HarnessNovel.exe.sha256`。
2. 按项目教程校验 SHA256（推荐）。
3. 双击 `HarnessNovel.exe`。
4. 在右上角配置模型 API，或从 CC Switch 导入配置。

作品和配置保存在用户目录中，后续替换 EXE 升级不会删除已有内容。Windows 10/11 需要 Microsoft Edge WebView2 Runtime，系统通常已经安装。
