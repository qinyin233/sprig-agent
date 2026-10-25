# 模型输出与思考配置

`--max-tokens N` 请求服务为每次模型调用限制输出 token，范围 1–65,536。Chat Completions 使用所选 `token_limit_field`，Responses 使用 `max_output_tokens`；Messages 未选上限时仍发送协议必需的 `max_tokens=4096`。它与文件/命令输出字节限制 `--max-output`、请求体字节上限 `--max-context-bytes`、轮次上限 `--max-steps` 分开，不是总会话 token 或账单硬上限。服务支持的范围和思考用量计法由提供商决定。

`--thinking` / `thinking=true` 选择 Sprig 的协议预设：

| 协议 | 发送字段 |
| --- | --- |
| Chat Completions | `chat_template_kwargs: {"enable_thinking": true}` |
| Messages | `thinking: {"type": "adaptive"}` |
| Responses | `reasoning: {"effort": "medium"}`；配置 `reasoning_effort` 时采用该值 |

这些预设需要提供商和模型支持。`thinking=false` 或 `--no-thinking` 只取消预设，不发送关闭字段，也不清除显式配置的 `extra_body`；不能据此认定服务端思考已关闭。默认请求保留服务自己的策略，不按模型名称猜测参数，不因拒绝参数而自动重试或更换模型。

要显式关闭思考，按服务文档配置实际字段。例如，[DeepSeek 官方文档](https://api-docs.deepseek.com/guides/thinking_mode/)给出 Chat Completions 的 `thinking.type=disabled`。仅在你的提供商确认遵循这一格式时，给相应提供商添加：

```toml
[providers.YOUR_PROVIDER.generation]
thinking = false

[providers.YOUR_PROVIDER.generation.extra_body.thinking]
type = "disabled"
```

Messages 的原生字段可通过 `extra_body.thinking` 传入；Responses 的 `reasoning_effort="none"` 会编码为 `reasoning.effort="none"`。是否接受这些取值必须以服务文档为准；兼容网关不能仅凭协议名称推断。完整设置、优先级和扩展字段限制见[配置说明](configuration.md)。

PowerShell 的已有 Agnes 启动器原样传递这些选项：

```powershell
.\scripts\run-agnes.ps1 --tui --workdir . --max-tokens 2048
```

要显式尝试思考预设，在上面加 `--thinking`。它可能增加输出用量与延迟；未选预设不能推断服务的默认思考模式。输出上限过低可能截断较长的工具参数，需按任务调整，不能用短回复当作修复完成。

在 Sprig 中，无论流式与否，只有完整、有效的最终文字或工具意图才会进入 agent 循环。流式文字可能在结束状态到达之前已显示；服务返回输出截断状态时仍判为未完成，截断响应里的工具动作不会执行，且不自动重试。继续对话需用户提交新消息或 `/continue`。Chat Completions 的 `reasoning_content` 默认不显示、不保存或回传，显式 `preserve_reasoning` 的隐私影响见[推理历史兼容](reasoning-protocol.md)；Messages/Responses 的原生连续性字段见[三协议支持](provider-protocols.md)。

选项进入统一的请求编码，因此 `/context`、压缩和 provider 发送前检查都计算它们的实际 JSON 字节。明确选择的 token 上限和思考模式也进入会话绑定；恢复时需传入相同参数，防止无意改变生成配置。未选择选项的旧会话保持原绑定格式。应用的逐次批准、只读、取消和路径限制继续独立执行。

基准驱动的 `generation_options` 记录选定的字段；mini 的固定 Chat Completions 适配器把标准生成字段交给原生 SDK，把提供商扩展交给 SDK 的 `extra_body`，保留上游 agent 循环。普通基准 CLI 只暴露它列出的参数，不自动读取用户的私有 `config.toml`。Python 驱动接收更多生成设置时也必须完整传递，不能只在报告中记录。mini 对照不支持 Messages/Responses，显式传入这两种协议会在加载 SDK 前报错。

本地测试使用真实 loopback HTTP，覆盖三协议 JSON/SSE 的显式关闭字段、字节预算、隐私、截断后不执行工具和会话绑定。mini 使用锁定的真实 SDK 核对实际请求中的输出上限、采样、推理强度及扩展字段。这些是实现和协议证据，不能证明服务端执行了思考开关或复杂任务成功率提高。历史报告不补写参数；`thinking:false` 只能说明当时未选 Sprig 预设，不能改写为服务端关闭思考的证据。

远程原生 OS 检查须以实际提交 SHA 和对应 Actions 作业为准。当前 CI 冻结规则见 [AGENTS.md](../AGENTS.md)；本地检查不替代待运行的原生 CI。
