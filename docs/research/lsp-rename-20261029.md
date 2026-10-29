# 符号重命名的协议与副作用边界

2026-10-29，Sprig 基线 `aca256916a72e6666e8e75fd2f4c980eb6ef3047`。读取既有 OpenCode `v1.18.34@aec0b9a6d8898f68f923aaf08b7306d931fd9d76` 的 [`tool/lsp.ts`](https://github.com/anomalyco/opencode/blob/aec0b9a6d8898f68f923aaf08b7306d931fd9d76/packages/opencode/src/tool/lsp.ts)：操作枚举包括定义、引用、悬停、符号、实现及调用层级，没有 rename。这里仅描述这一固定工具入口，不推断其他版本、编辑器前端或整个项目不支持重命名；没有执行上游。

Microsoft LSP 3.17 的 [`textDocument/rename`](https://raw.githubusercontent.com/microsoft/language-server-protocol/gh-pages/_specifications/lsp/3.17/language/rename.md) 接收位置和新名称，返回 WorkspaceEdit 或 null。prepareRename 是协商能力，返回 null 表示当前位置不能重命名。服务器计算提案，客户端负责应用；RPC 成功本身不是文件写入批准。

[`WorkspaceEdit`](https://raw.githubusercontent.com/microsoft/language-server-protocol/gh-pages/_specifications/lsp/3.17/types/workspaceEdit.md) 可用 changes 或 documentChanges，后者存在时优先。文档版本、资源操作、失败处理和变更注解分别协商。[`TextDocumentEdit`](https://raw.githubusercontent.com/microsoft/language-server-protocol/gh-pages/_specifications/lsp/3.17/types/textDocumentEdit.md) 的编辑按同一旧版本计算且不得重叠。Sprig 应声明实际的 abort 行为，不能把逐文件写入称为跨文件事务。

Sprig 的设计是支持已有文件的文本重命名，并复用既有独立文件批次预检、具体差异批准和私有撤销备份。拒绝越界/私有/重复别名目标、未协商的资源操作和注解；不丢掉不安全目标后继续执行剩余修改。对服务器报告的文档版本核对已同步版本和摘要；未标版本的目标在接收提案时读取并绑定当前完整字节，明确不能据此证明服务器索引对应同一快照。

结果准备受 RPC 总期限约束，完成后关闭该服务器连接，再逐文件批准和重新检查全部待修改文件。拒绝、取消、错误或不确定写入停止后续修改，保留已经写入的前缀及恢复记录。成功重命名应进入应用既有编辑后检查路径；固定验收仍独立执行，重命名或语法观察不代表语义正确。

这些是实施约束。最终交付需有三协议、TUI、本地平台回归和真实语言引擎的分开证据；标准库协议 peer 不充当语义引擎。11 月 1 日前继续冻结 CI，所有提交使用 `[skip ci]`。
