# 模型输出与思考配置

`--max-tokens N` 请求服务为每次模型调用限制输出 token，范围 1–65,536。默认不发送此字段，继续采用服务默认配置。它与文件/命令输出字节限制 `--max-output`、请求体字节上限 `--max-context-bytes`、轮次上限 `--max-steps` 分开；不是总会话 token 或账单硬上限，输入用量、思考用量的计法及服务是否接受参数由提供方决定。

`--thinking` 显式发送 `chat_template_kwargs: {"enable_thinking": true}`，默认不发送扩展字段。当前使用的 `agnes-3.0-flash` 在[官方模型文档](https://wiki.agnes-ai.com/en/docs/agnes-30-flash)中给出了这一 Chat Completions 参数；它启用同一模型的模式，不更换模型。其他服务必须自行支持这个扩展；服务拒绝后会停止，不偷偷重试或改用其他模型。

PowerShell 的已有 Agnes 启动器原样传递这些选项：

```powershell
.\scripts\run-agnes.ps1 --tui --workdir . --max-tokens 2048
```

要显式尝试思考模式，在上面加 `--thinking`。它可能增加输出用量与延迟，当前资金约束下保持默认关闭；本批未运行真实 API 或任何其他模型。输出上限过低可能截断较长的工具参数，需按任务调整，不能用短回复当作修复完成。

在 Sprig 中，无论流式与否，只有完整、有效的最终文字或工具意图才会进入 agent 循环。推理字段不显示、不保存到会话、不发送为下一轮的历史。流式最终文字可能在结束状态到达之前已显示；服务返回 `finish_reason: length` 时仍判为未完成，截断响应里的工具动作不会执行，且不自动重试。继续对话仍需用户明确提交或 `/continue`。

选项进入统一的请求编码，因此 `/context`、压缩和 provider 发送前检查都计算它们的实际 JSON 字节。明确选择的 token 上限和思考模式也进入会话绑定；恢复时需传入相同参数，防止无意改变生成配置。未选择选项的旧会话保持原绑定格式。应用的逐次批准、只读、取消和路径限制继续独立执行。

固定仓库入口与两边的小夹具入口均支持 `--max-tokens`、`--thinking`。报告新增 `generation_options`，记录实际选定的非秘密字段。mini 通过原生 SDK 的 `max_tokens` 和 `extra_body` 传递扩展，未更换其 agent 循环。对照时须给两边相同参数；历史报告不补写新字段。缺失字段不能推断服务默认思考策略或 token 上限。

本地验证使用真实 loopback HTTP：检查普通/流式请求字段、计入预算、推理隐私、输出截断后不执行工具、CLI 到 TUI 的参数传递及会话绑定。mini 的原生 SDK 本地 HTTP 回归验证收到的顶层字段，防止参数仅存在于本地配置却被 SDK 丢弃。这些是实现和协议证据，尚不能证明真实复杂任务的成功率提升。

本批 Windows Python 3.13 运行 454 项，440 通过、14 平台跳过；WSL Python 3.12 运行 454 项，438 通过、16 平台跳过。22 项 mini SDK 本地回归、锁文件、CLI、构建、文档链接和打包一致性检查通过。远程原生 OS 检查使用本批单独手动 workflow；其结果须以实际提交 SHA 和对应 Actions 作业为准。
