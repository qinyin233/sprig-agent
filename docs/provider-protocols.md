# 模型协议

在可信用户 `~/.sprig/config.toml` 的供应商表中选择协议；也可用 `--wire-api` 临时覆盖。默认 `chat_completions` 保持既有请求格式。模型名、服务地址、可用性和支持的生成参数由供应商决定，Sprig 不自动切换协议、模型或重试。

| `wire_api` | 在 `base_url` 后追加 | 默认认证 | 工具与流式完成标记 |
| --- | --- | --- | --- |
| `chat_completions` | `/chat/completions` | `Authorization: Bearer …` | function tools、`finish_reason` 和 `[DONE]` |
| `messages` | `/messages` | `x-api-key: …` | `tool_use` / `tool_result`、`message_stop` |
| `responses` | `/responses` | `Authorization: Bearer …` | `function_call` / `function_call_output`、`response.completed` |

```toml
[providers.anthropic]
wire_api = "messages"
base_url = "https://api.anthropic.com/v1"
model = "YOUR_MODEL_ID"
api_key_env = "ANTHROPIC_API_KEY"

[providers.anthropic.generation]
max_tokens = 4096
# thinking = true  # Messages: adaptive thinking; requires model support
# Older models can instead use extra_body.thinking = { type="enabled", budget_tokens=1024 }

[providers.openai]
wire_api = "responses"
base_url = "https://api.openai.com/v1"
model = "YOUR_MODEL_ID"
api_key_env = "OPENAI_API_KEY"

[providers.openai.generation]
max_tokens = 4096 # becomes max_output_tokens
# reasoning_effort = "medium" # becomes reasoning.effort
```

Messages 自动发送 `anthropic-version: 2023-06-01`；可通过供应商 `headers` 显式指定版本。两种认证字段 `api_key_header` / `api_key_scheme` 默认 `auto`，按协议解析；需要 Bearer 的 Messages 网关可显式设置 `api_key_header="Authorization"` 和 `api_key_scheme="bearer"`。`base_url` 必须包含服务要求的 API 前缀（常为 `/v1`），不包含最终方法名。

## 探测已配置的模型

```powershell
uv run --locked sprig --provider USTC --model deepseek-flash --probe-model
```

也可在 TUI 或 `--chat` 中输入 `/probe-model` 测试当前模型。确认前会显示协议、完整请求端点、模型名和请求上限。CLI 从交互式终端输入 `y` 或 `yes` 批准。四种检查采用独立的固定输入，依次验证 JSON 文本、SSE 文本、JSON 工具调用和 SSE 工具调用。文本检查要求完整的非空回复，流式文本还要求观察到正文增量；工具检查要求完整解码出一个指定诊断调用及预期参数。工具调用只解析，永不执行。工具参数增量分别由现有 Chat Completions、Responses 和 Messages 解析器处理。[OpenAI 工具调用](https://developers.openai.com/api/docs/guides/function-calling) [Anthropic Messages SSE](https://platform.claude.com/docs/en/build-with-claude/streaming)

探测沿用当前鉴权、代理和生成设置，包括已配置的 `extra_body`。每次最多请求 1024 个输出 token，每次期限最多 30 秒，较小的配置上限优先。关闭自动重试。鉴权、权限、限流、证书校验或重定向错误会停止余下检查。取消也会保留已观察到的结果。探测请求可能产生费用，模型用量字段缺失时仍显示未知。

请求只使用固定提示和已配置的请求设置。当前任务、会话历史、附加文件和工作区指令不进入探测。报告只显示检查结果、失败类别和受限的数值统计，省略回复正文和工具参数。`passed` 表示本次观察通过，`failed` 表示本次失败，`not_run` 表示尚未发送该项检查，`cancelled` 表示检查中断。一次结果只适用于检测时的配置和请求上限。命令行退出码为 `0`（四项通过）、`2`（拒绝、失败或未完成）或 `130`（取消）；配置错误仍返回 `1`。

`max_tokens` 在 Messages 中必需，未配置时为 4096；Responses 映射为 `max_output_tokens`。`thinking=true` 在 Chat Completions 中仍为 `chat_template_kwargs.enable_thinking`，Messages 为 `thinking.type=adaptive`，Responses 为 `reasoning.effort=medium`（显式 effort 优先）。Messages 的 `reasoning_effort` 映射为 `output_config.effort`。`seed` 和 `token_limit_field` 只适用于 Chat Completions，其他协议会在本地拒绝。不同服务接受的参数值可能不同，配置它们不会触发自动探测或收费重试。

三种协议均支持正文、多个客户端函数调用、工具结果续传、流式文本、配置的文件上下文和固定检查。Messages 把 system 提取到顶层，把连续工具结果合入 user 内容块；Responses 把结果绑定到 `call_id`，展开函数定义并显式设置 `strict=false`，保留现有可选参数语义。没有接入供应商托管的 shell、搜索、文件上传、图像、音频或远程会话：文件使用现有受限 UTF-8 快照和引用路径，不能绕过隐私或大小限制。

Responses 始终 `store=false`，不创建 `previous_response_id` / conversation 链，并请求 `reasoning.encrypted_content`。Messages 的 thinking/signature/redacted_thinking 和 Responses 的完成态 reasoning/items 作为隐藏原生状态随历史续传，流式初始块中的部分 encrypted_content 不会取代完成块。原生状态与助手正文和函数调用逐项校验，不能包含另一套执行意图，也不具有批准效力。Chat Completions 的 `reasoning_content` 仍需显式 `preserve_reasoning=true`。

原生状态会占用请求和已启用会话保存的大小预算；保存文件私有但不加密。协议加入会话绑定，恢复时不能换协议。上下文压缩可缩短旧工具结果，但不改写携带原生状态的助手项或其参数，也不折叠这些调用；超限时停止发送，保存历史保持完整。

读取和大文本处理沿用统一限制：实际原生请求体的 UTF-8 字节参与发送前计量，响应及 SSE 整体最多 2 MiB。正文可提前显示，完整终止标记、状态和函数参数验证完成后才保存或执行意图。截断、出错、取消、过期引导、未知执行类输出或不一致的完成项都不能触发工具。写入和命令继续逐次批准，没有新增网络重试或命令沙箱。

Chat Completions 兼容网关将同一工具的纯参数续片合入单条 SSE 消息的情况：在保留原索引与身份的前提下按顺序拼接参数，重复声明身份、名称或不一致类型仍拒绝。完整结束标记、调用数量与响应大小限制继续生效；没有供应商或模型名称分支。

用量归一化为已有 prompt/completion/total 字段；缺失值保持未知。Messages 的普通输入、缓存读取和缓存创建 token 合计为输入量；没有报告 total 时不推算。延迟、首段可见文本时间、请求/响应字节在三种协议下使用同一测量实现，不把推理事件当作可见首 token。

离线回归：`uv run --locked python -m unittest tests.test_native_protocols -q`，使用严格回环 HTTP 和逐字节 SSE，覆盖两种原生协议的多轮工具、认证、Unicode、隐藏状态、批准/拒绝/只读、取消、截断、会话、检查与请求预算；既有 Chat Completions 测试一并保留。协议测试不代表某家服务或模型的任务成功率。

协议依据：[OpenAI Responses 迁移指南](https://developers.openai.com/api/docs/guides/migrate-to-responses)、[Responses 流式事件](https://developers.openai.com/api/reference/resources/responses/streaming-events)、[Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create)、[Messages 流式说明](https://platform.claude.com/docs/en/build-with-claude/streaming)。
