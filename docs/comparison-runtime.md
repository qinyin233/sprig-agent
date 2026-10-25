# 比较环境必须包含完整的 Sprig 依赖

`benchmarks/clients/mini` 同时承载 mini SDK 参考入口和 Sprig 对照入口，后者会使用 Sprig 的搜索、源码解析和附件工具。该项目现在显式依赖本仓库的 `sprig-agent` editable 包，uv 从根 `pyproject.toml` 解析运行依赖并写入 mini 锁文件。无需手工补装库，也不依赖其他环境中的偶然安装。

```powershell
uv sync --project benchmarks/clients/mini --locked --python 3.12
uv run --project benchmarks/clients/mini --locked python -m unittest discover -s benchmarks/clients/tests -q
uv run --project benchmarks/clients/mini --locked python -m unittest tests.test_grep tests.test_syntax tests.test_image_inputs -q
uv run --project benchmarks/clients/mini --locked python -I -B scripts/benchmark-tasks.py --self-test
```

在仓库根运行。模型运行、逐次批准和临时工作区规则仍见[编码诊断](coding-benchmark.md)及 [mini 任务入口](mini-task-runner.md)；这些离线检查不请求模型，也不代表编码成绩。真实运行的密钥只从进程环境读取。

## 本次发现与修正

2026-10-25，在此前使用的 Windows mini 环境目录内，当前预检发现 10 个 Sprig 运行依赖缺失：RE2、Pillow、tree-sitter 及七个语法包。直接运行已有的正则、语法和图片检查，各产生一个错误。仅检查 SDK 自己能否启动，不能证明同一环境中的 Sprig 工具完整可用。

修正仅增加本地包依赖和锁条目。mini 原有 86 个锁定包的版本保持，增加 Sprig 本身及上述 10 个依赖；两端均核对了 Sprig 的 13 项直接依赖约束，并在这个环境中实际运行原有的 40 项工具回归和 25 项 SDK 回归。没有新增仅检查配置文本的重复测试，也没有改应用、模型循环、提示、固定验收或原始任务。

该共同环境使用 mini 锁文件解析完整依赖图，满足 Sprig 声明的版本范围；它不承诺与根 uv 锁文件的所有传递版本完全相同。机器记录保存各环境的直接依赖版本、安装分布清单摘要及锁摘要。原有 mini 版本保持，不能把它写成升级 SDK 后得到的结果。

首次离线安装只因缺少当前 Python 3.12 的 Pillow wheel 而停止，随后按同一锁文件下载了相应 wheel；没有改版本、关闭 TLS 或使用非锁定安装。完整范围见[本地记录](benchmarks/comparison-runtime-20261025.json)。远程 CI 继续暂停，Windows 与 WSL 的本地结果不代替原生远程 runner。

## 历史结果的边界

旧报告没有保存完整依赖清单。今天复用目录中的缺失状态，不能证明历史运行时也缺失；原有任务通过/失败、用量、崩溃和预算记录保持。它同样不能证明当时已满足完整运行依赖，因此历史对照需保留这一未核实条件，不据此推断全部工具能力或整体排名。

后续真实复测应在修正后的锁定环境上固定源码、任务、预算与顺序，分别保留两边原生报告，并在循环后另行批准独立验收。应用源码与环境、校准与模型成绩分别记录；之前中断的任务不会因本次离线检查通过而变成成功。

此后已在修正提交上完成[一次新的真实搜索任务配对](live-evaluation-search-pair-20261025.md)。两边均通过 4/4，原生数值与自检失败过程另页保留；它属于后续模型试验，不将本页的依赖/离线检查改写成模型成绩。
