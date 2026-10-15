# mini-SWE-agent 参考任务入口

此入口使用锁定的 mini-SWE-agent 2.4.6 `DefaultAgent`、`LitellmModel` 与原始 `mini.yaml` 模板，让参考 agent 决定自己的 Bash 操作顺序。它没有换成 Sprig 的行动循环，也不使用帮助探测的 DummyInput / DummyOutput。默认 Bash 环境替换为逐动作批准、外部验收和数值测量的适配层，因此这是有明确适配的 SDK 参考运行，不等同官方榜单配置或完整交互式 mini TUI。

## 运行

先安装独立 uv 环境，Windows 需要 Git Bash，POSIX 需要 Bash，JavaScript 任务还需要 Node：

```powershell
uv sync --project benchmarks/clients/mini --locked --python 3.12
.\scripts\benchmark-agnes.ps1 --client mini --run --case py-pagination --report .\mini-pagination-01.json
```

Windows 启动器沿用 Agnes 模型和服务预设，未提供进程密钥时用隐藏输入设置临时 `SPRIG_API_KEY`，退出时恢复原变量。`--client sprig` 为默认入口；mini 暂不支持 `--stream`、`--request-usage`、`--compact-context` 等 Sprig 专有参数。普通跨平台入口如下，只使用进程环境提供的 `SPRIG_API_KEY` 或 `OPENAI_API_KEY`：

```sh
uv run --project benchmarks/clients/mini --locked python -I -B benchmarks/clients/mini_task.py --run --case py-pagination --model agnes-3.0-flash --base-url https://apihub.agnes-ai.com/v1 --report mini-pagination-01.json
```

`--list` 不导入 SDK、不请求模型；`--run` 必须有交互式 stdin/stdout 和进程凭据。七个 Python / JavaScript 诊断任务使用与 Sprig 相同的初始代码、任务文字及外部验收。合成历史 `py-long-history` 未适配，运行前拒绝，不能宣称两边长会话输入等价。

每条 Bash 行动单独显示完整 argv 和命令文字，默认拒绝；没有自动批准选项。Windows 命令通过固定 Python 中继进入 Git Bash，避免 CRT / MSYS 改写长命令的引号和反斜线；中继仍处于 Sprig 的 Job Object / POSIX 进程组内。通过命令变量传入待执行文字不等于额外授权，实际文字已经是外层批准参数。单个命令默认有 30 秒超时、16,000 字节输出上限；`--request-timeout` 可调整共享超时（大于 0、至多 120 秒），`--request-interval` 可设置 0–60 秒后端起始间隔。取消和超限清理整个进程树。工作目录字段不能越过候选根；用户批准的 Shell 命令本身仍不是 OS 沙箱，可能访问外部文件和网络。

参考模型发出完成标记后，应用运行与 Sprig 相同的外部固定验收 argv，此操作也需新批准。验收失败作为工具结果返回 mini，由它继续决定修复方法；拒绝验收直接结束且不计成功。每次批准和动作前后检查外部验收快照、驱动/中继文件及 SDK 源码摘要；变动使运行无效。这不是任意恶意程序的隔离或完整防作弊保证。

## 隐私与测量范围

SDK 导入前切换到一次性 HOME、配置、缓存和会话路径，仅模型进程持有 OpenAI 凭据。Bash 子进程沿用 Sprig 的环境过滤；环境模板不暴露 `os.environ`，不读取个人模型配置或保存轨迹。原始 SDK 日志、响应正文和异常不导出，显示的模型内容经过密钥和终端控制字符过滤。报告是显式创建的新文件，既有文件不会覆盖。

报告保留模型请求参数、模型路由、Python/平台、任务/输入/验收/驱动/SDK/模板/锁文件摘要、完成状态、SDK 模型 query 数、后端调用计时、逐工具结果字节和计时、批准次数与等待时间。任务总时间包含适配和完整性检查开销，扣除批准等待后的时间另列，不能仅展示扣除后的值。

报告 schema 2 区分 SDK 后端调用 `requests` 和实际 HTTP 测量 `request_measurements`。每次 `_query` 动态传入专用 OpenAI/HTTPX 客户端，不放入可序列化的模型配置，也不复用 LiteLLM 的全局客户端缓存。SDK 内层、LiteLLM 和 mini 外层自动重试均停用；`http_attempts` 是发起底层 HTTP 调用的次数，不保证服务实际收到请求。

HTTP 适配层从完成的原始 JSON 响应中提取通用 token 字段，缺失或矛盾保持未知，不使用 SDK 补出的字段；服务明确返回的全零有效。报告只保留三个经过检查的 token 数，不保留其他服务元数据，仍不能当作真实账单证据。记录序列化请求体、读取到的响应体字节及 HTTP 调用耗时，HTTP 头不计入字节数；错误和重定向正文不读取。HTTP 成功仅表示完整响应体已在限额内取得，SDK 格式校验和任务验收另行判断。非流式首段文本时间保持未知；此入口不估算费用，未知模型价格使 SDK 金额上限停用。默认最多 20 次模型 query（可设 1–100），小夹具的额外 SDK 壁钟限制为 `600 + interval * (max_steps - 1)` 秒，包含审批等待；这些不是精确费用或硬网络总时限。仓库任务取消这项额外壁钟限制，详见[固定仓库入口](repository-task-benchmark.md)。

[2026-10-15 实测](live-evaluation-20261015.md)中，mini 与 Sprig 均通过同一简单分页诊断的七项外部检查。一次样本、不同代理路由和审批等待不足以构成速度、稳定用量优势或整体性能排名；完整复杂任务成绩仍缺失。

Sprig 和 mini 现在都在发送前限制实际序列化的 JSON 请求体字节，默认 131,072，`--max-context-bytes` 可设 4,096–2,097,152。计算包括各自模板和工具定义，按 UTF-8 字节而非字符数，不强制参考 agent 采用 Sprig 的模板或操作顺序。超预算记为未发送并停止，不删除历史；mini 尚无 Sprig 的显式压缩适配。HTTP 只允许配置的 Chat Completions POST 地址，不跟随重定向；响应体上限 2 MiB。请求 `Accept-Encoding: identity`，压缩响应在解压前拒绝，避免 SDK 自动扩张压缩正文。超限时清理连接，已读取的单个底层块计入响应字节，因此超限数值可能略大于上限；限额不是进程总内存上限。

相同字节上限只对齐这一项资源约束，不代表 token、价格、工具、自然历史或完整运行环境相同。脚本注入模型的离线测试没有 HTTP 传输，字节预算和 HTTP 统计保持未知。还未测总内存或 TUI 就绪，不能直接把诊断结果当作正式性能排名。

## 已验证与未完成

原有独立客户端 CI 使用实际锁定 SDK，检查 Python 与 Node 验收、失败后继续修复、拒绝验收、取消、本地 HTTP 请求的模型路由和数值用量。产品 CI 检查批准、只读、凭据隔离、输出限额、Bash 引号/Unicode 往返和超时后子孙进程退出，覆盖 Windows、Linux、macOS。新增的传输适配已在本机 Windows 运行实际 SDK 的本地 HTTP 及边界测试；按节省额度的更新策略，本批推送跳过 CI，新增测试在 POSIX 上的远程结果尚未取得。

新增 [固定 Click 仓库任务](repository-task-benchmark.md)使用独立环境中的 pytest 8.4.2，让两边从同一修复前源码和外部验收运行。Windows/WSL 的固定回复回归覆盖各自行动循环中首次验收失败、四文件修复和重新验收通过，以及拒绝验收。它仍是单个公开历史任务，未运行真实模型，新增批次采用 `[skip ci]`，没有该批次远程 CI 结论。

这些测试中的模型回复和 HTTP 内容都是固定夹具，不是模型编码成绩。当前仍未完成真实模型对照、多仓库独立任务集、自然长会话或峰值进程内存测量，不能因此声称接近顶尖 agent。
