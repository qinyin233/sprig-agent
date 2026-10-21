# MCP Streamable HTTP

本阶段提供实际远程 MCP 后端：明确配置的 HTTP 服务、连接/断开、工具目录和调用、资源/模板目录、资源读取、提示词目录和读取。CLI/chat/TUI 共用同一运行时。默认不连接服务器，不添加模型工具；配置后通过有界 `mcp` 网关渐进发现，避免把全部远端 Schema 放入每次模型请求。

stdio、旧 HTTP+SSE 双端点、OAuth 登录、订阅、采样、elicitation、MCP Apps 和持久后台任务仍未接入；本阶段不代表完整 MCP 功能。

## 配置和操作

在私有用户配置或可信 profile 中声明：

```toml
[mcp.servers.docs]
transport = "streamable_http"
url = "https://example.com/mcp"
protocol_version = "2026-07-28"
enabled = true
timeout = 30
deadline = 120
proxy = "system"
headers_env = { Authorization = "MCP_AUTHORIZATION" }

[tools]
mcp = true
```

`MCP_AUTHORIZATION` 按服务器要求填写，例如完整的 `Bearer ...`；不要把凭据放进 URL。也支持私有配置中的字面 `headers`。模型提供商鉴权头不会自动转发给 MCP。所有配置过的 MCP 凭据（包括未选中的 profile）参与脱敏和子进程环境过滤；Bearer token、Basic 有效载荷也被保护。

最多八个命名服务，URL 必须 HTTPS，只有 localhost/127.0.0.1/::1 可用 HTTP；端点路径和尾部 `/` 保留。拒绝 URL 用户名/密码、query、fragment 和重定向。profile 按服务名替换完整定义，项目配置不能修改 MCP。端点、版本、头名称/环境引用等绑定会话，凭据值不进入显示或绑定；配置改变需重启。

`timeout` 是网络空闲上限（0–120 秒），`deadline` 是单次 RPC 总时限（0–3600 秒），心跳不能延长总时限。`proxy` 支持 system/direct 或无凭据的 HTTP(S) 代理 origin。最多 16 个自定义头、合计 16 KiB；保留头和 `Mcp-*` 协议头不能覆盖。请求 JSON 最多 64 KiB，网关参数最多 32 KiB，响应最多 1 MiB。

`uv run --locked sprig --list-mcp` 无须模型或 API 凭据即可离线检查，不连接服务器。TUI/chat 的 `/mcp` 只显示本地状态；`/mcp connect SERVER`、`/mcp disconnect SERVER`、`/mcp tools SERVER`、`/mcp resources SERVER`、`/mcp templates SERVER`、`/mcp prompts SERVER` 分别操作连接和目录。连接及每次 RPC 都单独批准；TUI 审批中 Ctrl+L、Enter 可发送新要求，旧操作不执行，主草稿保留。

模型先通过 `mcp(action="tools", server=...)` 获取 Schema 和 `catalog_id`，再使用 `action="call"`、`name`、`arguments`、`catalog_id` 调用。`read_resource` 使用 `uri`，`get_prompt` 使用名称和字符串参数。目录的 `nextCursor` 由 `cursor` 传入，每页仍是新批准的 RPC。`/mcp result ID OFFSET` 或网关 `result` 操作只回查当前内存结果，不重复远端请求。

## 新旧版本与取消

依据 [2026-07-28 HTTP 绑定](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)和 [2025-11-25 绑定](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)，配置支持 `2026-07-28`、`2025-11-25`、`2025-06-18`，不猜版本或自动重发调用。新版使用 `server/discover`；每次请求携带 `_meta`、协议/方法/名称头，并根据有效 `x-mcp-header` 镜像静态属性链上的参数。Unicode、换行、首尾空白及 Base64 哨兵字面量按规范编码。没有初始化会话或 GET 流。

旧版使用 `initialize` 和 `notifications/initialized`，完整校验初始化结果后才确认。session ID 仅存内存，后续请求带回；本客户端要求 16–1024 个可见 ASCII 字符，拒绝不安全的过短标识。标识注册到应用运行期凭据，文件/模型请求、私有保存和子进程环境同样保护它，不只处理 RPC 结果；断开后仍保持脱敏，单个配置对象最多记住 1024 个，达到上限需重启。过期、鉴权失败、断流不自动重新初始化或重试。显式断开在新批准后发送 DELETE，允许服务器返回 405；退出、新对话和恢复仅清除本地连接/缓存，不暗中发 DELETE，旧远端会话可能等待服务器过期。

两种版本均接收 JSON 与请求范围的 SSE。必须收到匹配请求 ID 的完整 JSON-RPC 结果；重复 JSON 键、非 UTF-8、错 ID、超限、反向请求和不完整流均拒绝，最多 256 个流事件。不自动续传 SSE。取消关闭响应；旧版另尝试一次一秒期限的取消通知。取消、超时、断连不能证明服务器已停止或撤销副作用，结果保留交付可能不明，不自动重试。已发出的 RPC 不能被执行中引导改写。

## 权限、Schema 与结果

只读模式不提供模型 MCP 工具，执行层也禁止连接和 RPC，不相信 `readOnlyHint`。`[tools].mcp=false` / `--disable-tool mcp` 同时禁用模型 Schema 和执行入口，本地配置/状态仍可检查；`enabled=false` 阻止服务连接。只允许固定 MCP 方法，模型不能指定任意 HTTP URL、方法或 JSON-RPC 方法。

工具目录先校验 Schema，再提供 `catalog_id`；调用前复核目录摘要和参数。重新发现会移除旧工具，重名/无效工具不暴露，目录变化通知使缓存失效。最多缓存 64 个工具，目录分页不自动循环。Schema 为受限的 2020-12：32 KiB、1024 节点、16 层、32 个组合节点；仅支持无环本地 Schema 节点引用，拒绝网络引用、动态引用/ID、嵌套 dialect 和 `unevaluatedProperties`。正则使用有界 RE2 子集，拒绝 lookaround/backreference；参数校验另有 8192 步/一秒协作预算和取消检查。未知/不支持的 Schema 明确拒绝，不按“全部允许”处理。

批准提示列出服务、端点、协议、头名称、代理、操作和实际参数。连接批准只覆盖协议连接，不授予后续 RPC 或持久权限。MCP 服务可能写入和执行命令，Sprig 文件排除不是服务器的沙箱；应用始终执行自身的禁用、只读、取消和审批检查。

服务器 instructions、工具说明、URI、提示词角色和结果均为外部 JSON 数据，不能成为用户/系统消息、批准、`read_id` 或验收证明。不自动访问资源 URI、加载网页、下载图像或发送图像给模型；二进制块返回省略标记。`isError=true` 保留为失败。要求采样/elicitation/roots 的多轮结果停止为明确错误，不追加模型或 RPC 请求。

大结果整份脱敏后保留为当前会话内存只读句柄，最多 16 条/4 MiB，单条 1 MiB，脱敏扩张也有上限。按 UTF-8 字节分页，每页受 `max_output` 与 16 KiB 较小值限制；网络操作要求 `max_output>=1024`。回查不携带新的批准。分页 `complete` 仅表示读完记录，不证明外部操作或任务成功。恢复、新对话、退出清空连接/注册/句柄；历史句柄不可用时明确报错，不重放调用。

## 源码与验证

重读固定 OpenCode `dev@9d90d89` 的 [`mcp/index.ts`](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/mcp/index.ts) 和 [`mcp/client.ts`](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/mcp/client.ts)，对照连接状态、协议 era、目录和会话过期处理。本实现采用按次批准和渐进网关，没有移植自动重连、持久权限或隐式订阅。

测试覆盖实际双 era HTTP/SSE、三模型协议完整调用链、不完整模型响应不外连、目录/Schema/头/凭据边界、回查与恢复不重放、取消、TUI 引导和退出。拒绝重定向时的响应关闭也覆盖模型/MCP 两条路径。最终平台、打包和精确 CI 结果见[机器记录](benchmarks/mcp-http-20261021.json)；固定回复证明协议和程序行为，不证明编码成功率。
