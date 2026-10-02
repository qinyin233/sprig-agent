# Sprig Agent

一个小而直接的 Python 命令行编程代理：把任务交给支持工具调用的模型，由模型读取项目、提出修改、请求执行命令。你逐次确认每次写入和命令执行。

- Python 3.11+，模型与工具核心使用标准库，TUI 使用 Textual
- Linux / macOS / Windows 10+ / Windows WSL
- Chat Completions + function tools，可选 SSE 流式输出
- 全屏终端 TUI、连续对话、项目代码地图与本地 Git 查看
- 用户固定验收命令：失败反馈修复，通过后才确认验收完成
- 可选本地会话保存与恢复
- 只读分析、上下文预算、敏感文件排除、可审阅的文件编辑撤销与重做

**先了解边界：Sprig 不是沙箱。** 文件工具有工作目录限制；你批准的命令仍以当前用户权限运行，能访问其他目录、网络和环境变量。请先在不含秘密的临时项目或独立容器中试用，不要在生产环境直接运行。

## 安装

使用 [uv](https://docs.astral.sh/uv/) 管理 Python、虚拟环境和锁文件。在项目根目录执行，Windows PowerShell 与 POSIX 终端相同：

```sh
uv sync --locked --managed-python
uv run sprig --help
uv run python -m unittest discover -s tests -v
```

`.python-version` 固定开发主版本为 3.13，`.venv` 由 uv 管理，`uv.lock` 提交到 Git；包仍支持 Python 3.11+。`--managed-python` 使用 uv 管理的 CPython，避免选择 Microsoft Store 应用别名。无需手动激活环境或使用 pip 安装。Textual 及其依赖由 uv 锁定安装；模型、文件、会话和命令核心仍使用标准库。构建由 uv 管理隔离的 `setuptools` / `wheel` 环境，离线使用需预先准备依赖和构建缓存。

Windows 中文路径下若必须使用 Python 3.11，请在 `uv sync` 和 `uv run` 上都加 `--no-editable`，避开旧版 Python 对 editable `.pth` 路径的编码限制。默认的 3.13 开发环境无需此选项。

在同一 Windows/WSL 共享检出中，请为 WSL 使用独立环境，避免替换 Windows 的 `.venv`：

```sh
export UV_PROJECT_ENVIRONMENT=.venv-wsl
uv sync --locked
uv run sprig --help
```

也可以仅查看源码入口，不创建环境：

```sh
PYTHONPATH=src python -m sprig_agent --help
```

PowerShell 源码运行：`$env:PYTHONPATH = 'src'; python -m sprig_agent --help`。

### Agnes 3.0 Flash 预设

已按 Agnes 的[模型说明](https://wiki.agnes-ai.com/en/docs/agnes-30-flash)接入 `agnes-3.0-flash`，Base URL 为 `https://apihub.agnes-ai.com/v1`。

在 PowerShell 项目根目录执行：

```powershell
.\scripts\run-agnes.ps1
.\scripts\run-agnes.ps1 --tui --workdir . --save-session coding
.\scripts\run-agnes.ps1 '阅读项目并说明结构' --stream --workdir .
```

不带参数时进入全屏 TUI，默认流式输出。启动器使用 `uv run --locked --managed-python` 运行项目环境；未设置密钥环境变量时，会提示隐藏输入。模型和地址已有预设，密钥只进入当前进程环境，退出后恢复原环境，不写入 `.env`、源码、配置文件或会话。脚本遵循本机 PowerShell 执行策略。

## 开始使用

选择支持普通 Chat Completions function tools 的模型。模型名没有默认值，须通过 `--model` 或 `SPRIG_MODEL` 指定。

以下密钥输入示例适用于 Bash，输入不会回显，也不会把密钥文字写入命令历史：

```bash
read -r -s -p 'API key: ' SPRIG_API_KEY
export SPRIG_API_KEY
printf '\n'
export SPRIG_MODEL='替换为你的模型名'

uv run sprig '阅读项目，说明结构并指出一个可以小幅改善的地方' --workdir ./my-project
```

也支持环境变量 `OPENAI_API_KEY`，优先使用 `SPRIG_API_KEY`。密钥只从环境变量读取，不接受命令行密钥参数，也不读取配置文件或 `.env`。不要把密钥放进任务文本。

使用其他兼容服务时：

```sh
export SPRIG_BASE_URL='https://your-provider.example/v1'
uv run sprig '为现有函数补一个最小测试，并运行测试' --workdir ./my-project
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
      [--stream] [--chat | --tui] [--check JSON_ARGV ...]
      [--save-session NAME | --resume NAME] [--session-dir PATH]
      [--change-history-dir PATH]
```

`python -m sprig_agent` 使用相同接口。

| 参数 | 默认值 / 说明 |
| --- | --- |
| `TASK` | 新会话必填；使用 `--chat`、`--tui` 或恢复会话时可省略 |
| `--workdir` | `.`，文件工具的工作根目录 |
| `--model` | `SPRIG_MODEL`；必须指定 |
| `--base-url` | `SPRIG_BASE_URL`，否则为 `https://api.openai.com/v1` |
| `--max-steps` | `20`，限制本次代理循环的模型调用次数 |
| `--timeout` | `30` 秒；网络 socket 的等待超时（非整个请求的总时限），命令的墙钟时限 |
| `--max-output` | `16000` 字节，工具输出上限；读取 / 搜索结果可截断，命令超出上限会被终止 |
| `--stream` | 可选；逐步显示模型文本，工具调用仍在完整响应校验通过后执行 |
| `--read-only` | 只允许文件检查；工具入口强制拒绝所有写入和命令，不可搭配 `--check` |
| `--inherit-env` | 显式允许获批命令继承更多环境变量；已知模型密钥仍移除 |
| `--max-context-bytes` | 默认 131072；限制实际序列化请求大小，范围 4096–2097152；不等同 token 或费用预算 |
| `--compact-context` | 显式启用旧成功工具输出压缩；用户要求、失败、拒绝和最近六条消息保留 |
| `--chat` | 可选；在同一上下文中连续输入任务，须在交互式终端运行 |
| `--tui` | 全屏终端界面，自动启用流式输出；标准输入和输出都须连接终端 |
| `--check JSON_ARGV` | 可重复指定固定验收命令，最多 16 条；每次执行都需要确认 |
| `--save-session NAME` | 显式启用会话保存 |
| `--resume NAME` | 恢复指定的已保存会话 |
| `--session-dir PATH` | 默认 `~/.local/state/sprig-agent/sessions`，须位于工作目录之外 |
| `--list-sessions` | 列出本地会话元数据，无需模型或密钥，不上传对话 |
| `--delete-session NAME` | 逐次确认后删除本地会话文件；非交互输入默认拒绝 |
| `--change-history-dir PATH` | 显式持久化精确修改备份，目录须位于工作空间/仓库之外，使用所有者私有权限；不加密 |

### 上下文预算

请求在实际发送前检查字节预算，超限时停止，不会请求模型或自动删除历史。CLI `/context` 和 TUI“查看上下文预算”显示当前请求大小、压缩后大小以及将缩减的消息、字段和字节数。CLI `/compact` 或 TUI“压缩旧工具输出”需要确认；也可启动时明确指定 `--compact-context`。

压缩只影响下一次发送的请求，不修改保存的完整工具输出。用户要求、拒绝、失败与验收结果、工具调用顺序和最近六条消息保留；旧成功读取或命令的正文替换为省略标记，模型需要时应重新检查实际文件。无法压缩的长要求仍会被预算拦截，可以由用户增加预算或开始新会话。

### 会话管理

```sh
uv run --locked sprig --list-sessions
uv run --locked sprig --delete-session coding
```

TUI 的操作页也提供会话列表和删除按钮。列表显示名称、模型、目录和消息数量，不显示对话正文。删除默认拒绝，不改变项目文件。

新会话名称在使用前即被保留，保存或恢复后会持有独占锁直到切换或退出。同一个会话不能被两个 Sprig 进程同时加载、覆盖或删除。锁由操作系统句柄管理，进程崩溃后自动释放；残留的锁文件无需手动删除，运行期间删除锁文件会破坏并发保护。

### 文件编辑撤销与重做

TUI 提供“查看修改记录”“撤销最近文件编辑”“重做文件编辑”；CLI 连续对话提供 `/changes`、`/undo`、`/redo`。修改记录只包含 `edit_file` 的精确前后版本，最多保留 16 次、合计 4 MiB，达到上限会淘汰最旧记录。默认只在当前进程内保留；需要退出后继续撤销时显式启用：

```sh
uv run --locked sprig --tui --model YOUR_MODEL --change-history-dir "~/.local/state/sprig-agent/changes"
```

持久化目录不加密，存储源码副本，不能放在工作空间或 Git 仓库内。同一工作目录的持久化记录有独占锁。源码中的已知模型密钥或可识别凭证不会进入编辑备份，这不保证识别任意秘密。

撤销与重做都先显示完整 diff，默认拒绝，批准后再次检查当前文件身份、内容和隐私规则。文件被编辑器或命令修改时拒绝覆盖，其他用户文件保持原样；撤销新建文件会确认删除该文件，重做可以重建。新编辑会清除原重做分支，只读模式阻止撤销和重做。

获批命令的改动没有记录，无法用这些按钮恢复；这里没有 Git 工作树隔离。撤销会使之前的验收结果失效，该提示进入会话，下一次继续须检查并重新验收。持久化意图先于文件写入保存；中断或备份保存失败留下“结果不确定”记录，后续编辑与恢复会停止，不自动重放。先检查实际文件和备份，再通过 `/discard-history` 或 TUI“清除修改备份”明确确认放弃记录；清除不会更改工作空间文件。

## 工具

### 文件隐私与只读模式

使用 `uv run --locked sprig --tui --read-only --model YOUR_MODEL` 进行项目分析。只读模式由工具入口执行，模型看不到写入和命令工具；即使手工调用，也会被拒绝。验收命令可能修改文件，因此只读模式不接受 `--check`。

文件工具默认排除 `.ssh`、`.aws`、`.kube`、`.netrc`、私钥、凭证文件及 `.pem` / `.key` 等敏感名称，并阻止上传检测到的常见 API key 或私钥正文。这只是部分格式检测，不能识别任意秘密。

项目根目录可放 `.sprigignore`，每行一个相对路径或 glob，例如 `private/`、`*.secret`、`data/*.csv`。最多 256 条、32 KiB；`#` 开头是注释。该文件只能增加排除项，不支持 `!` 重新放行，读取、搜索、地图和编辑都遵守它。普通文件工具每次操作重新读取规则；获批编辑、撤销和命令在确认后再次检查路径规则，规则文件无效时拒绝操作。

命令默认只继承 PATH、系统目录、用户目录、临时目录、语言和终端等基础环境变量，减少其他服务凭证的意外继承。需要代理等自定义环境时，可由用户明确加 `--inherit-env`；命令仍有访问本机文件和网络的能力。

CLI 与 TUI 都实时显示命令输出，保留 UTF-8 分段，遮盖跨片段的已知密钥并转义终端控制字符。显示和返回模型的结果受相同输出上限约束，超限会停止命令。

| 工具 | 行为 |
| --- | --- |
| `list_files(path=".")` | 列出目录内容 |
| `read_file(path)` | 读取 UTF-8 文本文件 |
| `search(query, path=".")` | 递归搜索文本中的字面字符串，不解释为正则表达式 |
| `project_map(path=".")` | 返回有界文件地图和 Python 类、函数、方法的名称与行号；其他语言只列文件路径 |
| `edit_file(path, old, new)` | 将恰好一处 `old` 替换为 `new`；零处或多处匹配都拒绝 |
| `run(argv, cwd=".", timeout=...)` | 运行参数数组所指定的命令；可选超时不能超过全局上限 |

`edit_file` 的 `old` 为空时仅用于创建新文件，且父目录必须已存在。已有文件不能用空字符串匹配来覆盖。写入都需先检查 diff 并确认。

文件工具拒绝越出工作根目录的路径、符号链接，以及路径中包含 `.git`、`.env`、`.env.*` 的组件。它们只处理 UTF-8 文本，不能编辑二进制文件。

工具路径在所有系统上都用 `/` 分隔。Windows 还拒绝 junction 等重解析点、设备名、备用数据流、尾随点和空格，并按大小写不敏感方式保护 `.git` / `.env`；通过文件句柄检查 DOS 短名对应的实际路径。文件编辑保留 POSIX 权限位或原文件的 Windows DACL。

搜索会跳过不可读文件、二进制文件、`node_modules`、`.venv`、`.venv-*` 和 `__pycache__`，最多遍历 5,000 个条目、32 层目录、读取 8 MiB；单文件编辑和搜索读取限制为 512 KiB。结果带有截断标记，但“没有匹配”不代表被跳过的文件也已检查。

代码地图采用相同遍历与读取预算。它使用 Python AST 提取声明，没有跨语言语义索引或调用图；无效或过大的 Python 文件会标记 `symbols_unavailable`。请继续读取相关源码确认行为。

`run` 使用参数数组，不自动经过 shell。但批准 `sh`、`bash`、Python、项目脚本等命令仍可能执行任意代码。工作目录只决定命令从哪里启动，不限制它能去哪里。

Windows 的 `.bat` / `.cmd` 文件需要显式请求 `cmd.exe /c`；不会自动套用 shell。命令使用从创建起就关联的 Windows Job Object，超时、输出超限、异常和 Ctrl-C 都清理作业中的子进程。无法创建作业或配置句柄时会拒绝执行。

Windows 应用执行别名（`APPEXECLINK`）以及 `pyvenv.cfg` 指向这类别名的 Python 环境会被拒绝：应用代理可能让进程逃出 Job，无法保证超时清理。遇到提示时用 `uv sync --locked --managed-python --python 3.13` 重建环境。批准 shell 或其他启动器仍可能间接调用代理，Job 机制不构成面对任意恶意程序的沙箱。

## 流式与连续对话

### 全屏终端界面

```sh
uv run sprig --tui --model your-model --workdir ./my-project
```

Windows 推荐使用 Windows Terminal 与 PowerShell；也可在 Linux、macOS 终端运行。界面使用 [Textual](https://textual.textualize.io/)，不依赖浏览器。

- 左侧对话区逐步显示模型输出；下方是持续可用的对话输入框，Enter 或“发送”按钮提交，Shift+Enter 换行。不能区分 Shift+Enter 的终端可用 Ctrl+J 换行；粘贴多行文字不会自动发送。空消息不发起请求。
- 执行中也能发送新要求，连续补充按发送顺序进入同一会话，由单一工作线程处理。等待中的模型响应结束后，应用丢弃其中过时的操作并按新要求继续；已批准且正在执行的命令会先完成并保存实际结果，其余未执行调用停止。需要立即停止命令时使用 Ctrl+C。
- 右侧操作栏提供 Git diff、Git 状态和项目地图；“事件”标签显示工具结果与验收状态。这些本地查看结果不会自动发送给模型。
- 每次修改弹出完整 diff，每次命令弹出 argv、工作目录和资源限制。确认窗默认聚焦“拒绝”，此时 Enter 默认拒绝，Esc 拒绝；批准需要选择“批准这次操作”。确认窗中的调整输入框同样使用 Enter 发送，发送会让这次待确认操作作废，模型重新规划后须取得新的批准；Ctrl+L 可聚焦调整输入框。
- 会话名称框配合“保存”“恢复”按钮使用。保存后自动记录后续轮次；恢复替换已有内存对话时先确认，不自动发起模型请求或重放工具。恢复后输入新任务，或点击“继续当前对话”。也支持原有 `--save-session` / `--resume` 参数。
- Ctrl+C 或“取消”停止当前任务，清理正在运行的命令及其正常后代进程；待确认操作不会执行。网络请求采用协作取消，等待中的 socket 可能仍需等到数据返回或超时。已经接收的补充要求保留在对话中，点击“继续当前对话”或发送新消息时采用；取消不会自动启动下一轮。
- Ctrl+Q 退出；任务运行时先取消并等待清理，不立即丢下工作线程。Ctrl+L 聚焦任务输入。

TUI 的可见对话保留最近 64,000 字符，事件区最多 1,000 行；这些显示上限不会压缩模型上下文或改变完整会话保存。暂不提供并行任务队列、逐行命令输出或工具权限自动批准。

### 简单连续对话

```sh
uv run sprig --chat --stream --model your-model --workdir ./my-project
```

每次输入的任务沿用当前对话上下文，`--max-steps` 仍按每次任务计算。`/help` 查看用法，`/status` 查看本地上下文，`/exit` 退出。`/diff` 和 `/git` 在确认后分别执行 `git diff -- .`、`git status --short -- .`；这些本地查看结果不会自动发送给模型。

流式模式按 SSE 事件拼接文本和工具参数，支持交错的多个 function tools。必须收到完整结束原因和 `[DONE]` 才接受工具调用；断流、格式错误和超限时不执行这次响应的工具，也不保存半份助手消息。已经显示的部分文本不代表任务完成，未完整输出的参数也不会请求执行。已知密钥跨文本片段出现时仍会遮盖。会话保存完整的最终消息，恢复时可以切换流式开关。

## 独立验收

给任务指定由应用固定执行的验收命令。例如：

```sh
uv run sprig '修复计算逻辑' --check '["python","-m","unittest","discover","-s","tests"]'
```

PowerShell：

```powershell
.\scripts\run-agnes.ps1 '修复计算逻辑' --stream --check '["python","-m","unittest","discover","-s","tests"]'
```

模型结束一轮工作后，应用自己请求确认并执行这些命令。全部命令实际返回 0、没有超时或输出超限时，才输出 `[acceptance] All user checks passed`；失败会把实际工具结果交回模型继续修复，仍受 `--max-steps` 限制。拒绝验收命令会停止本次任务并保留未验收状态，不让模型绕过拒绝。

命令意图在执行前保存，结果在执行后保存；中断恢复不会重放它们。验收命令也绑定到保存的会话，恢复时必须提供相同的 `--check` 参数，避免静默丢失用户的验收条件。它们使用工作根目录、全局超时和输出上限。

验收标准由你负责定义；返回 0 只证明这些命令在该次运行中通过。没有指定 `--check` 时，模型正常结束的退出码 0 不表示代码已通过测试。命令没有沙箱，验收也仍需检查并批准。

## 保存与恢复

默认不保存会话。需要恢复时显式启用：

```sh
uv run sprig '检查现有测试并提出一个小修复' --workdir ./my-project --save-session small-fix
uv run sprig --workdir ./my-project --resume small-fix
uv run sprig '接下来验证修改' --workdir ./my-project --resume small-fix
```

恢复时须继续使用原来的工作根目录、模型、服务地址和验收命令。任一绑定发生变化都会拒绝恢复，避免把已有上下文误发给另一服务或丢失验收条件。恢复不会重放旧的工具调用；未得到结果的调用会标记为中断。

会话保存到工作目录之外，使用 POSIX 的 `0700` / `0600` 权限或 Windows 受保护的当前用户专属 DACL，以及原子替换。已有目录或文件权限不合要求时拒绝使用，不会自动更改它们的权限。会话可能包含源码、任务内容、模型回复与命令输出，**没有加密，也不是秘密保险箱**。保存本身不会额外上传会话文件；恢复并继续调用模型时，历史上下文会发送给原先绑定的服务。请自行管理保留期限，避免提交或共享会话目录。

同一会话只应由一个 Sprig 进程使用；此 MVP 不提供并发会话锁。原子保存可避免半份文件，不能合并并发运行的历史。

## 安全地验证

先运行本地测试，无需 API 密钥：

```sh
uv run python -m unittest discover -s tests -v
```

2026-10-02 已在原生 Windows 上使用 Agnes 3.0 Flash 完成普通和流式 API 的端到端测试：读取临时项目、修复函数、执行固定验收命令并保存恢复会话。[验证记录](docs/validation.md)列出实际测试范围，不代表任意兼容服务都能正常工作。

可选真实 API 测试（会使用账户额度）：

```sh
uv run python scripts/live-smoke.py
uv run python scripts/live-tui-smoke.py
```

此脚本只上传临时 `calc.py` 测试项目，测试夹具只批准一项精确修复和固定的验收 argv；其他写入和命令一律拒绝。使用 `SPRIG_API_KEY` / `OPENAI_API_KEY`、`SPRIG_MODEL`、`SPRIG_BASE_URL` 环境变量，没有测试密钥默认值。普通单元测试不会连接 API。

第一次连接真实模型时，建议：

1. 使用没有凭证、私人文件和生产数据的小型临时项目
2. 先提交一个仅阅读的任务，确认模型和服务支持工具调用
3. 检查每个 diff 和命令；看不懂的操作就拒绝
4. 完成后查看 `git diff` 并自行运行测试；模型说“完成”不等于验证通过

项目内的文件、命令输出和模型回复都可能包含误导性指令。确认提示和系统提示不能消除提示注入风险。工具结果会送回模型，批准命令前也要考虑其输出是否包含不宜上传的数据。

## 范围与限制

- `run` 没有操作系统级隔离，可能访问工作目录之外的文件、网络或其他环境变量中的秘密。程序会移除子进程环境中的 API 密钥变量，并遮盖已知密钥的原文；这不等于能检测或隐藏任意秘密
- 文件工具的路径检查不构成面对恶意并发文件修改、硬链接或任意命令的完整安全边界。需要隔离时，请另外使用受限账户或容器
- 仅支持文本任务和普通 function tools；没有图片、浏览器、MCP、多代理或自动重试；上下文压缩需要用户明确启用
- `--max-steps` 限制循环步数，`--max-context-bytes` 限制发送大小；均不是精确 token / 费用预算，也不保证适合每个模型的上下文窗口
- 网络请求不自动重试，避免不明确地重复计费；POSIX 命令使用进程组终止机制，不承诺清理主动脱离进程组的进程；Windows 命令使用 Job Object
- HTTP 超时是 socket 等待超时，持续缓慢返回的响应可能超过所设秒数；响应仍有 2 MiB 大小限制，可用 Ctrl-C 中断
- “OpenAI-compatible” 不保证模型、工具格式和响应行为兼容；所选服务与模型必须支持本项目发送的 Chat Completions function tools
- Windows 需要支持受保护 DACL 的文件系统及 Job Object API（Windows 10+）；不支持 FAT/exFAT 会话目录，或权限/作业策略禁止必要 API 的环境

本项目用标准库 `urllib` 发送 `POST /chat/completions`，默认非流式，`--stream` 使用 SSE。OpenAI 在[官方 Chat API 文档](https://developers.openai.com/api/reference/resources/chat)中建议新项目尝试 Responses API；这里为了保留更广泛的兼容服务接入，明确选择 Chat Completions。若只接入 OpenAI，可按实际需求另行迁移。

## 源码导览

- `src/sprig_agent/cli.py` / `__main__.py`：参数解析与命令行入口
- `src/sprig_agent/tui.py`：Textual 全屏终端界面、确认窗、任务取消与本地会话操作
- `src/sprig_agent/config.py`：环境变量与运行配置校验
- `src/sprig_agent/provider.py`：Chat Completions HTTP 请求
- `src/sprig_agent/streaming.py`：有界 SSE 解码、文本遮盖和工具片段组装
- `src/sprig_agent/agent.py`：模型与工具之间的代理循环
- `src/sprig_agent/tools.py`：文件工具、diff 确认和命令执行
- `src/sprig_agent/filesystem.py` / `windows.py`：跨平台文件句柄、Windows 私有 ACL 与进程作业
- `src/sprig_agent/session.py`：可选会话保存与恢复
- `tests/`：可离线运行的标准库 `unittest` 测试
- `pyproject.toml`：包元数据和 `sprig` 命令入口
- `.python-version` / `uv.lock`：uv 管理的开发 Python 版本与依赖锁文件
- `scripts/run-agnes.ps1`：不存储密钥的 Agnes PowerShell 启动器
- `scripts/live-smoke.py` / `live-tui-smoke.py`：可选真实 API 端到端测试，仅批准精确的临时夹具操作
- [同类项目对照与开发路线](docs/agent-comparison.md)：参考实现、当前覆盖和后续差距
- [差异化调研与产品提案](docs/sprig-differentiation.md)：现有先例、需求变更工作台与分阶段验收标准；提案功能尚未实现
- [其他产品方向调研](docs/sprig-alternative-directions.md)：首次跑通项目、失败复现卡、编程陪练等较小范围的产品方案

## 许可

[MIT License](LICENSE)，Copyright © 2026 Sprig contributors。
