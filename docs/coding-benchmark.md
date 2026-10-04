# 编码任务诊断与正式对照准备

这套集合验证定位、边界修复、跨文件修改、新功能、状态逻辑和长输入的基本行为。它是自建的小型诊断集合，不是 SWE-bench、Aider polyglot，也没有任何已完成的竞品成绩。必须先进行同模型、同输入的真实运行，再按失败证据改进；外部真实仓库、更多语言、复杂任务和自然长会话仍需补齐。

## 任务和验收

| 任务 | 语言 | 主要诊断 | 行为检查数 |
| --- | --- | --- | ---: |
| `py-interval` | Python | 半开区间、空区间、边界修复 | 6 |
| `py-pagination` | Python | 分页 API 与辅助模块的跨文件修复 | 7 |
| `py-grouping` | Python | 分组新功能、顺序、缺失值 | 3 |
| `py-lru` | Python | 有状态缓存、读取刷新、更新与淘汰 | 4 |
| `py-late-router` | Python | 大文件后部定位、路径边界与最长匹配 | 5 |
| `py-long-history` | Python | 合成长历史、保持既有 API 要求 | 4 |
| `js-stable-unique` | JavaScript | 顺序、不同值类型与对象身份 | 4 |
| `js-search` | JavaScript | 跨文件新功能、前缀与大小写 | 4 |

输入与已知参考实现位于 [cases.json](../benchmarks/coding/cases.json)。参考实现只用于离线校准，不预置到候选项目。验收比较实际函数输出及调用后的输入，拒绝缺失输出、错误类型和输入修改；不能只凭候选进程返回 0 判定成功。Python / Node 子进程有 10 秒超时和 65,536 字节输出上限，使用 Sprig 现有进程清理机制；验收只输出固定分类和失败检查序号，不回显候选错误正文。

```powershell
uv run --locked python scripts/benchmark-tasks.py --list
uv run --locked python scripts/benchmark-tasks.py --self-test
```

JavaScript 任务需要 Node.js。CI 显式安装 Node 24，在 Windows、Linux、macOS 与四个 Python 版本上测试；缺失 Node 是失败，不以跳过伪装通过。离线自检要求八个原始错误版本全部被拒绝、八个参考实现的 37 项检查全部通过，不调用模型，也不产生 agent 成绩。

## 单次真实 Sprig 运行

使用交互式终端、uv 环境和进程变量 `SPRIG_API_KEY` 或 `OPENAI_API_KEY`。凭据不作为命令参数、不写夹具或报告、不从历史对话和配置文件取回。默认沿用已有 Agnes 预设；可以通过 `--model`、`--base-url` 指定受信服务。

```powershell
.\scripts\benchmark-agnes.ps1 --run --case py-pagination --stream --request-usage --report .\pagination-run-01.json
```

未设置密钥时，这个 Windows 启动器使用隐藏输入，把凭据临时放入进程环境；结束后恢复原有变量。默认不带参数只列任务。普通跨平台入口是 `uv run --locked python scripts/benchmark-tasks.py --run --case py-pagination --stream --request-usage`；`--request-usage` 需要服务支持，不支持时不会自动重试。

每次只运行一个任务，默认最多 20 次模型轮次，`--max-steps` 可显式调整。每次文件写入和命令执行仍逐次显示完整动作并确认，默认拒绝；没有自动批准参数。非交互输入在调用模型前就被拒绝。用户固定验收命令在模型完成后由 Agent 执行，失败会返回模型用于继续修复；拒绝验收不能计为成功。临时项目在结束后清理，根路径或目录身份发生变化时保留并报告错误，不跟随变化后的目标删除。

报告包含输入、任务、初始历史、驱动脚本、锁文件和验收快照摘要，Python/Node 与平台、请求模型配置、验收结果、任务总耗时、扣除批准等待后的耗时、逐工具耗时及结果字节，以及 Provider 请求记录。模型名表示请求参数，不能证明网关实际使用的权重。记录总时间和批准等待时间，不能只展示扣除后的数字。服务缺失用量保持未知；任务失败、取消、验收被拒绝和验收器改变分别标记。`--report` 是显式导出数值与配置元数据，不含模型文本、工具参数、候选输出或密钥；既有文件不会覆盖，目标父目录必须已存在。

验收代码、期望值和一份 Sprig 执行器快照放在候选工作区之外，逐操作检查已知文件及目录清单。普通文件工具不能修改它们；检测到变动会使运行无效。获批命令和候选代码仍不受操作系统沙箱限制，可能访问工作区之外；这个校验不是对任意恶意程序的防作弊或安全隔离保证。通过 37 个样例也不能证明所有输入正确，仍需审查代码与扩大任务覆盖。

## 竞品使用相同输入

固定 Aider、OpenCode、Pi、mini-SWE-agent 的环境与跨平台元数据/帮助检查见 [客户端对照准备](client-comparison.md)。[mini SDK 任务入口](mini-task-runner.md)已接入七个相同初始代码/验收任务，保留 mini 自己的行动循环和逐动作批准。两边默认限制实际 JSON 请求体 131,072 字节，关闭自动重试；mini 新增传输适配目前仅在 Windows 本地验证。合成历史仍未对齐，真实模型成绩仍未取得。

```powershell
uv run --locked python scripts/benchmark-tasks.py --prepare C:\Users\25079\AppData\Local\Temp\sprig-comparison-01 --case py-pagination
```

指定一个新的空目标路径，已有项目不会被覆盖。结果给出 `workspace`、固定 `acceptance_argv` 和摘要；候选工作区仅含初始文件与 `TASK.md`，不会执行候选命令或请求模型。为 Sprig 和每个竞品分别准备新的副本，核对相同输入摘要；在各自正常界面完成同一任务，使用相同模型、受信服务、预算和验收定义，保持 Sprig 的逐次批准。竞争产品使用自己的正常工具流程，不能强迫它们遵循 Sprig 的脚本调用顺序。

`py-long-history` 的合成旧工具结果另放在 `initial-history.json`，当前仅用于 Sprig 历史压力诊断。其他客户端需要本机对话格式适配和单独的等价性验证；不能仅凭准备了相同文件宣称长会话输入相同，更不能把合成历史当作自然长任务成绩。

正式比较还需记录每个 agent 的版本/commit、实际任务成功与失败类型、重复运行与波动、请求数、延迟、用量、工具开销、启动和内存。外部客户端缺失的统计必须标为未知，不能拿 Sprig 的计时字段代替。当前工具提供统一输入与验收及 mini 的诊断适配，尚未完成相同完整预算下的真实性能对照。
