# 固定版本客户端与原生入口检查

优化目标是接近当前顶尖开源编程 agent 的实际性能。能安装、显示帮助或较快退出只是对照的前提，不能据此判断编码成功率、整体速度或排名。

## 选择依据与版本

2026-10-04 核对的官方材料中，[SWE-bench 默认 Verified 视图](https://www.swebench.com/)让模型使用相同 mini-SWE-agent 环境；[mini-SWE-agent](https://mini-swe-agent.com/latest/)提供本地、批量评测和轨迹入口，因此把 mini 纳入编码评测参考。不能把某个模型的成绩归为所有模型都能达到的 agent 成绩。[OpenHands 官方评测仓库](https://github.com/OpenHands/benchmarks)提供 SWE-Bench、SWE-Bench Pro、ProgramBench 等协议，是复杂任务的另一候选；它要求匹配的 SDK commit 和工作区，目前尚未接入。

Aider、OpenCode、Pi 是日常编码客户端参考，选择它们不是当前排名的证明。正式选择“顶尖”对照时，需保存评测日期、任务集、模型、预算、agent 配置与源码版本；不能凭星数、历史分数或不同模型的榜单数字宣布追平。

| 客户端 | 固定版本 | 版本与帮助核对 |
| --- | --- | --- |
| Sprig | 0.1.0 | 根目录 uv 锁文件，原生模块入口 |
| Aider | 0.86.2 | 独立 uv 锁文件，`python -I -m aider` |
| OpenCode | 1.18.34 | npm 锁文件，本机原生二进制 |
| Pi | 1.0.0 | npm 锁文件，`@earendil-works/pi-coding-agent` 的 CLI |
| mini-SWE-agent | 2.4.6 | 独立 uv 锁文件，分发包版本与帮助分别检查 |

mini 的 [CLI 源码](https://github.com/SWE-agent/mini-swe-agent/blob/v2.4.6/src/minisweagent/run/mini.py)没有 `--version`，分发包元数据检查明确标为 `distribution-version`，其耗时不能与其他客户端的 CLI 启动作同项比较。mini 在导入时初始化 PromptSession，Windows 重定向句柄没有控制台屏幕缓冲区；`mini_help.py` 为未使用的提示框提供 DummyInput / DummyOutput，再执行原 CLI 帮助。适配器仅接受 `--help`，其他参数在导入客户端前被拒绝；它不是 TUI 或真实任务适配器。

## 安装与检查

在仓库根目录运行。Python 客户端用独立 uv 环境，不加入 Sprig 的运行依赖：

```powershell
uv sync --locked
uv sync --project benchmarks/clients/aider --locked --python 3.12
uv sync --project benchmarks/clients/mini --locked --python 3.12
npm --prefix benchmarks/clients/node ci --ignore-scripts --no-audit --no-fund
node benchmarks/clients/node/node_modules/opencode-ai/postinstall.mjs
uv run --locked python scripts/benchmark-clients.py --repeat 5 --report client-probe-01.json
```

npm 安装禁用脚本，随后只显式执行固定版本 OpenCode 的平台安装器。它把本机二进制链接或复制到 `bin/opencode.exe`；POSIX 上名字也保留 `.exe`，内容仍是 ELF / Mach-O。不要探测安装前的占位文件或旧版 JS 启动器。Node 需满足 Pi 的版本要求，CI 使用 Node 24。

`--client` 可选单个客户端，`--repeat` 限 1–20。报告只能创建新文件且父目录需存在。检查精确版本、实际帮助参数、返回码、超时和输出超限；返回 0 但输出为空、版本错误或参数缺失都失败。每个进程限 30 秒、65,536 字节输出，使用 Sprig 的 Windows Job Object / POSIX 进程组清理。

每次检查建立一次性工作区和私有 HOME、配置、缓存、会话目录；子进程仅继承列举的 OS / locale 变量，不继承 API 密钥、模型配置或认证文件路径，禁用已知更新、目录获取和分析上报。不构造模型 Provider。运行脚本仅授权固定元数据/帮助命令，内部批准回调不是 agent 任务的自动批准接口；这仍不是 OS 沙箱或第三方文件/网络隔离保证。

报告保留数值、验证状态、时间戳、平台、Python / Node 版本、驱动与锁文件摘要、入口运行前后摘要，不保存原始输出、异常正文或凭据。Aider / mini 的入口摘要是解释器，Pi 是 CLI 脚本，OpenCode 是二进制；它们不是整个已安装依赖树的完整校验。

## 实测范围与下一步

[Windows 原始报告](benchmarks/client-probes-windows.json)记录本机 Python 3.13 控制器、Aider / mini 的 Python 3.12.12、Pi 的 Node 24.11.1，五个客户端元数据/帮助检查通过。计时包含适配进程和 Sprig 执行器，首次样本未清空 OS 缓存；它们不是模型请求、任务延迟、TUI 就绪或总内存测量。CI 不设置时间阈值，也不把平台跳过算通过。

CI 在 Windows、Ubuntu、macOS 分别安装并检查实际客户端；原有三平台、四个 Python 版本的产品测试保留。[任务诊断工具](coding-benchmark.md)可导出相同代码与外部验收，真实编码适配、同模型成绩、复杂真实仓库、自然长会话、峰值内存仍待实施。真实任务需使用客户端正常流程，保留逐动作批准、隐私限制、失败与超限记录；缺失统计标为未知。mini 帮助适配器不能用于任务，也不能用 OpenCode 自动批准换取速度。
