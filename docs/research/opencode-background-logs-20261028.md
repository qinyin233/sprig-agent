# OpenCode 后台输出与日志恢复源码核对

2026-10-28，只读核对 OpenCode 固定提交 `fa1260d6ca484fe0fd868aa992ad81ab85343cf3`，以及 Sprig 基线 `3877e3de07cdb1884f2c78b1bfac20246b501634`。下述是这些路径的源码事实和设计推论，没有运行上游、调用模型或触发 CI；不声称涵盖其他版本、所有调用入口或实际编码成绩。

## 输出留存与终态发布

OpenCode 的 `Shell` 服务为每个命令创建一个 `.out` 文件，位于应用数据目录的 `shell/<project-id>/` 下。命令的 stdout/stderr 合流块持续写入这个文件，同时累计原始字节数与换行数；因此不是只有展示被截断后才开始留存。这个写入路径直接接收字节块，所读片段没有 Sprig 的按配置凭据脱敏步骤；不能据此推断项目其他层完全没有过滤。[目录与索引](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/core/src/shell.ts#L141-L147)、[输出路径](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/core/src/shell.ts#L287-L301)、[采集与关闭](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/core/src/shell.ts#L333-L355)

进程退出和超时竞争记录结束原因；终态发布还要等待输出流关闭，然后才更新状态、唤醒等待者和发出退出事件。这个顺序使服务观察到终态时，正常采集链已结算，但不能据此证明磁盘耐久性、写入错误均已被辨认，或外部副作用成功。[终态发布顺序](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/core/src/shell.ts#L389-L433)

后台工具先返回 shell ID 与输出文件路径；对应 Job 结束后才把完成、错误或取消反馈作为 synthetic 会话消息送达。后台默认超时可以为零，前台被转入后台的分支也可以清除超时。这些是上游选择，不适合直接替换 Sprig 的逐次批准和固定期限。[后台结果](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/core/src/tool/plugin/shell.ts#L96-L101)、[完成通知](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/core/src/tool/plugin/shell.ts#L157-L186)、[启动与转后台](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/core/src/tool/plugin/shell.ts#L198-L266)

## 分页与展示截断

`Shell.output` 必须先在当前服务的内存索引中找到命令，然后按文件字节偏移读取；默认一页 65536 字节，返回下一 cursor 与已采集总字节数。每页独立使用 UTF-8 解码，所读实现没有跨页码点拼接状态；不能将其字节 cursor 当作 Sprig 现有字符偏移日志接口直接复用，也不能从返回的 `truncated=false` 推断已读完，客户端还要比较 cursor 与 size。[分页实现](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/core/src/shell.ts#L208-L235)、[分页数据类型](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/schema/src/shell.ts#L70-L84)

`Shell.result` 在命令结束后取受字节和行数限制的尾部；默认值来自 `ToolOutput` 的 50 KiB / 2000 行，配置可以覆盖。预览被截断时附完整输出文件位置。通用 `ToolOutput.truncate` 是另一条路径：若结果已经标注 truncated 就跳过处理；否则把过大的合并文本写成工具输出文件并保留前部预览。因此不能把通用工具的前部截断描述成 shell 尾部展示。[shell 尾部](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/core/src/shell.ts#L238-L260)、[默认值](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/core/src/tool-output.ts#L13-L16)、[通用输出路径](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/core/src/tool-output.ts#L69-L92)

## 清理与重启不是同一种恢复

服务在内存中保留最多 25 个已退出命令，超出后移除最旧项及其文件。另一个每小时运行的清理扫描按文件 mtime 删除超过七天的匹配输出文件，包括重启遗留文件。这两个限额分别针对进程内记录和时间；所读采集路径没有每份输出/全部磁盘的字节配额，不能把它们称为全局磁盘容量上限。[终态容量](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/core/src/shell.ts#L30-L34)、[记录移除](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/core/src/shell.ts#L167-L179)、[扫描及调度](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/core/src/shell.ts#L91-L119)、[时间清理实现](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/core/src/file-retention.ts#L6-L21)

`Shell` 的命令查找依赖进程内 Map。独立的 `SessionRestart.recoverShell` 从持久化 Job 信息补发结果：原 running 状态转成 cancelled，已经完成的使用保存的输出/错误，并用 `resume:false` 避免仅因通知唤醒空闲会话。所读恢复分支没有重新启动 shell，也没有把遗留 `.out` 文件重建成可查询的进程句柄。文件可能留存、历史结果可能恢复、原 Job 能继续运行，是三个不同的命题。[内存索引与查找](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/core/src/shell.ts#L141-L164)、[shell 恢复分支](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/core/src/session/execution/restart.ts#L101-L135)

## Sprig 基线缺口及可借鉴边界

Sprig 基线的后台输出按 stdout/stderr 分别增量解码和脱敏，只保留最多 1 MiB 内存前缀；`run(background=true)` 的批准说明明确不写前台日志 journal。前台则已有私有存储、会话所有权、不透明 `log_id`、容量/过期规则、当前凭据重新脱敏，以及获批快照变化时拒绝保存的机制。[后台保留](https://github.com/qinyin233/sprig-agent/blob/3877e3de07cdb1884f2c78b1bfac20246b501634/src/sprig_agent/jobs.py#L17-L67)、[批准与分支](https://github.com/qinyin233/sprig-agent/blob/3877e3de07cdb1884f2c78b1bfac20246b501634/src/sprig_agent/tools.py#L1332-L1377)、[前台日志保存](https://github.com/qinyin233/sprig-agent/blob/3877e3de07cdb1884f2c78b1bfac20246b501634/src/sprig_agent/command_logs.py#L193-L225)、[现有接口](../command-logs.md)

基于上述事实，本批设计应补的是**获批的后台日志可回查**，而不是声称复制上游全部持久进程能力。具体推论：

- 沿用现有 `log_id` / `read_log` 与私有所有权，不能返回一个允许模型绕开工作区读取边界的任意文件路径；新对话和跨进程恢复不得重放命令或 stdin。
- 留存计划进入该次具体命令批准，继续保留原命令期限、原始输出停止上限和内存展示上限；脱敏先于持久容量截取。即使日志留存成功，也不证明命令或固定验收通过。
- 多个后台命令可能持有同一旧 journal 快照。不能用旧批准淘汰在此后新写入的记录，也不能在后台静默刷新淘汰授权；无法按原计划保存时单独报告日志失败，保留真实退出/取消/超时/超限状态，不重新运行命令。
- 进程终态、采集结算与日志提交状态应可区分，避免“已退出”被误读成“日志已耐久保存”。保存异常或结果不确定不应阻止进程清理，也不应被改写成原命令失败。
- 明确本批究竟是完成后提交有界日志，还是执行中持续落盘；两者的崩溃留存和可回读时机不同。没有执行中落盘证据，就不能声称与上游流式文件采集相同。

这些是实现约束，不是已完成声明。本次没有测试 Sprig 新功能，未把静态阅读当作真实跨平台行为或模型收益证据。远程 CI 继续按用户要求冻结至 2026-11-01；到期后仍需核实账户限制与此前门禁。
