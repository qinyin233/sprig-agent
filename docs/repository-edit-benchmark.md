# 真实仓库的文件编辑基线

本基线使用 [Click issue 3107](https://github.com/pallets/click/issues/3107) 及其[已接受修复 PR 3228](https://github.com/pallets/click/pull/3228)：为拼错的子命令提供候选建议，并统一未知选项的消息格式。修复涉及 4 个源码文件、8 个独立片段，可检查同文件分散改动带来的重复 diff、备份和写入开销。

输入固定在修复前 `19fd4d6e18bc9fce451f92f422696b11169faa57` 和修复后 `831c8f0948af519e45b90801d7430ff25451f972`。每份快照包含源码、测试、许可、README 和项目配置共 52 个文件；保留上游 BSD-3-Clause 许可。它是选定范围的真实仓库快照，未包含项目的全部文档、示例或全部测试执行协议。

## 重复运行

独立 uv 环境锁定 Python 3.12、pytest 8.4.2、Windows Colorama 及 Sprig 的运行依赖，不改变产品环境。两版工具在同一环境中运行：

```powershell
uv sync --project benchmarks/repositories/click --locked
$sprigClickFixture = Join-Path $env:TEMP ('sprig-click-' + [guid]::NewGuid().ToString('N'))
uv run --project benchmarks/repositories/click --locked python -I -B scripts/benchmark-edit-repo.py --fetch $sprigClickFixture
uv run --project benchmarks/repositories/click --locked python -I -B scripts/benchmark-edit-repo.py --fixture $sprigClickFixture --compare-ref e01ec46945a2da29ddfea0bdf4a3fac784624625 --repeat 11 --report click-edit-01.json
```

普通 POSIX 入口相同，将 `$sprigClickFixture` 替换为已有父目录内的全新目录路径。`--fetch` 只下载两个固定公开 HTTPS 地址，校验 archive 和快照 SHA-256，不执行代码；测量阶段离线读取快照。下载最多 4 MiB、解压最多 16 MiB、遍历最多 2,048 项，拒绝路径逃逸、链接、重复文件和过大文件。既有下载目标和报告不会覆盖；下载失败留下的目录不能通过快照校验。只使用受信的本地 Git revision 作为 `--compare-ref`。

工作进程通过原生 Windows Job Object / POSIX 进程组运行，120 秒超时、64 KiB 标准输出上限，使用一次性 HOME 和配置目录、过滤的进程环境，禁用 pytest 插件自动加载。已知补丁只应用到一次性候选副本；脚本中的立即批准属于固定验证夹具，产品仍需每个文件的新批准。工具计时包含匹配、隐私校验、合并 diff、备份和实际写入，排除导入、输入构造、pytest 和工作进程启动；这里没有模型或真实用户等待。

## 校准和结果

外部固定验收使用修复后的 `tests/test_commands.py`、`tests/test_options.py`、原始配置和 fixture，导入候选副本中的 Click 源码。使用上游的正常 pytest `prepend` 模式，测试未裁剪；明确要求收集 642 项、零跳过。修复前必须出现指定的 7 项失败并通过其余 635 项，修复后必须 642 项全部通过。拒绝零用例、失败项目变化或无退出码依据的通过判断。

[Windows 原始报告](benchmarks/multi-edits-click-windows.json)记录了两版实际运行的源码摘要、驱动摘要、依赖锁文件摘要、原始输入与验收身份及全部重复数据。新版对同一个文件调用一次 `edits`，旧版逐片段调用原来的 `old/new`；两边都实际应用同一已知补丁，不用估算代替工具执行。

| 项目 | 旧版工具 | 同文件多片段编辑 |
| --- | --- | --- |
| 每次修复的文件数 / 片段数 | 4 / 8 | 4 / 8 |
| 工具调用、批准、实际文件写入 | 8 | 4 |
| 最终文件字节 | 与上游修复完全一致 | 与上游修复完全一致 |
| 实际工具结果的上游检查 | 642 通过，零跳过 | 642 通过，零跳过 |

时间见原始报告。重复测量只有这一个 Windows 案例，未清空 OS 缓存，不能外推任意仓库。旧版也能在一条模型回复中提出多个工具调用，所以工具调用减少不等于模型请求轮次、token、费用或整个任务延迟同比减少。

本入口根据已知上游补丁构造编辑参数，测量文件工具的执行效果；模型没有定位或生成修复。这不是模型编码成功率、完整 Click 测试、SWE-bench 分数或顶尖 agent 对照。真实同模型仓库任务、自然长会话、多语言和峰值进程内存仍需继续验证。新增项目前仅有 Windows 本地证据，远程 POSIX 检查将随大更新执行。
