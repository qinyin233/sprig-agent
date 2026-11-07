# 固定 Click 仓库任务

此入口让 Sprig 和 mini-SWE-agent 2.4.6 从同一 Click 源码快照、任务文字和外部验收开始，各自保留正常行动循环。任务来自 [Click #3107](https://github.com/pallets/click/issues/3107)，行为与验收参考 [#3228](https://github.com/pallets/click/pull/3228)：增加拼错子命令的建议，统一命令和选项错误格式，并导出独立的 `NoSuchCommand` 异常。它为实际模型对照提供入口，当前校准和脚本回复测试都不是模型成绩。

另有独立的 [Requests 异常序列化任务](requests-task-benchmark.md)，使用同一项目中的完整 Sprig / mini 环境检查 pickle 与真正进程池行为。2026-10-26 两套验收改为让内层 worker 继承外层已批准命令的进程组 / Job，补验取消与超时后的普通子孙进程收尾；Click 的任务、642 项检查及已知失败集合保持。更新后的本地校准范围见[本批记录](benchmarks/requests-task-20261026.json)，没有新的真实模型成绩。

## 输入和验收

| 项目 | 固定条件 |
| --- | --- |
| 初始提交 | `19fd4d6e18bc9fce451f92f422696b11169faa57` |
| 初始快照 SHA-256 | `7d610833c8a6564f980f747e4601e7f4821872702c4a59f32ff9c5d653300d08` |
| 验收提交 | `831c8f0948af519e45b90801d7430ff25451f972` |
| 验收快照 SHA-256 | `62d0162a63fa5322307e29d1710ec7624bd3ca20572329fddbdae1fe628f628d` |
| 候选输入 | 52 个源码、测试、许可证、README 和配置文件；不是完整仓库归档 |
| 验收 | 固定外部 `test_commands.py`、`test_options.py` 的 642 项 pytest 检查，另检查公开异常类型 |
| 运行环境 | 锁定 Python 3.12 环境、pytest 8.4.2；报告保存实际补丁版本和平台 |
| 默认预算 | 20 次模型轮次，实际序列化 HTTP JSON 请求体最多 131,072 字节；均可显式设置 |

候选工作区只有修复前源码。适配器校验两份完整快照后仅保留参考测试和配置，外部验收目录不包含参考实现。原始下载目录仍由调用者保存，不是不可访问的隐藏答案；应拒绝读取该目录或检索上游解答的操作。Sprig 保留原生文件工具，mini 保留自己的 Bash 行动、SDK `DefaultAgent` 和模板，没有统一两者的操作顺序、提示词或工具。单个公开历史任务可能被模型记住，不能代表未见任务、完整 SWE-bench 或整体排名。

## 运行

在仓库根目录执行。首次准备下载固定公开归档，目标必须不存在；内容受大小和摘要校验约束，不执行归档代码：

```powershell
uv sync --project benchmarks/repositories/click --locked --python 3.12
uv sync --project benchmarks/clients/mini --locked --python 3.12
uv run --project benchmarks/repositories/click --locked python -I -B scripts/benchmark-edit-repo.py --fetch .\click-pinned
uv run --project benchmarks/repositories/click --locked python -I -B scripts/benchmark-repository-task.py --fixture .\click-pinned --self-test
```

`--self-test` 在两个一次性候选工作区运行固定校准，不请求模型。`--prepare .\prepared-click` 只导出初始工作区、任务文字和外部验收，不启动候选程序或模型；目标必须是新目录。`--report` 只能显式创建新文件，不覆盖已有文件。

root 依赖更新后要同时核对 `uv lock --project benchmarks/repositories/click --check`；2026-10-22 已修复嵌套锁文件缺少 root 既有依赖的问题。不要用非锁定运行绕过失败。近期 [Sprig Flash-2 记录](live-evaluation-work-progress-20261023.md)为 640/642，另一提交上的 [mini Flash-2 参考](live-evaluation-mini-flash2-20261023.md)为 639/642，两者均未完成；这不是已配对的性能对照，校准通过也不算真实任务完成。

真实运行需要在当前终端进程中提供 `SPRIG_API_KEY` 或 `OPENAI_API_KEY`；Agnes HTTPS 原点也支持 `AGNES_API_KEY`，不把该别名发送到其他服务。模型和地址可用参数或 `SPRIG_MODEL`、`SPRIG_BASE_URL` 设置，默认服务预设仍为 Agnes。两条命令需在交互式终端分别运行，报告使用不同的新路径：

```powershell
uv run --project benchmarks/repositories/click --locked python -I -B scripts/benchmark-repository-task.py --fixture .\click-pinned --run --client sprig --model agnes-3.0-flash --base-url https://apihub.agnes-ai.com/v1 --report .\click-sprig-01.json
uv run --project benchmarks/clients/mini --locked python -I -B scripts/benchmark-repository-task.py --fixture .\click-pinned --run --client mini --model agnes-3.0-flash --base-url https://apihub.agnes-ai.com/v1 --report .\click-mini-01.json
```

POSIX 使用相同命令，把路径写成 `./click-pinned` 等。Windows 的 mini 需要 Git Bash，POSIX 需要 Bash。密钥不写入配置或报告，不提供自动批准选项。每次写入、Shell 操作和外部验收都需要新批准，默认拒绝。失败的验收回到各自 agent 的正常循环中继续修复；拒绝或取消不计成功。

`--request-interval 20` 可让两边后端调用开始之间至少相隔 20 秒；默认 0，不等待。范围 0–60 秒，非有限值被拒绝。报告单列 `request_pacing` 的起始次数和等待时间；这些起始次数不是实际 HTTP 次数，后续校验仍可能阻止发送。总任务时间与扣除审批等待的时间都包含该等待，实际 HTTP 计时不含等待。它减少请求密度，不重试失败的 POST，也不能保证规避共享 token 限额或配额耗尽。

`--request-timeout 60` 可显式调整后端超时，默认 30 秒，范围大于 0 且至多 120 秒，非有限值被拒绝。该值传入共享配置，报告为 `request_timeout_seconds`；默认命令超时也受该配置约束，工具显式提出的超时仍单独校验。它不增加模型轮次或请求体容量。

两边均可显式设置 `--max-tokens 2048` 和 `--thinking`，报告用 `generation_options` 记录生成配置；默认不发送这两个字段。它们计入实际请求体预算；mini 通过原生 SDK 发送同样的顶层 `max_tokens` 与 `chat_template_kwargs`。思考模式依赖服务支持，每次输出上限不是总费用上限。比较必须对齐这些参数，不能把思考模式的新运行与旧默认配置直接当作同条件结果。详见[生成配置](model-generation.md)。

仓库对照的 mini 现在使用 SDK 原生 `wall_time_limit_seconds=0`，与 Sprig 都不施加包含审批等待的额外任务壁钟上限；模型轮次、实际请求体预算、请求/命令超时和取消仍保留。普通小夹具入口继续使用 `600 + interval * (max_steps - 1)` 秒。这是评测配置调整，未修改上游 agent 循环或自动批准动作；旧报告的 600/980 秒限时与审批等待记录保持原样。

Sprig 每次工具调用新增数值 `diagnostics`：错误/批准/截断标志、读取/扫描计数、搜索命中行数及 regex 模式、正文字节数等。最多保留 128 个检查指纹，`same_output_repeats` 是相同参数与相同完整返回对象的先前出现次数；不输出指纹、路径、查询、正文或任意错误消息。重复的截断输出仍可能遗漏数据，不能据此认定工作区完整或没有变化。所有工具仍实际执行，诊断不充当缓存、自动批准或性能成绩。

2026-10-24 产品交互默认改为 1 MiB 采集上限，但这里的 Sprig/mini 评测工厂仍显式将原始采集上限固定为各自声明的 `max_output`，默认 16000 字节；校准、客户端探针及历史上下文回放也保留原上限。不得把产品默认更新静默变成评测资源扩张；旧数值记录不重写，产品长日志回归不算新的模型成绩。

## 验收边界和报告

应用在动作和批准前后检查外部验收目录、驱动源码、锁文件，以及 mini 的 SDK 源码。新增文件、空目录、符号链接、重解析点、目录替换或内容变化使运行无效。固定验收的 `-I -B` 解释器不使用候选 pytest 配置或插件环境；先导入锁定的 pytest，再导入候选 Click，避免候选 `src/pytest.py` 替换运行器。

监督进程以现有 Windows Job Object / POSIX 进程组执行候选子进程，限 20 秒和 65,536 字节输出。外层验收由用户批准，内层仅执行已批准的固定 worker，不接受模型自选命令。监督进程要求完整且类型正确的结果、实际收集和通过数均为 642、零跳过、正确公开异常类型和零退出码；单独 `os._exit(0)` 或输出噪声不能通过。只公开七个固定已知失败标识，其他失败只报数量，不导出任意候选输出或异常。

这些约束不是 OS 沙箱或完整防作弊隔离。获得批准的 Python/Shell 代码仍可能访问外部文件、网络或同一账户进程；恶意候选代码也可能修改进程内 pytest 或伪造协议。mini 使用一次性 HOME/配置路径，Sprig 的普通运行环境不因此获得相同的 HOME 隔离。此入口适合受控对照，不能用来免除不可信代码的操作审查。

报告包含任务、输入、验收、入口、适配器及锁文件摘要、模型设置、请求与工具开销、批准等待、平台和完成状态；不保存原始模型文字、API 密钥及 Shell 参数。两条入口的报告 schema 和工具不同，未知统计保留未知；相同请求字节上限不等于相同 token、价格或总内存。mini 的详细适配条件见 [参考任务入口](mini-task-runner.md)。

## 本地验证记录

[Windows 校准](benchmarks/click-task-oracle-windows.json)和 [Linux 校准](benchmarks/click-task-oracle-linux.json)分别记录初始源码 635/642、参考修复 642/642、零跳过，公开异常契约由失败变为通过。它们证明固定验收能区分两份源码，未发生真实模型对照。

新增根测试覆盖结果类型/数量、重复 JSON 字段、预检拒绝、凭据过滤及验收完整性。独立仓库测试运行实际 Sprig HTTP 链路与 mini SDK 循环，固定回复先提交失败，再修复四个源码文件后通过；另检查拒绝验收、零退出、输出超限、候选测试/配置和 pytest 遮蔽。复现无需模型密钥：

```powershell
$env:SPRIG_CLICK_FIXTURE = (Resolve-Path .\click-pinned).Path
uv run --project benchmarks/repositories/click --locked python -m unittest discover -s benchmarks/repositories/click/tests -p test_repository.py -q
uv run --project benchmarks/clients/mini --locked python -m unittest discover -s benchmarks/repositories/click/tests -p test_mini_repository.py -q
```

POSIX 在命令前设置 `SPRIG_CLICK_FIXTURE=/absolute/path/to/click-pinned`。回归已加入现有三平台 `client-probes` job，不增加新矩阵 job，供下一批启用 CI 时运行。本批采用 `[skip ci]`，本地 Windows/WSL 结果不代表远程 runner 或 macOS 验证。下一步需要同一平台、模型、任务和预算下的多次真实运行，并扩展独立任务集合，才能判断 Sprig 的实际差距。

## 2026-11-07 单次真实复测

在干净的 Sprig `8c39bc8c5425c686a625417d24e30747f0c55286` 上，从重新下载并通过摘要校验的 52 文件初始快照运行一次。Windows 使用锁定的 Python 3.12.12 和 pytest 8.4.2。USTC `deepseek-flash-2` 走 Chat Completions SSE，最多 12 次请求，每次最多 2048 输出 token、131072 请求字节，HTTP 空闲上限 120 秒、总期限 180 秒。使用 `thinking=false`，保留协议推理状态，开启确定性压缩、基线和编辑后检查。基线和独立后置检查分别新批准。

| 观测 | 结果 |
| --- | --- |
| HTTP 尝试和有效回复 | 9 次尝试，8 次完整回复。第 9 次 HTTP 200，终止原因为 `length`，应用记为 `output_limit` |
| HTTP 请求和响应 | 累计 293825 / 332456 字节。最后一次响应收到 149646 字节，低于客户端 2 MiB 限制 |
| 服务报告 token | 8 次完整回复合计 66486。第 9 次用量未知 |
| 模型工具 | 8 次 grep、6 次文件读取、1 次 glob、1 次目录列出。没有文件写入或模型发起的命令 |
| 独立验收 | 基线和后置均为 635/642，零跳过，公开异常契约失败。候选 52 个文件与初始快照逐字节一致 |

末次响应被丢弃，未执行其中的工具调用，也未自动重试。前 8 次有效响应触发的工具调用中出现一次多范围选择器错误。数值报告未保存具体参数。随后本地复现发现，系统提示要求 `line_numbers=true`，但 `ranges` 会拒绝这个与默认行为相同的显式标志。修复后两种写法返回相同的编号窗口和读取版本，三协议回环测试覆盖显式标志到获批编辑的路径。这次真实复测先于修复，Click 仍未完成，单次结果不足以衡量新接口对完成率的影响。[多范围读取](text-transfer.md)
