# Sprig Agent

一个小而直接的 Python 命令行编程代理：把任务交给支持工具调用的模型，由模型读取项目、提出修改、请求执行命令。你逐次确认每次写入和命令执行。

- Python 3.11+，运行时仅使用标准库
- 面向 Linux / macOS / Windows WSL 的 POSIX MVP（不支持原生 Windows 执行）
- 非流式 Chat Completions + function tools
- 可选本地会话保存与恢复

**先了解边界：Sprig 不是沙箱。** 文件工具有工作目录限制；你批准的命令仍以当前用户权限运行，能访问其他目录、网络和环境变量。请先在不含秘密的临时项目或独立容器中试用，不要在生产环境直接运行。

## 安装

在项目根目录执行：

Windows 用户请在 WSL 的 Linux 终端中执行以下步骤。原生 Windows 可查看 `--help` / `--version`，实际运行会明确报错，不会退化成较弱的文件保护。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install setuptools wheel
python -m pip install -e . --no-build-isolation
sprig --help
```

`setuptools` / `wheel` 仅用于打包；Sprig 本身没有第三方运行时依赖。离线环境需预先备好这些构建工具。也可以不安装，直接从源码运行：

```sh
PYTHONPATH=src python -m sprig_agent --help
```

## 开始使用

选择支持普通 Chat Completions function tools 的模型。模型名没有默认值，须通过 `--model` 或 `SPRIG_MODEL` 指定。

以下密钥输入示例适用于 Bash，输入不会回显，也不会把密钥文字写入命令历史：

```bash
read -r -s -p 'API key: ' SPRIG_API_KEY
export SPRIG_API_KEY
printf '\n'
export SPRIG_MODEL='替换为你的模型名'

sprig '阅读项目，说明结构并指出一个可以小幅改善的地方' --workdir ./my-project
```

也支持环境变量 `OPENAI_API_KEY`，优先使用 `SPRIG_API_KEY`。密钥只从环境变量读取，不接受命令行密钥参数，也不读取配置文件或 `.env`。不要把密钥放进任务文本。

使用其他兼容服务时：

```sh
export SPRIG_BASE_URL='https://your-provider.example/v1'
sprig '为现有函数补一个最小测试，并运行测试' --workdir ./my-project
```

基础地址须指向 API 根路径，程序在其后追加 `/chat/completions`。只允许 HTTPS，回环地址上的 HTTP 可用于本地服务或测试；HTTP 重定向被拒绝。请只配置你信任的服务，任务、读取的内容和工具结果会作为上下文发送到该服务。

### 确认流程

- 读取、列出和搜索文件无需额外确认
- 写文件前显示完整 diff；确认默认是拒绝
- 执行每条命令前显示完整参数列表和工作目录；确认默认是拒绝
- 没有自动批准开关。请逐项检查路径、代码和命令，不要机械地全部同意

例如，可让模型提出修改后，在 diff 提示处拒绝写入，先检查建议是否合理。拒绝会成为工具结果，模型可以据此调整方案。

## 命令行参数

```text
sprig [TASK] [--workdir PATH] [--model MODEL] [--base-url URL]
      [--max-steps INT] [--timeout SECONDS] [--max-output INT]
      [--save-session NAME | --resume NAME] [--session-dir PATH]
```

`python -m sprig_agent` 使用相同接口。

| 参数 | 默认值 / 说明 |
| --- | --- |
| `TASK` | 新会话必填；恢复时可省略，也可追加新任务 |
| `--workdir` | `.`，文件工具的工作根目录 |
| `--model` | `SPRIG_MODEL`；必须指定 |
| `--base-url` | `SPRIG_BASE_URL`，否则为 `https://api.openai.com/v1` |
| `--max-steps` | `20`，限制本次代理循环的模型调用次数 |
| `--timeout` | `30` 秒；网络 socket 的等待超时（非整个请求的总时限），命令的墙钟时限 |
| `--max-output` | `16000` 字节，工具输出上限；读取 / 搜索结果可截断，命令超出上限会被终止 |
| `--save-session NAME` | 显式启用会话保存 |
| `--resume NAME` | 恢复指定的已保存会话 |
| `--session-dir PATH` | 默认 `~/.local/state/sprig-agent/sessions`，须位于工作目录之外 |

## 工具

| 工具 | 行为 |
| --- | --- |
| `list_files(path=".")` | 列出目录内容 |
| `read_file(path)` | 读取 UTF-8 文本文件 |
| `search(query, path=".")` | 递归搜索文本中的字面字符串，不解释为正则表达式 |
| `edit_file(path, old, new)` | 将恰好一处 `old` 替换为 `new`；零处或多处匹配都拒绝 |
| `run(argv, cwd=".", timeout=...)` | 运行参数数组所指定的命令；可选超时不能超过全局上限 |

`edit_file` 的 `old` 为空时仅用于创建新文件，且父目录必须已存在。已有文件不能用空字符串匹配来覆盖。写入都需先检查 diff 并确认。

文件工具拒绝越出工作根目录的路径、符号链接，以及路径中包含 `.git`、`.env`、`.env.*` 的组件。它们只处理 UTF-8 文本，不能编辑二进制文件。

搜索会跳过不可读文件、二进制文件、`node_modules`、`.venv` 和 `__pycache__`，最多遍历 5,000 个条目、32 层目录、读取 8 MiB；单文件编辑和搜索读取限制为 512 KiB。结果带有截断标记，但“没有匹配”不代表被跳过的文件也已检查。

`run` 使用参数数组，不自动经过 shell。但批准 `sh`、`bash`、Python、项目脚本等命令仍可能执行任意代码。工作目录只决定命令从哪里启动，不限制它能去哪里。

## 保存与恢复

默认不保存会话。需要恢复时显式启用：

```sh
sprig '检查现有测试并提出一个小修复' --workdir ./my-project --save-session small-fix
sprig --workdir ./my-project --resume small-fix
sprig '接下来验证修改' --workdir ./my-project --resume small-fix
```

恢复时须继续使用原来的工作根目录、模型和服务地址。任一绑定发生变化都会拒绝恢复，避免把已有上下文误发给另一服务。恢复不会重放旧的工具调用；未得到结果的调用会标记为中断。

会话保存到工作目录之外，使用 POSIX 仅所有者可访问的权限和原子替换。会话可能包含源码、任务内容、模型回复与命令输出，**没有加密，也不是秘密保险箱**。保存本身不会额外上传会话文件；恢复并继续调用模型时，历史上下文会发送给原先绑定的服务。请自行管理保留期限，避免提交或共享会话目录。

同一会话只应由一个 Sprig 进程使用；此 MVP 不提供并发会话锁。原子保存可避免半份文件，不能合并并发运行的历史。

## 安全地验证

先运行本地测试，无需 API 密钥：

```sh
python -m unittest discover -s tests -v
```

尚未执行真实付费 API 的端到端验证。测试通过只证明覆盖到的本地行为，不代表任意兼容服务都能正常工作。

第一次连接真实模型时，建议：

1. 使用没有凭证、私人文件和生产数据的小型临时项目
2. 先提交一个仅阅读的任务，确认模型和服务支持工具调用
3. 检查每个 diff 和命令；看不懂的操作就拒绝
4. 完成后查看 `git diff` 并自行运行测试；模型说“完成”不等于验证通过

项目内的文件、命令输出和模型回复都可能包含误导性指令。确认提示和系统提示不能消除提示注入风险。工具结果会送回模型，批准命令前也要考虑其输出是否包含不宜上传的数据。

## 范围与限制

- `run` 没有操作系统级隔离，可能访问工作目录之外的文件、网络或其他环境变量中的秘密。程序会移除子进程环境中的 API 密钥变量，并遮盖已知密钥的原文；这不等于能检测或隐藏任意秘密
- 文件工具的路径检查不构成面对恶意并发文件修改、硬链接或任意命令的完整安全边界。需要隔离时，请另外使用受限账户或容器
- 仅支持文本任务和普通 function tools；没有流式输出、图片、浏览器、MCP、多代理、自动重试或自动上下文压缩
- `--max-steps` 限制循环步数，不是 token / 费用预算。长会话仍可能超出服务的上下文限制
- 网络请求不自动重试，避免不明确地重复计费；命令超时使用 POSIX 进程组终止机制，不承诺清理主动脱离进程组的进程
- HTTP 超时是 socket 等待超时，持续缓慢返回的响应可能超过所设秒数；响应仍有 2 MiB 大小限制，可用 Ctrl-C 中断
- “OpenAI-compatible” 不保证模型、工具格式和响应行为兼容；所选服务与模型必须支持本项目发送的 Chat Completions function tools
- Windows 尚不作为此 MVP 的支持目标；文件权限与进程控制按 Linux / macOS POSIX 语义设计

本项目用标准库 `urllib` 发送非流式 `POST /chat/completions`。OpenAI 在[官方 Chat API 文档](https://developers.openai.com/api/reference/resources/chat)中建议新项目尝试 Responses API；这里为了保留更广泛的兼容服务接入，明确选择 Chat Completions。若只接入 OpenAI，可按实际需求另行迁移。

## 源码导览

- `src/sprig_agent/cli.py` / `__main__.py`：参数解析与命令行入口
- `src/sprig_agent/config.py`：环境变量与运行配置校验
- `src/sprig_agent/provider.py`：Chat Completions HTTP 请求
- `src/sprig_agent/agent.py`：模型与工具之间的代理循环
- `src/sprig_agent/tools.py`：文件工具、diff 确认和命令执行
- `src/sprig_agent/session.py`：可选会话保存与恢复
- `tests/`：可离线运行的标准库 `unittest` 测试
- `pyproject.toml`：包元数据和 `sprig` 命令入口

## 许可

[MIT License](LICENSE)，Copyright © 2026 Sprig contributors。
