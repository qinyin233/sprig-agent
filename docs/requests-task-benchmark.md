# 第二个固定仓库任务：Requests 异常序列化

此任务来自 [Requests #6628](https://github.com/psf/requests/issues/6628) 和[上游修复 #6629](https://github.com/psf/requests/pull/6629)：JSON 解码异常经过 pickle 或进程池传输时，调用者应收到原异常，并能继续提交有效任务。它扩展了已有的 [Click 仓库任务](repository-task-benchmark.md)，用于发现另一类真实库代码问题；本批只有离线校准和固定回复回归，没有外部模型请求或新的编码成绩。

## 固定输入与检查

| 项目 | 条件 |
| --- | --- |
| 初始提交 | `96b22fa18c00831656ee4b286bf1c9062459b00a` |
| 初始归档 SHA-256 | `b9a1f599c259a4e29edeefb0a0690a31c89a4d038212bd44ea9b9d8321908ca5` |
| 初始所选文件摘要 | `1e92afeb2bca849b3dfa91a1d604c43fc2d960a2b2736db18a1edb062b2de0a2` |
| 参考修复提交 | `3ff3ff21dd45957c9e143cd500291959bb15f690` |
| 参考归档 SHA-256 | `7df06fd63752aeaf6e0bed7d2f31d97383bebec3cf19694dbe26d92f9153fdc5` |
| 参考所选文件摘要 | `8cf248a17d3c52edb0a25bc79f050cda83891a3fd3aa106089e122c6b16e707b` |
| 候选输入 | 40 个源码、测试、许可证及项目文件，共 412,614 字节；不是完整仓库 |
| 环境 | 同一独立 uv 项目，Python 3.12；Sprig 完整运行依赖、mini-SWE-agent 2.4.6、LiteLLM 1.103.2、pytest 8.4.2、simplejson 3.20.2 |
| 默认预算 | 12 次模型轮次，2048 输出 token，序列化请求体最多 131,072 字节，请求超时 120 秒 |

任务文字只描述症状和预期行为，不包含文件位置、继承顺序分析或修复代码。参考源码只用于校准；候选输入保持修复前版本。外部验收保留上游新增回归函数及许可证，再加入针对 pickle 协议 0–5、不同文档、复制、异常继承、正常 JSON、无效 JSON 和真正 `spawn` 进程池的检查。进程池同时覆盖返回异常、抛出异常，以及出错后再次执行有效任务。

标准库 json 和 simplejson 各执行 29 项固定检查，合计 58 项、零跳过。这不是 Requests 全部上游测试。初始源码固定通过 12/58，参考修复通过 58/58；自检同时要求失败标识完全符合预期，缺库、错误导入或任意失败不能冒充正确基线。报告严格检查 JSON 字段、类型、完整数量和退出码，单独零退出不表示通过。

两端使用相同项目和候选快照，各自保留原生工具、循环与模板。Sprig 支持三协议 JSON/SSE；当前固定 mini SDK 只接受 Chat Completions，其他协议在初始化 SDK 前拒绝。该任务公开且历史悠久，存在训练数据污染可能；任务数量和校准结果都不能用来宣称接近顶尖 agent。

## 运行

在仓库根目录执行，夹具和准备目录必须位于仓库之外、父目录已存在、目标尚不存在：

```powershell
$sprigFixture = Join-Path $env:TEMP 'sprig-requests-pinned'
uv sync --project benchmarks/repositories/requests --locked --python 3.12
uv run --project benchmarks/repositories/requests --locked python -I -B scripts/benchmark-requests-task.py --fetch --fixture $sprigFixture
uv run --project benchmarks/repositories/requests --locked python -I -B scripts/benchmark-requests-task.py --self-test --fixture $sprigFixture
```

`--fetch` 只下载固定官方归档，先核对完整归档和所选文件摘要，再创建私有文件；不解压链接或执行归档代码。下载限制为每份 8 MiB、最多 10000 个条目、单文件 4 MiB、所选文件合计 64 MiB。夹具目录和归档在加载时仍需通过当前账户的私有权限检查。WSL 应在原生 Linux 文件系统创建私有夹具，不能把 NTFS 的权限映射当成 POSIX `0600`。

`--self-test` 在一次性目录执行固定基线和参考修复，不请求模型。`--prepare PATH` 只导出修复前工作区、任务和外部验收，目标也必须是新的外部目录。`--report PATH` 只能创建新报告，不覆盖已有文件。

真实运行使用当前终端的进程环境凭据和逐次操作批准：

```powershell
uv run --project benchmarks/repositories/requests --locked python -I -B scripts/benchmark-requests-task.py --run --client sprig --fixture $sprigFixture --model YOUR_MODEL --base-url https://YOUR_PROVIDER/v1 --report .\requests-sprig-01.json
uv run --project benchmarks/repositories/requests --locked python -I -B scripts/benchmark-requests-task.py --run --client mini --fixture $sprigFixture --model YOUR_MODEL --base-url https://YOUR_PROVIDER/v1 --report .\requests-mini-01.json
```

凭据使用 `SPRIG_API_KEY` / `OPENAI_API_KEY`；Agnes HTTPS 原点也接受 `AGNES_API_KEY`。不要在命令参数、文件或报告中写入密钥。两条真实入口要求交互式终端，不提供自动批准参数。Windows mini 需要 Git Bash，POSIX 需要 Bash；POSIX 使用相同命令及相应外部路径。

可以显式指定 `--wire-api`、`--stream`、`--max-steps`、`--max-tokens`、`--max-context-bytes`、`--request-timeout`、`--request-interval` 以及生成参数。`--no-thinking` 只表示不加入 Sprig 预设；服务是否关闭推理取决于明确的提供商参数。详见[模型生成设置](model-generation.md)。比较需要对齐实际参数和环境，mini 不支持的运行控制会被拒绝。

## 运行环境与进程边界

首次解析得到的 LiteLLM 1.106.4 在实际 HTTP 请求前因可选 MCP 路由导入失败，手工补入当时 MCP 包仍有类型不兼容。最终项目固定为已验证 mini 环境使用的 LiteLLM 1.103.2，并在这套锁定环境中实际运行 SDK 循环。没有为 Sprig 产品增加 MCP 依赖，也没有将初始化失败记成模型失败。mini 报告新增实际依赖版本，并使用所选项目的锁摘要；旧入口默认仍记录其原 mini 项目锁。

Requests 和 Click 的固定验收 worker 现在继承外层已批准命令的 Windows Job / POSIX 进程组，避免内层监督再次创建独立会话，让取消遗漏普通子孙进程。捕获有期限和字节上限，拒绝不完整或无效 UTF-8 结果；Windows 不会在另一个线程读取、子进程仍持有写端时无期限关闭管道。Requests 每个后端限 15 秒，外层校准限 60 秒；Click 保持原有 20 秒 worker 限额和验收数量。

固定回复测试覆盖三个协议的 JSON/SSE 读取、修改、重新验收，mini 的首次验收失败、修改和再次提交，以及不修改和拒绝验收。另用真实临时进程检查输出超限、内层/外层超时和外层取消时的收尾。这些回归中的回复和修复是已知夹具，不能计为 agent 自主修复能力。

保护仍依赖每次操作的明确批准、目录/源码/锁文件完整性、凭据过滤及有限输出。它不是 OS 沙箱或防作弊隔离：获准的代码可能访问网络和同账户文件，恶意代码可能另建会话或伪造进程内检查。原始参考归档由操作者保留，不能批准模型检索其中的解答。数值报告不保存提示词、源码、原始模型文字、工具参数或凭据。

初版 Requests 保护集合遗漏实际执行的 Sprig 源文件。隔离反例模拟同大小内容变化，确认验收仍被错误视为完整；现已像 Click 一样核对实际运行源码、辅助驱动和锁文件的解析路径、大小及有限读取的内容，并检查验收根目录的替换和重解析点。回归还模拟源路径改指向其他文件，整个过程不修改共享仓库源码。初版完整回归不作为这次补强后的最终结果。

完整 Windows 回归还暴露既有子进程测试的 200 毫秒启动窗口过短。夹具改为 2 秒命令期限、子进程 4 秒后尝试写入，再观察 4.2 秒；仍要求实际创建子进程、发生超时且没有延迟写入。原生对照中只杀父进程确实留下延迟写入，Sprig 收尾后没有写入。只调整测试夹具的启动余量，没有扩大产品默认期限；此前失败和修正后的完整回归分别记录。

当次失败没有直接记录启动调度，因此不将具体调度原因当成已证实事实。控制性地在父进程创建子进程前延迟 0.35 秒，可复现旧期限下没有标记、新期限下创建成功且正常超时的差异；这证明旧夹具的启动假设不足，不能把缺少标记单独解释成子进程回收失败。

随后完整回归发现采样截断、普通命令也有相同的 150–200 毫秒假设，一并为依赖输出的夹具保留 2 秒启动余量，并将清理总耗时限制与配置期限分开。延迟写入仍在期限之后，截断、输出上限和无残留写入断言保留。父进程先退出、子进程继续持有管道的用例还新增实际创建标记检查，防止进程未启动时错误通过；不依赖启动后输出的短期限测试保持原语义。

## 本地验证与后续

[本批机器记录](benchmarks/requests-task-20261026.json)保留最终源码摘要、依赖、两套任务校准和本地检查范围。复现 Requests 集成回归：

```powershell
$env:SPRIG_REQUESTS_FIXTURE = $sprigFixture
uv run --project benchmarks/repositories/requests --locked python -I -B -m unittest discover -s benchmarks/repositories/requests/tests -q
uv run --project benchmarks/repositories/requests --locked python -I -B -m unittest discover -s benchmarks/clients/tests -q
```

CI 工作流已加入这些离线步骤，供恢复后的三平台 runner 验证；本批使用 `[skip ci]`，11 月 1 日前不触发 Actions。届时仍需先核实账户限制与此前所需门禁，本地 Windows/WSL 以及平台跳过均不代替远程原生 CI。后续还需条件一致的多次真实模型运行、更多独立任务和自然长会话，当前没有整体性能结论。
