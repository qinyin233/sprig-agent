# 后台命令任务

长测试或服务可以在当前 Sprig 进程中后台运行，模型继续进行其他调查，用户可查看状态、等待、读取输出或停止任务。后台命令仍是有完整系统权限的外部程序，不是 OS 沙箱；每次启动都批准准确 argv/cwd、期限、环境名称及输出限制。

## 开启与操作

用户配置或可信 profile：

```toml
[execution]
background = true
background_timeout = 300

[tools]
run = true
job = true
```

也可启动时使用 `uv run --locked sprig --background --background-timeout 300`，`--no-background` 覆盖配置。默认关闭；项目层不能声明 execution，不能开启后台执行。关闭 run/job 或启用只读模式在执行层阻止启动，模型也不获得相应定义。开关及启用时的期限绑定会话，模型切换不能放宽策略。

启用后 `run` 接受 `background=true`，返回 `job_id`，不等待程序退出；仅后台可使用独立的 `background_timeout` 上限（默认 300 秒，最大 3600 秒）。普通前台命令仍受原有 timeout 上限限制。argv 不经过隐式 shell，cwd 仍为工作区相对 POSIX 路径，执行前和批准后复查路径、开发指令、取消和凭据。Windows 的批处理需明确批准 cmd.exe /c，不能隐式换解释器。

TUI/chat 本地入口：

```text
/jobs
/job start {"argv":["uv","run","python","-m","unittest","discover","-s","tests","-q"],"cwd":".","timeout":300}
/job status job-<ID>
/job wait job-<ID> 5
/job output job-<ID> 0
/job cancel job-<ID>
```

`job` 模型工具提供 list/status/output/wait/cancel；启动统一使用 run(background=true)。list 可带列表偏移，输出页有 next_offset。wait 的 seconds 最多 30 秒，模型未提供时只观察当前状态；本地 `/job wait ID` 默认等待 1 秒。等待可以取消，不重发命令。

启动和 cancel 都新批准。用户 Ctrl+C 明确取消当前任务和当前对话的全部后台命令，空闲时也有效；退出/关闭会停止当前归属的进程树。TUI 每隔短暂时间显示完成状态，空闲完成不自动请求模型，不自动继续任务。需要后续处理时用户正常 Enter 发送或继续。执行中输入引导的原有语义不变，批准中的新要求会拒绝旧启动。

## 状态、输出与验收

状态区分 running/exited/cancelled/timed_out/output_limited/failed/cleanup_failed。只有实际观察到自然退出时才有可靠退出码；取消、超时或清理失败不能伪装成退出 0。启动结果标为 started、task_completed=false；任务句柄不是可重放的命令，也不允许用任意 PID 停止其他进程。状态里的 command 是有截断标记的短预览，完整批准文本才是原 argv。

stdout/stderr 分别进行增量 UTF-8 解码和跨块脱敏，再合并为观察顺序的文字；不保证两路的全局时间顺序。命令应输出 UTF-8，其他编码按替代字符处理，不会自动修改任意程序的编码设置。stdin 立即关闭，不提供 PTY、交互输入或终端仿真。

原始总输出仍受 output_limit 或 max_output 限制，超限会停止进程树。另有独立的**内存回查上限**：每任务最多 1 MiB 脱敏正文、最多 8 条记录，至多 4 条同时运行。超出内存上限明确标 retention_limited，程序可在原输出限制内继续；不要把未保留正文当成命令失败。新启动可淘汰已结束的旧记录，不能淘汰正在运行的所有权。

output 返回 UTF-8 字节 offset、end、next_offset、脱敏快照前缀 SHA-256 和不可信标记。续读时同时传回 end，可在输出继续增长时保持同一前缀；模型使用 job(action=output,job_id,offset,end)，本地命令 `/job output ID OFFSET END`。字符边界错误和越界明确拒绝。完整 JSON 页不超过 min(max_output,16384)，后台启动要求 max_output>=1024。新获知的凭据命中已存正文时，回查内容整体丢弃并禁止后续正文保留，程序状态与退出码独立保留。

后台输出仅在内存，不使用前台 command_log_dir 的磁盘 journal；已进入模型工具结果的片段仍可能作为普通私有会话历史保存。只保存历史文字/已观察状态，不保存后台进程、PID 归属或启动授权；恢复、历史分支、模型切换和新对话都不会重启命令。

模型在下一安全边界收到有界的结束状态，不把程序输出当开发指令。后台任务的启动/结束使旧文件读取标识与语法缓存失效。仍有任务运行或清理不确定时，最终结束保持未验收，固定基线/最终检查不启动，编辑后检查延后；等待或停止后需明确继续。检查过程中后台状态发生变化也不能宣布最终通过。后台命令退出 0 不是应用的固定验收，最终检查仍另起一次新批准。

## 所有权与退出边界

复用已受管的非阻塞进程管道：Windows 在子进程执行前分配 kill-on-close Job Object，POSIX 独立进程组保留组长归属直至清理。命令结束也清理继承管道的后代，避免只留下父 PID 消失、后代仍运行的情况。后台线程只泵取有界输出和观察状态，不执行模型回调或申请批准。

重置期间禁止新启动，已打开的旧启动提案失效；清理完成前不丢弃进程所有权。关闭永久禁止该所有者再启动；清理超时会保留记录供检查/重试关闭，不报告已清理。目录/会话句柄在所属工作线程结束前保持有效。

这些是正常取消和关闭的进程归属管理，不是系统隔离。命令可能访问外部文件、网络或把工作交给外部服务；进程组/Job 不会撤销已有副作用。宿主被强制杀死/崩溃后，特别是 POSIX 上，无法保证所有后代已消失；恢复应检查实际系统和工作区，不能按旧记录盲目重启或附着到可能复用的 PID。

## 源码与证据

阅读固定 OpenCode `9d90d892dcbfc52b47e95b2ea31b8aba877128fd` 的 [`shell.ts`](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/shell.ts) 和 [`tool/plugin/shell.ts`](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/tool/plugin/shell.ts)：上游拆分归属、异步 job、带游标的文件输出和通知；默认后台可不设期限，完成通知写入会话。Sprig 保留 argv、逐次批准、明确硬期限、内存限额、取消和不重放，空闲不因完成自动调用模型；不复制 shell 字符串执行、无期限运行或七天文件自动清理。

验证覆盖实际 Windows/POSIX 子进程与后代、stdin EOF、双通道分片 UTF-8、私有参数/环境和分页、同时启动上限、退出/超时/超限/取消、重置并发与永久关闭、任务未结束/检查中变化禁止验收，以及真实 TUI/chat 和三协议的启动→并行编辑→等待→读取→独立检查。固定模型回复不证明新的编码能力或性能排名。最终范围见[机器记录](benchmarks/background-jobs-20261022.json)。

按用户要求 2026-11-01 前不调度、不重跑 CI，所有推送使用 `[skip ci]`。本地 Windows/WSL 和平台跳过单独计数，不当作原生 runner 或完整版本矩阵；新进程生命周期仍待相关 CI 验证，到期先核对账户及先前必要 gate。

最终冻结源码的主测试两端各 1163 项：Windows 1144 通过、19 平台跳过，WSL 1140 通过、23 跳过；Windows Python 3.11 相关 78 项、两端 mini SDK 各 25 项通过。隔离安装核对 60 个源码模块，80×24 TUI 经三次独立批准走完真实后台命令、并行文件修改、等待/输出和固定检查；私有恢复禁止进程创建、旧句柄不重放。分流 UTF-8 解码与重置并发问题均在修复后才冻结验证。本批没有外部模型请求或远程 CI；本地结果不代表原生 CI runner、完整版本矩阵或新的编码性能成绩。
