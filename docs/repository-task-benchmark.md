# 固定 Click 仓库任务

此入口让 Sprig 和 mini-SWE-agent 2.4.6 从同一 Click 源码快照、任务文字和外部验收开始，各自保留正常行动循环。任务来自 [Click #3107](https://github.com/pallets/click/issues/3107)，行为与验收参考 [#3228](https://github.com/pallets/click/pull/3228)：增加拼错子命令的建议，统一命令和选项错误格式，并导出独立的 `NoSuchCommand` 异常。它为实际模型对照提供入口，当前校准和脚本回复测试都不是模型成绩。

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

真实运行需要在当前终端进程中提供 `SPRIG_API_KEY` 或 `OPENAI_API_KEY`。模型和地址可用参数或 `SPRIG_MODEL`、`SPRIG_BASE_URL` 设置，默认服务预设仍为 Agnes。两条命令需在交互式终端分别运行，报告使用不同的新路径：

```powershell
uv run --project benchmarks/repositories/click --locked python -I -B scripts/benchmark-repository-task.py --fixture .\click-pinned --run --client sprig --model agnes-3.0-flash --base-url https://apihub.agnes-ai.com/v1 --report .\click-sprig-01.json
uv run --project benchmarks/clients/mini --locked python -I -B scripts/benchmark-repository-task.py --fixture .\click-pinned --run --client mini --model agnes-3.0-flash --base-url https://apihub.agnes-ai.com/v1 --report .\click-mini-01.json
```

POSIX 使用相同命令，把路径写成 `./click-pinned` 等。Windows 的 mini 需要 Git Bash，POSIX 需要 Bash。密钥不写入配置或报告，不提供自动批准选项。每次写入、Shell 操作和外部验收都需要新批准，默认拒绝。失败的验收回到各自 agent 的正常循环中继续修复；拒绝或取消不计成功。

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
