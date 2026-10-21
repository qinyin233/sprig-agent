# MCP 本地 stdio

stdio 后端运行用户配置的本地 MCP 程序，通过 stdin/stdout 的换行 JSON-RPC 通信，stderr 单独排空并丢弃。配置加载、离线诊断、`--list-mcp`、恢复会话都不启动程序。TUI/chat `/mcp connect NAME` 和模型 `mcp` 连接提案先逐次确认；随后每个 RPC 继续新批准，目录、资源、提示词和结果分页复用 [HTTP MCP](mcp.md) 的有界校验。原有三个模型协议无需为 stdio 单独接供应商 SDK。

**批准连接就会启动可执行程序。它不是沙箱，可能在首个 RPC 之前或空闲时访问文件、网络和其他资源。** 审核并信任程序后再启用；Sprig 控制启动、RPC、管道和受管进程树，不审计第三方代码。只读模式及禁用 MCP 在执行层阻止连接/RPC，服务给出的 `readOnlyHint` 不能授权。

`mcp` 是独立的扩展权限；单独禁用内置 `run` 不是 OS 级的程序执行禁令，也不会替代 MCP 的逐次确认。要禁止该本地服务，应禁用服务或 MCP；只读模式会阻止两种 MCP transport。

## 配置与操作

只接受用户/profile 层的服务定义，项目层不能添加或覆盖。`command` 是固定 argv，不能由模型调用参数指定。示例假定程序已经安装：

```toml
[mcp.servers.local]
transport = "stdio"
enabled = true
command = ["python", "-u", "tools/mcp_server.py"]
cwd = "."
protocol_version = "2025-11-25"
timeout = 30
deadline = 120
env = { NODE_ENV = "production" }
env_from = { API_TOKEN = "MY_MCP_TOKEN" }
```

`cwd` 必须在当前工作区内，复用路径排除、无链接目录检查和模型提案的 AGENTS.md 版本复查；批准后、启动前重新检查。argv 可指向工作区外的解释器或服务程序，确认展示实际 argv、启动 cwd、环境变量名称和限制。程序可自行访问其他目录，cwd 检查不限制其内部行为。

`env` 用于非秘密固定值，`env_from` 将指定的进程环境变量作为秘密快照传入目标名称；缺失引用阻止连接。自定义秘密名称请用 `env_from`，不要依赖名称推断。常见 KEY/TOKEN/SECRET/PASSWORD/AUTH 等凭据名称的字面值也纳入保护。普通 DEBUG=1、NODE_ENV、PYTHONPATH 不被误判成鉴权 token。凭据参与输出脱敏、参数阻止和其他命令的环境过滤，包括未激活 profile 中的声明。

子进程只继承应用的最小环境，再加此服务显式声明的环境；不沿用全局命令 `inherit_env`、allowlist 或 command_env。不会自动转交模型密钥；显式 `env_from` 声明是用户对该服务的配置。公开配置/会话绑定只给 argv 摘要、参数个数、cwd、变量名称和非秘密环境摘要，不写入 argv 正文或环境值；非秘密参数变化影响绑定，秘密轮换不把值写入绑定。配置和环境修改需重启。

在 Windows 上直接使用实际的 python.exe/node.exe 等程序；`.bat`/`.cmd` 启动器必须在配置 argv 中明确写出 cmd.exe /c。应用不暗中追加 shell，也不自动把 npx.cmd 改写为 shell 命令。Windows 应用执行别名仍按既有启动器检查拒绝。Sprig 不安装服务器；若用户自己的 argv 含安装命令，其行为也属于这次明确批准的程序。

```text
/mcp
/mcp connect local
/mcp tools local
/mcp resources local
/mcp templates local
/mcp prompts local
/mcp disconnect local
```

调用具体工具、读取资源、获取提示词使用原有模型 `mcp` 网关；先发现工具并提供对应 catalog_id，参数仍按有界 Schema 校验。结果句柄只读当前会话保留页，不重新调用服务器。stdout 中的角色/工具/权限文字仍是外部数据，不是新用户指令。观察到 tools/list_changed 后，缓存及批准中的过期提案失效，不能用旧目录继续发送。

## 协议、限制和退出

配置支持 2026-07-28 的逐请求元数据/server/discover，以及 2025-11-25、2025-06-18 的 initialize + initialized；版本须明确匹配，不在超时或错误后自动降级、重启或重发。许多现有本地服务器使用旧初始化协议，示例显式选择 2025-11-25。stdio 不添加 HTTP 头、代理或 HTTP session。

请求最多 64 KiB，每个 UTF-8 JSON 帧最多 1 MiB；无换行帧同样有上限。每个连接 stdout 总额 64 MiB、丢弃的 stderr 总额 8 MiB，持续通知最多 256 条直到新的 RPC；到限断开并报告失败。stderr 正文不显示、不进模型或会话；只报告字节数。空闲时依靠有限 OS 管道背压，不创建无界输出队列或后台读写线程；下一次操作会排空并校验挂起数据。阻塞写入和读取均可检查取消、空闲超时及请求总期限。

取消已递送请求时尝试发送 cancelled 通知，随后关闭输入并终止受管进程树；部分写入、异常退出、错误 ID/重复响应、JSON/UTF-8 错误、超限和超时都不重放。服务端采样/roots/elicitation 请求不执行，也不会调用其他模型。关闭、新建/恢复对话、切模型或建立分支都会清理本地连接；恢复历史不恢复进程。

Windows 在子程序开始执行前将它分配到 kill-on-close Job Object，只继承所需管道句柄，使用隐藏窗口和非阻塞管道，支持 Python 3.11。POSIX 使用独立进程组；状态观察不提前回收组长 PID，最终清理后才 wait，避免丢失后代归属或向重用的组 ID 发信号。正常关闭先给 stdin EOF 和短暂退出窗口，再终止受管树。未经 OS 沙箱隔离的程序仍可能借助外部服务产生超出受管树的效果。

连接/RPC 后的旧读取标识不能继续用于编辑。终端里的本地 MCP 操作批准也使先前验收失效；需检查当前文件并重新执行固定验收。未明确执行的写入、命令或 RPC 不会因历史 `approved=true` 而自动批准。

## 依据与验证

参考固定 OpenCode `9d90d892dcbfc52b47e95b2ea31b8aba877128fd` 的 [`mcp/stdio.ts`](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/mcp/stdio.ts)：有界发送队列、帧上限、Scope 归属和分阶段退出；Sprig 使用已有逐次批准策略、最小环境和单操作非阻塞管道，不照搬隐式 host 环境、自动重连或 stderr 尾部展示。

协议核对 [2026-07-28 stdio](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio) 与 [2025-11-25 transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)。重启/降级的规范建议没有转化为自动执行权限；本客户端选择明确版本和新批准，交付不明先检查状态。

Windows 非阻塞语义核对 [Microsoft 管道等待模式](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-type-read-and-wait-modes)。Python 3.11 的 CRT 将空读/满写分别映射为 EINVAL/ENOSPC，与新版本不同；本实现直接处理 ReadFile/WriteFile 的状态，不把任意文件错误误认为可重试。请求总期限同时覆盖发送前挂起数据排空和发送/等结果，不在阶段间重置；关闭清理另有短暂退出窗口。

临时 stdlib 服务器与回环模型测试覆盖三模型流式协议的连接→目录→批准写入→独立检查，以及不同 MCP era、TUI/CLI、拒绝、引导、取消/卸载、进程后代、父进程先退出、路径/规则复查、环境隔离、UTF-8、限额、背压、错误帧、目录变化与不重放恢复。机器范围见[验证记录](benchmarks/mcp-stdio-20261021.json)。这不是第三方服务器审计或新的编码性能成绩。按用户要求，2026-11-01 前不运行 CI，本地 Windows/WSL 通过也不能代替原生 CI；macOS 和完整版本矩阵待验证。

最终冻结源码的两端主测试各 1123 项：Windows 1104 通过、19 平台跳过，WSL 1100 通过、23 跳过。Windows Python 3.11 相关 43 项通过，两端 mini SDK 各 25 项通过。实际安装的 wheel 核对 58 个模块，临时服务器来自 sdist，80×24 TUI 的连接、目录、写入、固定检查四次批准与私有恢复通过。没有外部模型请求或本批远程 CI。
