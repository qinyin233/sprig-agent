# 工作树收尾与恢复

完成一个独立任务后，可以在另一个工作树中显式移除它。输入框 Enter 发送本地命令，路径保留内部空格和 Windows 反斜杠：

```text
/remove-worktree ../完成的任务目录
```

TUI 和 `--chat` 均支持。一次性入口不初始化模型，也不需要模型凭据：

```powershell
uv run --locked sprig --no-config --workdir E:\projects\repo --remove-worktree E:\projects\finished-task
```

管理过程不切换当前工作根或对话，不向模型请求任务，也不把目录、Git 输出或恢复引用加入模型历史。已经批准的命令仍使旧验收和文件读取标识失效；历史可收到只说明结论过期的程序观察。

## 先保留提交，再移除目录

每条 Git 命令分别批准。应用核对源仓库、完整 worktree 列表、目标注册和 HEAD、两边配置、索引、包含忽略项的状态，以及实际目录内容。只有完整读取才能继续；命令输出被截断或超限时停止，不用片段推断干净。

通过检查后，审批说明列明目标、完整提交和一个新的 `refs/heads/sprig-worktree-…` 恢复分支。先单独批准创建该分支，再单独批准非强制的 `git worktree remove`，最后批准核对登记。原有任务分支保留；detached HEAD 上的提交也由新分支持续引用。没有自动 commit、stash、merge、push、删除分支或清理脏文件。

结果只有在路径消失、登记消失及恢复引用仍指向原提交时才返回 `removed=true, verified=true`，并给出 `recovery_ref` 和 `restore_argv`。可用同一安全创建入口恢复：

```text
sprig --workdir /path/to/repo --create-worktree /path/to/restored-task --worktree-ref refs/heads/sprig-worktree-返回的标识
```

目标须不存在、父目录须存在，仍逐次批准。恢复的是已提交文件；不会自动恢复旧进程、会话、未提交修改或验收结论。恢复分支不会自动过期，可在确认无需保留之后自行审阅和删除。

## 拒绝与不确定结果

当前工作树、主工作树、其他仓库、锁定/失效登记、源目录内的目标，以及包含已配置私有存储或配置文件的目标不能移除。存在暂存、修改、未跟踪或忽略文件、额外空目录、子模块、符号链接/重解析点、冲突、`skip-worktree` 或 `assume-unchanged` 索引项时停止。只读和禁用 `run` 同样在执行层生效。

检查覆盖目标文件的身份、模式、大小、时间和完整内容摘要，以及索引、HEAD、分支引用、登记和配置。审批前后变化使提案失效；同大小、同时间戳的修改也不能复用旧批准。Git hooks、外部 clean/smudge/process filters、fsmonitor、自动维护、用户/系统配置和网络路由按[工作树管理限制](worktrees.md#批准与失败边界)处理。

目录扫描限 20,000 项、深度 64、128 MiB 文件正文、每次 15 秒；单个元数据文件限 16 MiB，合计 64 MiB。更小的配置读取限额仍适用。任何超限均停止，不自动提高预算或只检查前缀。拒绝、取消和超时不自动重试。若恢复分支已创建但未移除目录，它会保留并随结果列明；移除批准后无法可靠核对时返回不确定状态，需查看目标、恢复分支和 `/worktrees`，不会自动清理。

这不是排他锁或文件系统事务。Windows 在最终批准与复查后才释放目标目录句柄，以便 Git 删除；源目录、目标父目录及公共 Git 元数据仍保持固定。最终检查与外部 Git 执行之间仍存在竞态窗口，应停止目标上的其他编辑器、任务和后台写入后再批准。原生 CI 在 2026-11-01 前冻结，本地 Windows/WSL 检查不替代远程原生验证。

## 源码依据与验证范围

固定 OpenCode 源码 `9d90d892dcbfc52b47e95b2ea31b8aba877128fd` 的 [Git 策略](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/worktree/git.ts#L22-L26)将删除交给 Git 层，[Git 实现](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/git.ts#L1266-L1277)按 `force` 参数选择是否追加 `--force`，[服务层](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/worktree.ts#L257-L270)在策略成功后更新登记。这里只描述已阅读的固定版本，不据此推断其全部保留策略。

Sprig 对应提供本地收尾入口，并增加逐命令批准、显式恢复分支、忽略文件/索引隐藏标志检查和审批后的内容复查。Git 的普通 remove/force 语义见[官方文档](https://git-scm.com/docs/git-worktree)。测试使用一次性真实 Git 仓库、实际进程和 TUI Enter/审批流程；不使用外部模型，也不将这些结果称作编码成功率提升。完整本地测试、源码指纹和原生 Git 忽略文件对照见[验证记录](benchmarks/worktree-removal-20261026.json)。合并工作树、命令造成的任意修改恢复和 OS 沙箱仍未完成。
