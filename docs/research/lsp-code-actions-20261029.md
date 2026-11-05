# LSP 代码操作：源码依据与实施边界

2026-10-29，基线为 Sprig `555d464`。本页记录实施依据；最终功能与验证另列，不把本地协议夹具当作真实编码完成率。

继续读取固定 OpenCode `9d90d892dcbfc52b47e95b2ea31b8aba877128fd` 的 [`formatter.ts`](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/formatter.ts) 与 [`config/plugin/formatter.ts`](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/config/plugin/formatter.ts)：上游按扩展名选择 formatter，把 `$FILE` 替换为路径，通过本地进程执行原地修改；配置可组合 argv 和环境，执行时继承环境，忽略标准输入输出，失败记日志后尝试后续匹配项。这是源码阅读，没有安装或运行 OpenCode，也未据此推断所有发行版的行为。

Sprig 已有逐次批准的命令，但不把“可以运行 formatter 命令”当作完整的专用修复流程。本批先让已有 LSP 的编译器修复、重构等文本建议进入同一版本检查、完整差异、逐文件新批准与撤销后端，不复制自动原地修改或环境继承。

Microsoft 的 [LSP 3.17 CodeAction 规范](https://raw.githubusercontent.com/microsoft/language-server-protocol/gh-pages/_specifications/lsp/3.17/language/codeAction.md)允许直接返回 WorkspaceEdit，也允许声明 `dataSupport`、`resolveSupport` 后在选择时通过 `codeAction/resolve` 补算 edit；服务器不应在 resolve 时改动既有属性。请求用文件和范围定位，诊断上下文是辅助数据。协议也允许编辑后执行命令，但 Sprig 会拒绝整个带命令的操作，避免只做前半部分而假装完成。

设计为两步：先新批准查询并列出有界、不可执行的提案标识；选中后新批准计算/停止语言服务器，再分别审阅每个文件的完整差异。直接编辑绑定查询后的目标快照，延迟编辑的原始 data 只在本地保留并返回同一存活连接；不自动重启、重查或重放。源码、权限、凭据或归属变化拒绝旧提案，恢复不保留执行句柄。

复用现有 16 文件、512 TextEdit、UTF-16、BOM/换行、路径/隐私与编辑批次限制。命令、资源操作和修改注解不支持；禁用编辑、只读、取消、审批过期及部分写入仍按执行层处理。服务器本身仍有当前用户权限，不是 OS 沙箱；提案不是 read_id、授权、完整项目索引或任务验收。

## 2026-11-04 实际引擎验证后的补齐

原草稿在协议 peer 上通过，但真实 clangd 20.1.8 能报告缺少分号、codeAction 却返回空数组。继续读取 LLVM [release/20.x 的 ClangdLSPServer.cpp](https://github.com/llvm/llvm-project/blob/release/20.x/clang-tools-extra/clangd/ClangdLSPServer.cpp) 和 [ClangdServer.cpp](https://github.com/llvm/llvm-project/blob/release/20.x/clang-tools-extra/clangd/ClangdServer.cpp)：quickfix 输入使用请求里的诊断，再映射到 AST 诊断及编辑。原草稿始终发送空 context.diagnostics，协议封包正确仍不能完成真实修复。

实现改为在同一查询批准和预算下取得有界 pull/push 诊断，验证快照、UTF-16 范围与选择交集，过滤配置凭据并保留原始 data。依据 [PublishDiagnostics 3.17 的 dataSupport 定义](https://raw.githubusercontent.com/microsoft/language-server-protocol/gh-pages/_specifications/lsp/3.17/language/publishDiagnostics.md)，同时声明能在代码操作中保留该字段。版本缺失标记未确认，不把通用服务的无版本诊断拒绝成完全不可用；不发布诊断时保留预算继续请求普通操作。

新增真实 clangd 脚本不先单独查诊断，确认补分号提案、选择与文件两层批准、关闭服务器后写入、保留 BOM/Unicode/CRLF、精确撤销重做，以及重新启动并获批查询后的空诊断。另加红测证明旧实现会把 opaque data 的整数 1 与布尔 true 当作相等；现在按规范保留严格 JSON 属性身份。它们是实测修复链路和协议安全证据，不能替代模型自行选择修复或真实仓库成功率。
