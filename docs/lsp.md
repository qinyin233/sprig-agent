# 语言服务器语义查询

Sprig 可通过明确配置的本地 LSP stdio 服务器查询定义、引用、实现、悬停说明、文件/工作区符号和诊断。语法地图仍适合快速浏览；LSP 的结果由所选语言服务器提供，能够依据类型、命名空间和项目设置解析符号。没有安装器、自动发现或自动启动。

## 配置与终端入口

在用户 `config.toml` 或可信 profile 中配置已经安装并审查过的可执行文件：

```toml
[lsp.servers.cpp]
command = ["clangd", "--log=error", "--background-index=false", "--enable-config=false"]
languages = { ".cpp" = "cpp", ".hpp" = "cpp" }
cwd = "."
timeout = 15
deadline = 60

[tools]
lsp = true
read_file = true
```

Windows 可将 command 第一项写为已安装 `clangd.exe` 的完整路径，使用 TOML 单引号字符串或转义反斜杠。argv 直接执行，不隐式调用 shell；批处理需要明确的解释器 argv。语言服务器仍可能调用编译器、读取依赖和编译配置，须了解其权限及项目配置行为。示例关闭 clangd 后台索引和 `.clangd` 配置加载，其他服务器的选项各不相同。

服务器表支持 enabled、固定 argv、后缀到 language ID 的映射、工作区相对 cwd、env 字面值、env_from 秘密环境变量引用、空闲 timeout 和整次操作 deadline。最多八个服务器，每个最多 32 个后缀。用户表与 profile 按服务器名称合并；项目配置不能声明服务器或注入执行环境。`--show-config` 显示脱敏的本地配置，加载配置不启动进程。

TUI 和 `--chat` 均提供本地入口，不请求模型：

```text
/lsp
/lsp definition cpp "src/main.cpp" 12 8
/lsp references cpp "src/main.cpp" 12 8
/lsp implementation cpp "src/main.cpp" 12 8
/lsp hover cpp "src/main.cpp" 12 8
/lsp symbols cpp "src/main.cpp"
/lsp workspace_symbols cpp "choose"
/lsp diagnostics cpp "src/main.cpp"
/lsp rename cpp "src/main.cpp" 12 8 "new_name"
/lsp code_actions cpp "src/main.cpp" 12 8
/lsp code_actions cpp "src/main.cpp" 12 1 14 8
/lsp apply_code_action lsp-action-<ID>
/lsp result lsp-<ID> 0
/lsp disconnect cpp
```

行、列均从 1 开始，列按 Unicode 字符计算；内部协商 UTF-16 并处理代理对和 CR/LF 换行。模型工具 `lsp` 提供相同操作，三种模型协议共用后端；文件查询可以省略 server，只有后缀恰好匹配一个启用服务器时才自动选择。工作区查询必须明确 server 和 query，不接受 path 或行列。默认无服务器时不向模型提供该工具。

## 跨工作区查找符号

`/lsp workspace_symbols cpp "choose"` 与模型工具的 `action="workspace_symbols", server="cpp", query="choose"` 使用标准 `workspace/symbol`。查询最多 256 个 Unicode 字符/1024 个 UTF-8 字节，拒绝控制字符和已配置凭据；`""` 可以请求全部符号，仍受结果数量和总时限限制。模糊匹配、排序与索引覆盖由服务器决定，不自动放宽或重试查询。

这项操作不需要预先选择文件。首次批准涵盖启动已配置的服务器、初始化和本次工作区查询；已有连接只刷新至多四份先前明确打开的文档，并在批准前列出这些快照。不会为了查询自动遍历或向服务器同步整个仓库。仅支持工作区符号、没有文档同步能力的服务器也可查询，文件级操作仍要求同步能力。

结果返回名称、种类、容器说明、允许读取的工作区相对路径、转换后的行列和实际文件字节摘要。容器说明不重建层级。私有、越界、非文件 URI、非法位置、缺少位置范围等记录被省略，不输出对应名称；服务器附带的任意 data、命令和链接不转发执行。后续编辑必须重新读取当前源码，摘要不是 read_id 或批准。

客户端声明静态符号查询及支持的种类，不声明延迟 `resolveSupport`。依 [LSP 3.17 规范](https://github.com/microsoft/language-server-protocol/blob/gh-pages/_specifications/lsp/3.17/workspace/symbol.md)，服务器此时应返回带完整位置范围的结果；客户端不隐式追加 `workspaceSymbol/resolve`。同时接受带范围的 `SymbolInformation` 和 `WorkspaceSymbol`，未知种类回退为 null。无匹配项不证明整个项目不存在该符号，所有结果保持 `complete_project_index=false`。

## 批准、同步和结果

每次语义查询重新批准，首次批准同时涵盖进程启动、initialize、所列源码同步和本次查询。批准展示准确 argv/cwd、环境名称、源码摘要、位置与预算。服务器具有当前用户的文件系统和网络权限，**不是 OS 沙箱**，可能在初始化、查询间隙或读取项目配置时产生副作用。应只启用可信服务器；Sprig 只允许固定的客户端操作，不能阻止服务器自身的全部系统行为。

执行层拒绝只读模式、关闭 read_file/lsp、私有路径、链接、非 UTF-8/二进制、凭据形态及已知密钥正文。使用独立最小环境，不继承模型密钥或普通命令的环境覆盖，只传递必要系统变量和明确声明的 env/env_from。批准后再次检查路径、执行目录的开发指令和源码摘要；查询期间源码改变，关闭连接并丢弃陈旧结果。CLI/TUI 均按实际批准事件使旧验收失效，获批后返回错误或取消也不能沿用此前的成功状态。

每个服务器最多同步四份明确打开的源码；变更增加文档版本，按服务器协商发送 full 或 incremental didChange，淘汰时发送 didClose。单份源文件最多 512 KiB。结果只保留当前工作区允许读取的本地文件位置，过滤私有/越界/网络 URI，携带源码摘要和转换后的行列；不生成编辑 read_id，不表示整个项目索引已完整。

支持静态 pull diagnostics，以及版本匹配的 push diagnostics。过期版本丢弃；无版本报告标明 version_confirmed=false。没有收到报告会标为未知或超时，不能把缺少诊断当作检查通过。诊断及服务器说明都是外部数据，不能代替 Sprig 的固定验收命令。

客户端主动请求的[跨文件符号重命名](lsp-rename.md)在计算提案后关闭服务器，再逐文件批准完整差异并复用普通编辑的备份与版本门禁。[代码操作](lsp-code-actions.md)先结合当前诊断列出普通文本修复/重构提案，选中后重新批准，支持同一连接的延迟解析，再关闭服务器并逐文件审阅。服务器请求的编辑、命令和动态注册均拒绝；workspace/configuration 只返回空值，progress/create 返回空响应。结果中的 Markdown、命令、链接和数据字段均不执行。没有 formatting、executeCommand、动态能力注册、工作区文件监视、调用层级和延迟符号解析；服务要求这些能力时可能不可用。尚无语义检索数据库或完整调用图。

## 限额、取消和恢复

Content-Length 按 UTF-8 字节解析；单帧 1 MiB、头 8 KiB，每次最多 256 条通知/服务端请求，连接 stdout 共 32 MiB、stderr 共 8 MiB，原始 stderr 丢弃。一次最多 512 条位置/符号记录、64 份目标文档。整次批准后的初始化/同步/查询、结果规范化及首次分页共用 deadline，持续输出不能重置总期限。失败、断流或超限关闭受管进程，不重试或重放。

结果只存内存，最多八个 1 MiB 快照，JSON 页不超过 min(max_output,16384)，查询要求 max_output>=1024。offset 是 UTF-8 字节位置；分页不请求服务器，每次复查文件隐私与凭据保护，历史快照明确标注 historical。旧结果不是新的读取授权或验收。

取消正在执行的查询会尝试发送 cancelRequest 并关闭归属进程；disconnect 单独确认。新对话、恢复、模型切换、历史分支和正常退出停止 LSP 进程、清除内存句柄。私有会话可能保留已看到的文字，恢复不会重新连接或执行历史操作。复用现有 Windows Job Object/POSIX 进程组后端；本地 WSL 通过不代表原生 Ubuntu CI、macOS 或全部 Python 版本已验证。

离线回归使用临时 stdlib LSP peer，覆盖审批、协议和界面，不能据此宣称有真实语义引擎。已有 clangd 时，可在临时 C++ 项目验证同名函数、引用和修改后类型诊断：

```text
uv run --locked python scripts/verify-lsp-clangd.py --clangd <已安装的可执行文件> --approve-disposable
```

该显式选项仅批准脚本硬编码的六个临时 fixture 查询，不安装程序或调用模型。[本批记录](benchmarks/lsp-20261022.json)区分真实 clangd、协议 fixture、本地平台与远端 CI；按用户要求，2026-11-01 前所有 CI 暂停。

追加 `--workspace-symbols` 会再批准一次第二文件的符号查询和一次跨文件查询，共八次固定操作。本机实测找到两个文件、三个命名空间中的同名函数，并继续验证修改后的第二版类型错误；不推断完整仓库覆盖或模型编码收益。工作区扩展的 14 项新增回归还覆盖三种模型协议的查找→读取→版本编辑→独立检查、80×24 TUI 的 Enter/拒绝/重新批准和本地 CLI 无模型调用，完整范围见[本批记录](benchmarks/lsp-workspace-20261023.json)。
