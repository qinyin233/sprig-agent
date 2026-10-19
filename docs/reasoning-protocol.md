# 推理模型的工具历史兼容

有些 Chat Completions 服务需要把 `reasoning_content` 随助手消息继续发送。当前 [DeepSeek 思考模式文档](https://api-docs.deepseek.com/guides/thinking_mode/)要求带工具的后续请求回传这些字段，包括没有调用工具的先前回复；缺失会返回 400。其[工具文档](https://api-docs.deepseek.com/guides/tool_calls/#inserting-tool-calls-mid-conversation)还说明 Chat Completions 不支持客户端在历史中插入工具调用。这些是所引服务的协议要求，不能推断所有兼容网关都有相同限制。

Sprig 新增两个显式的提供商设置。默认行为保持不回传推理字段、使用原工具检查历史；不自动开启模型思考或切换模型，不发起能力探测或重试。

| 提供商设置 | 默认 | 作用 |
| --- | --- | --- |
| `preserve_reasoning` | `false` | 开启后保留完整文本、空串或 null 的 `reasoning_content`，用于后续请求和已启用的私有会话保存；不显示正文 |
| `check_history_format` | `"tool_calls"` | 设为 `"observations"` 时，将应用固定检查投影为明确标注的用户数据消息，模型自己发出的真实工具调用保持原样 |

例如，针对遵循上述 DeepSeek 协议的提供商，可在私有配置中选择：

```toml
[providers.reasoned]
model = "your-model-id"
base_url = "https://api.deepseek.com"
api_key_env = "DEEPSEEK_API_KEY"
preserve_reasoning = true
check_history_format = "observations"

# 仅在确实希望开启服务端思考时选择其协议参数。
[providers.reasoned.generation.extra_body.thinking]
type = "enabled" # 或按服务契约明确 disabled
```

用原有 `provider` / profile 选择该提供商；网关地址、模型 ID 和开关应按服务自己的契约设置。现有 `--thinking` 请求的是 `chat_template_kwargs.enable_thinking`，不等价于所有提供商的原生 thinking 参数；上述 `extra_body` 才是该示例采用的格式。兼容开关本身不写入服务端请求的顶层参数，也不改变输出 token 或请求字节上限。

CLI 可用 `--preserve-reasoning` / `--no-preserve-reasoning` 和 `--check-history-format observations` 覆盖。`--show-config`、TUI `/config` 显示生效值和来源，`--doctor` 仍完全离线。提供商字段可由用户配置、profile 选择提供商或 CLI 决定；显式项目层不能自行开启它们。

## 数据、预算和恢复

SSE 按顺序累积推理字段，直到完整成功响应才进入历史；不从该文本提取工具、不把它当作最终答案，也不当作用户批准。终端、实时输出及 TUI 恢复历史只显示普通回复。推理字段中的已配置凭据继续脱敏；类型错误、非法 Unicode、超过响应上限、输出 token 耗尽、断流或取消都不能执行该响应中的工具意图。

开启后，保留字段计入完整请求字节预算，并进入显式启用的私有未加密会话；会增加上下文和保存体积，不能据此推断费用减少。压缩可以继续缩减原工具正文，但不会删除、截断或伪造推理字段；此模式也不将旧工具调用合并成新助手账本。超限时在联网前停止，不通过自动丢弃协议数据或付费重试凑合发送。会话保存的 8 MiB 上限同样适用，保存失败发生在后续副作用之前。

两个设置都绑定会话。切换后不能直接恢复原设置的历史，以免向不兼容服务发送字段或静默改变私有数据处理。关闭保留时，默认 Provider 回复、发送正文和终端不包含该字段；手工传入请求中的系统、用户或工具消息也不会携带助手专属推理元数据。已经保存的旧文件不会因切换设置自动清理。

## 应用检查的观察格式

固定基线、编辑后检查、最终验收仍执行原有独立 argv，并逐次取得新批准。原始内存/保存历史保留规范工具调用、结果、顺序和应用来源标记 `sprig_check`；加载仍不执行动作。发送前才按明确来源标记将一对应用检查消息转换为两条用户数据：第一条标明阶段、ID 和完整参数 JSON，第二条保留原结果 JSON。模型自己产生的工具调用不转换，也不根据调用 ID 前缀猜测来源。

模型返回的伪造来源标记会丢弃；保存历史中的无效标记、缺失或错序结果会拒绝。应用标记不证明历史真实，不授予新批准；独立最终验收仍重新执行。默认 `tool_calls` 格式只移除内部标记，不改变原有服务请求格式。

转换复用结果字符串，不为长日志额外生成一份嵌套 JSON；观察格式及 JSON 转义都计入实际请求预算。失败、拒绝、输出截断、不确定和取消记录继续保留；用户消息形式不意味着检查结果成为新任务。

## 验证范围

一次性回环服务对每个助手消息检查推理字段，并拒绝客户端插入的工具调用；用相同约束分别验证 HTTP 与 SSE。默认丢字段可复现 400 且不自动重试，显式配置后可完成读取、版本关联编辑、失败基线、阶段检查、独立最终检查及下一轮对话。另验证私有保存/恢复、不显示正文、脱敏、取消、执行中引导、预算拒绝、字段类型和来源伪造。它们是协议夹具，没有实际付费模型成功率或原生服务接受结果，不能证明编码性能已经达到目标。

2026-10-19，功能提交 `2a78f62eb568e647afabc6977146850153a17097` 新增 26 项回归。Windows Python 3.13.0 主测试共 875 项，858 通过、17 平台跳过；WSL Ubuntu Python 3.13.14 共 875 项，853 通过、22 平台跳过。两端 mini SDK 各 25 项通过；跳过项不算通过，WSL 不是远程原生 runner。锁文件、Schema、wheel/sdist 与全新 wheel 安装验证通过，全部 36 个 Python 文件与源码一致；安装后的 HTTP/SSE 推理透传、检查观察格式、私有保存与不显示正文也通过。

严格夹具在默认丢字段模式下两次请求复现 400、零写入；显式配置后 HTTP 和 SSE 各四次请求、六次新批准，均在 32768 字节预算内完成上述两轮链路。工具定义仍为 4920 字节。该兼容修复使用 `[skip ci]` 保存 Actions 额度，**本提交未运行远程 CI**，不引用父提交的绿色结果。数值和边界见[验证 JSON](benchmarks/reasoning-protocol-20261019.json)。
