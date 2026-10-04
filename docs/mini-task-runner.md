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

每条 Bash 行动单独显示完整 argv 和命令文字，默认拒绝；没有自动批准选项。Windows 命令通过固定 Python 中继进入 Git Bash，避免 CRT / MSYS 改写长命令的引号和反斜线；中继仍处于 Sprig 的 Job Object / POSIX 进程组内。通过命令变量传入待执行文字不等于额外授权，实际文字已经是外层批准参数。单个命令有 30 秒超时、16,000 字节输出上限，取消和超限清理整个进程树。工作目录字段不能越过候选根；用户批准的 Shell 命令本身仍不是 OS 沙箱，可能访问外部文件和网络。

参考模型发出完成标记后，应用运行与 Sprig 相同的外部固定验收 argv，此操作也需新批准。验收失败作为工具结果返回 mini，由它继续决定修复方法；拒绝验收直接结束且不计成功。每次批准和动作前后检查外部验收快照、驱动/中继文件及 SDK 源码摘要；变动使运行无效。这不是任意恶意程序的隔离或完整防作弊保证。

## 隐私与测量范围

SDK 导入前切换到一次性 HOME、配置、缓存和会话路径，仅模型进程持有 OpenAI 凭据。Bash 子进程沿用 Sprig 的环境过滤；环境模板不暴露 `os.environ`，不读取个人模型配置或保存轨迹。原始 SDK 日志、响应正文和异常不导出，显示的模型内容经过密钥和终端控制字符过滤。报告是显式创建的新文件，既有文件不会覆盖。

报告保留模型请求参数、模型路由、Python/平台、任务/输入/验收/驱动/SDK/模板/锁文件摘要、完成状态、SDK 模型 query 数、后端调用计时、逐工具结果字节和计时、批准次数与等待时间。任务总时间包含适配和完整性检查开销，扣除批准等待后的时间另列，不能仅展示扣除后的值。

SDK 返回的通用 token 字段经过一致性检查；缺失、矛盾和可能由 SDK 补出的全零用量保持未知。它们标为 SDK 字段，尚未独立捕获原始 wire 用量，不能作为真实账单证据。后端调用数不等同已证明的 HTTP 尝试数，SDK 还可能自行重试；实际 HTTP 尝试、请求/响应体字节、首段流式时间均为未知。此入口不流式输出，也不估算费用；未知模型价格使 SDK 金额上限停用，限制使用最多 20 次模型 query（可设 1–100）及 SDK 的 600 秒轮次间检查，不能当作精确费用或硬网络总时限。

mini 当前保留上游上下文处理，`max_context_bytes` 明确为 `null`。还没有与 Sprig 的应用请求字节上限对齐，也未测总内存或 TUI 就绪。因此入口可用于诊断与对照准备，不能直接把结果当作相同完整预算的正式性能排名。

## 已验证与未完成

独立客户端 CI 使用实际锁定 SDK，检查 Python 与 Node 验收、失败后继续修复、拒绝验收、取消、本地 HTTP 请求的模型路由和数值用量。产品 CI 检查批准、只读、凭据隔离、输出限额、Bash 引号/Unicode 往返和超时后子孙进程退出，覆盖 Windows、Linux、macOS。

这些测试中的模型回复和 HTTP 内容都是固定夹具，不是模型编码成绩。当前仍未完成真实模型对照、共享请求预算、外部复杂仓库任务、自然长会话或峰值进程内存测量，不能因此声称接近顶尖 agent。
