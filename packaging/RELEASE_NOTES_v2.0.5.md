# PikachuNovel v2.0.5

## 模型调用与兼容性

- 支持 Chat Completions 和 Responses 两种调用协议。
- 支持流式接收长模型响应，严格拒绝未完成或错误的 Responses 流。
- CC Switch 导入会读取并验证 `wire_api`，未声明时自动探测 Responses 和 Chat Completions。
- CC Switch 验证会执行流式长响应检查，验证成功后才写入配置。
- 保持有限重试次数，并继续支持 HTTP 524 的 `retry_after`。

## 任务恢复与输入保护

- 事实卡、卷结构和全书结构均会及时写入断点状态。
- 大于 5 万字但未识别到章节或只识别到一章时，在调用模型前停止并提示修正章节标题。
- Responses 流式请求支持取消、失败事件和未完成事件处理。

## 其他

- 更新模型配置界面、命令行示例、README 和中文使用教程。
- Windows EXE 已通过健康接口、静态资源、更新检查、后台任务和窗口验证。
