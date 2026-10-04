# Antigravity 接入与测试

PikachuNovel 可以通过官方 `agy` CLI 执行小说写作任务。全书设计、人物和世界观记忆、章节编排、正文校验及文件保存仍由 PikachuNovel 管理；CLI 负责根据本次提供的上下文返回文本。现有 API 后端继续可用，各任务组可以分别配置。

## 安装和登录

1. 按 [官方安装与认证说明](https://www.antigravity.google/docs/cli/install/)安装 Antigravity CLI。`agy` 是外部依赖，源码安装和桌面 EXE 都不会附带它。
2. 重新打开终端，执行 `agy --version`，再执行 `agy`，按照官方流程登录你的 Google 账号。登录与凭据由 CLI 管理，本项目不读取或复制登录令牌。
3. 在终端执行 `agy models`，查看账号当前可选的模型 ID。不要直接复制其他 API 服务商的模型名。
4. 回到 PikachuNovel 设置页，将要测试的任务组切换为“Antigravity（Google 登录）”。

Windows 官方默认安装位置是 `%LOCALAPPDATA%\agy\bin\agy.exe`；macOS/Linux 为 `~/.local/bin/agy`。如果自动检测失败，在设置中填写实际可执行文件的绝对路径。路径字段只接受程序路径，不接受整段终端命令或附加参数。

要使用 Google AI Pro 对应的 Antigravity 权益，应在 CLI 中采用 Google 账号登录。官方 CLI 另有 Gemini API Key 模式，它直接调用 Gemini API；本项目中的 API Key 字段不会被转交给 CLI。可用模型、配额和可能的 AI Credits 超额使用由 Google 账户及 CLI 自身设置决定，本项目不购买额度，也不调整该设置。请以 [官方套餐说明](https://www.antigravity.google/docs/plans/)和账号中的用量页为准。

如果之前将 CLI 设置为 Gemini API Key 模式，请先按官方安装文档的“Revert to default authentication”步骤切回账号登录；本项目不会修改 CLI 的用户设置，也不会向 CLI 传递 Gemini API Key 环境变量。

## 配置字段

以下组名用于环境变量和 `~/.harnessNovel/.env`，也对应设置页的模型分组：

| 前缀 | 用途 |
| --- | --- |
| `DATA_BUILDER` | 参考小说拆解 |
| `ADAPTIVE_BUILDER` | 全书设计与舞台设计 |
| `ADAPTIVE_BUILDER_LITE` | 故事情节、逐章章纲、正文及轻量辅助任务 |
| `HUMANIZE_BUILDER` | 正文精修；未单独配置时继承写作生产模型 |

各组支持相同字段：

| 后缀 | 值及含义 |
| --- | --- |
| `_BACKEND` | `openai`（默认）或 `antigravity_cli` |
| `_MODEL` | CLI 模型 ID；留空使用 CLI 默认模型 |
| `_CLI_PATH` | `agy` 可执行文件路径；留空自动查找 |
| `_CLI_AGENT` | 已安装的自定义 Agent 名；留空使用本项目内置的小说写作 Agent |
| `_CLI_EFFORT` | `low`、`medium`（默认）或 `high` |
| `_BASE_URL`、`_API_KEY`、`_WIRE_API` | 仅用于原 API 后端，CLI 后端忽略这些字段 |

先只切换写作生产组的示例：

```ini
ADAPTIVE_BUILDER_LITE_BACKEND=antigravity_cli
ADAPTIVE_BUILDER_LITE_MODEL=
ADAPTIVE_BUILDER_LITE_CLI_PATH=
ADAPTIVE_BUILDER_LITE_CLI_AGENT=
ADAPTIVE_BUILDER_LITE_CLI_EFFORT=medium
```

切换后端时，应将旧 API 的模型名清空或替换为 `agy models` 列出的准确 ID。保留的原 API 配置可用于以后切回 API。若使用自定义 Agent，将其安装在 CLI 的全局 Agent 目录 `~/.gemini/config/agents/<名称>/agent.md`，再填 Agent 名，并在 Agent 配置中禁用工具（`tools: []`）。项目每次使用临时工作目录，不会加载真实小说目录中的 Agent 配置。目录规范见 [官方自定义 Agent 文档](https://www.antigravity.google/docs/cli/commands/agents/)。

## 调度和内容保存

- 同一系统用户下的 PikachuNovel CLI 请求共用调度锁，线程和后台任务子进程均串行执行，避免多项写作任务同时争用订阅配额。此锁不会限制你另行手动启动的 `agy`。
- 排队阶段可以取消；运行中的取消和超时会结束本次 CLI 调用。已由项目保存的章节和备份继续保留。
- 每次请求使用独立会话和临时工作目录。对话修改所需的前文由项目重新组织，不依赖 CLI 的“最近一次对话”，不同小说和不同任务不会因此共用会话上下文。
- 长文本通过标准输入发送；项目只接收 CLI 成功完成的最终结果，不把工具过程或中途生成的文本作为正文保存。调用协议依据 [官方 Headless 文档](https://www.antigravity.google/docs/cli/headless/)。
- 内置 Agent 要求直接返回所需文本或 JSON，不执行小说文件编辑。临时工作目录用于减少误操作范围，不是操作系统安全沙箱；CLI 的全局插件和权限仍由用户自行管理。
- 认证或额度错误会暂停后续 CLI 调用，避免反复重试。处理登录或等待配额恢复后，在设置页点击“恢复调度”，再到原任务界面继续所需写作任务。“恢复调度”只解除调用暂停，不会自动重试已中断任务。项目不会自动换模型、换账号或转用付费 API。

调度状态保存在 `~/.harnessNovel/antigravity`，暂停状态在程序重启后仍然保留。排队不占用单次生成的运行时限；运行时限复用 `HARNESS_NOVEL_LLM_TIMEOUT`（默认 600 秒）。连接测试另设约 65 秒总等待上限，避免长时间排队后界面一直等待。

正文精修仍经过项目现有的审读、局部替换和验证流程；一次生成可能包含场景规划、正文生成、精修等多次模型调用。开启精修会增加耗时和配额消耗。

## 首次验证顺序

1. 在设置页点击“检测安装”，确认路径和版本；检测通过仅代表本机程序可调用，不代表账号登录、Headless 协议或模型可用。
2. 登录完成后点击“测试连接（使用额度）”。连接测试会发送一条短提示词，可能消耗账号配额。检测和测试使用当前表单中的值，测试成功后还需要保存设置才对正式任务生效。
3. 用测试作品生成一章，检查正文格式、中文文风、人物事实及章纲承接，再尝试局部修改和正文精修。
4. 检查取消、失败提示和继续任务的表现，确认符合预期后再开启多章生产或将其他组切换为 Antigravity。

发现“需要登录”时，在终端重新运行 `agy` 登录；遇到模型不可用时，核对 `agy models`；遇到协议不支持时，按官方方式升级 CLI。额度不足时，先查看账号用量，恢复调度不会重置 Google 的配额。

本次接入提供调用和调度逻辑；自动测试使用模拟 CLI，不代表已验证你的 Google 账号、实际配额或中文长篇写作质量。真实账号测试需要你完成登录后按上述步骤进行。
