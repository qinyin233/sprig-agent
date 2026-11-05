# 本地 Git 状态

在聊天或 TUI 输入 `/git`，也可以输入 `/git src` 或 `/git src/example.py`。检测到 Git 工作区时，模型可通过 `git_status(path=..., limit=...)` 请求同一个观察。Git 必须可从启动进程的 PATH 找到，每次观察都需要批准。只读模式和禁用 `run` 会阻止这条命令，也可单独在可信配置中禁用 `git_status`。

返回的路径相对于当前 Sprig 工作区。每条记录的 `index` 和 `worktree` 分别对应 Git 状态的暂存列与工作树列，空格表示该列无变化，两个问号表示未跟踪文件。`total` 只计算当前范围内可以显示的记录。`truncated` 为真时可缩小路径范围。结果说明当前的 Git 状态，无法判断修改者或变更时间。 [Git porcelain 格式](https://git-scm.com/docs/git-status#_porcelain_format_version_1)

Sprig 从普通仓库或注册的链接工作树读取有界的 HEAD、索引和忽略规则，把必要的索引信息放入临时私有目录。固定的宿主 Git 命令使用该目录的配置和索引，并读取原仓库对象。它不会加载原仓库的 Git 配置或继承 Git 路由、过滤器和外部 diff 设置。发现外部对象目录、无法验证的工作树注册，或观察期间 HEAD 或索引变化时，命令会拒绝返回状态。启用 bubblewrap 后，普通命令仍使用隔离，Git 状态继续走这条独立批准的入口。

观察使用工作树内的 .gitignore 和仓库的 info/exclude。全局忽略规则、用户或仓库的转换过滤器不参与比较。重命名按独立删除和新增显示，子模块状态不在结果内。跨 Windows 和 WSL 使用的链接工作树应在其 Git 注册所用的系统中打开。工具不提供 reftable 引用后端。

Codex 的 worktree 调用会清理继承的 Git 路由，并禁用 hooks、fsmonitor 和已配置的内容过滤器。OpenCode 用独立快照存储记录内容树，再按指定路径比较两个快照。Sprig 这里借用独立索引的思路观察实时 Git 状态，原索引留在原处。 [Codex 源码](https://github.com/openai/codex/blob/c0100ad4ecd2251c2b48b68534db08afac78c606/codex-rs/worktree/src/git.rs) · [OpenCode 源码](https://github.com/anomalyco/opencode/blob/893e8dc43f05e872d4cac603486ce8a7181170fa/packages/core/src/snapshot.ts)
