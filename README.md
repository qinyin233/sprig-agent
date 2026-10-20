# Sprig Agent

一个小而直接的 Python 命令行编程代理：把任务交给支持工具调用的模型，由模型读取项目、提出修改、请求执行命令。你逐次确认每次写入和命令执行。

- Python 3.11+，模型与文件执行核心使用标准库，TUI 使用 Textual，跨语言声明索引使用 Tree-sitter
- Linux / macOS / Windows 10+ / Windows WSL
- Chat Completions、Messages、Responses 三种协议，函数工具与 SSE 流式输出
- 可选每次模型输出 token 上限与兼容服务的思考模式
- 分层 `config.toml`、命名提供商/模型别名/profile、生成/代理/鉴权参数、配置 Schema 与离线诊断
- 工具禁用、文件排除、命令环境白名单、会话存储和 TUI 偏好设置
- CLI `--file` / TUI `/add` 附加有界快照；`--ref-file` / `/ref` 维护请求前刷新的文件引用
- TUI Tab 补全本地命令和文件参数，`/files` 模糊选择快照或实时引用；主输入框保持 Enter 发送
- 全屏终端 TUI、连续对话、项目代码地图、分目录 `AGENTS.md` 规则与本地 Git 查看
- 交互式需求澄清：选项、多选和自填，Enter 确认、中途引导；答案不批准操作，见[提问流程](docs/questions.md)
- 九种语言按声明名读取源码，保留真实行号与版本绑定；见[命名读取](docs/named-reads.md)
- 同一地图按名称查询语法调用位置及所属声明；见[调用线索与边界](docs/call-sites.md)
- 用户固定验收命令：失败反馈修复，通过后才确认验收完成
- 可选本地会话保存与恢复
- 只读分析、上下文预算、敏感文件排除、可审阅的文件编辑撤销与重做
- SHA-256 版本绑定的按行编辑，减少长提案重复携带旧正文；输出截断明确报告且不执行部分工具意图，见[编辑协议](docs/revision-edits.md)
- 独立文件编辑先整批预检、再逐文件批准；失败停止剩余编辑，见[批次边界](docs/edit-batches.md)
- 长命令日志可显式配置总输出上限，脱敏后保留有界首尾，见[输出与验收边界](docs/command-output.md)
- 可选私有命令日志保留，模型和用户按会话句柄分页回查省略内容，见[日志回查](docs/command-logs.md)

**先了解边界：Sprig 不是沙箱。** 文件工具有工作目录限制；你批准的命令仍以当前用户权限运行，能访问其他目录、网络和环境变量。请先在不含秘密的临时项目或独立容器中试用，不要在生产环境直接运行。

## 安装

使用 [uv](https://docs.astral.sh/uv/) 管理 Python、虚拟环境和锁文件。在项目根目录执行，Windows PowerShell 与 POSIX 终端相同：

```sh
uv sync --locked --managed-python
uv run sprig --help
uv run python -m unittest discover -s tests -v
```

`.python-version` 固定开发主版本为 3.13，`.venv` 由 uv 管理，`uv.lock` 提交到 Git；包仍支持 Python 3.11+。`--managed-python` 使用 uv 管理的 CPython，避免选择 Microsoft Store 应用别名。无需手动激活环境或使用 pip 安装。Textual、Tree-sitter、所需语法包与 RE2 正则引擎由 uv 锁定安装，工具只加载已安装的本地包；模型传输、会话和命令核心使用标准库。构建由 uv 管理隔离的 `setuptools` / `wheel` 环境，离线使用需预先准备依赖和构建缓存。

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

### 首次配置

```sh
uv run --locked sprig --init-config
uv run --locked sprig --doctor
uv run --locked sprig
```

逐次确认后在 `~/.sprig/config.toml` 创建私有配置，预设 `agnes-3.0-flash`，引用现有 `AGNES_API_KEY`，不写入真实密钥。已存在的配置不会覆盖。最后一条命令打开 TUI；Enter 发送，执行中可继续输入引导，`/help` 查看本地操作，`/keys` 查看快捷键。配置文件、附加规则/语言、profile、键位和设置来源见[配置与首次启动](docs/configuration.md)，完整示例见 [config.example.toml](config.example.toml)，Codex/OpenCode/Aider/Pi 的设置对照及未实现后端见[配置调研](docs/configuration-comparison.md)。

### 独立任务工作目录

```sh
uv run --locked sprig --workdir /path/to/repo --create-worktree ../repo-task
uv run --locked sprig --workdir /path/to/repo-task --tui
```

从固定提交创建 detached Git worktree，保留原目录的未提交修改。TUI 和聊天可用 `/worktrees`、`/worktree ../新任务目录`；每次 Git 命令仍逐次批准，当前会话不切换，不向模型发请求。hooks、内容过滤器和 Git 网络传输在管理过程中禁用；目录共享 Git 对象和引用，不提供 OS 沙箱。使用、恢复和隐私边界见 [worktree 工作流](docs/worktrees.md)。

### Agnes 3.0 Flash 预设

已按 Agnes 的[模型说明](https://wiki.agnes-ai.com/en/docs/agnes-30-flash)接入 `agnes-3.0-flash`，Base URL 为 `https://apihub.agnes-ai.com/v1`。

在 PowerShell 项目根目录执行：

```powershell
.\scripts\run-agnes.ps1
.\scripts\run-agnes.ps1 --tui --workdir . --save-session coding
.\scripts\run-agnes.ps1 '阅读项目并说明结构' --stream --workdir .
```

不带参数时进入全屏 TUI，默认流式输出。启动器使用 `uv run --locked --managed-python` 运行项目环境；未设置密钥环境变量时，会提示隐藏输入。模型和地址已有预设，密钥只进入当前进程环境，退出后恢复原环境，不写入 `.env`、源码、配置文件或会话。脚本遵循本机 PowerShell 执行策略。

`--max-tokens 2048` 可请求限制每次模型输出；`--thinking` 可显式请求同一 Flash 模型的思考模式，默认不启用。参数、恢复规则与验证范围见[模型输出与思考配置](docs/model-generation.md)。

## 开始使用

选择支持所配置协议和函数工具的模型。供应商 `wire_api` 支持 `chat_completions`（默认）、`messages` 和 `responses`，参见[协议配置](docs/provider-protocols.md)。模型名须通过 `config.toml`、`--model` 或 `SPRIG_MODEL` 指定；没有自动选择的备用模型。

以下密钥输入示例适用于 Bash，输入不会回显，也不会把密钥文字写入命令历史：

```bash
read -r -s -p 'API key: ' SPRIG_API_KEY
export SPRIG_API_KEY
printf '\n'
export SPRIG_MODEL='替换为你的模型名'

uv run sprig '阅读项目，说明结构并指出一个可以小幅改善的地方' --workdir ./my-project
```

显式 `SPRIG_API_KEY` 优先，然后使用选定提供商的 `api_key_env` 或私有文件中的 `api_key`。未配置提供商密钥时，Agnes HTTPS 原点的 `AGNES_API_KEY` 优先于通用的 `OPENAI_API_KEY`；该别名不用于其他地址。不接受命令行密钥参数，不读取 `.env`。完整优先级与地址绑定规则见[配置说明](docs/configuration.md)。不要把密钥放进任务文本。

使用其他兼容服务时：

```sh
export SPRIG_BASE_URL='https://your-provider.example/v1'
uv run sprig '为现有函数补一个最小测试，并运行测试' --workdir ./my-project
```

基础地址须指向 API 根路径，程序按 `wire_api` 追加 `/chat/completions`、`/messages` 或 `/responses`。只允许 HTTPS，回环地址上的 HTTP 可用于本地服务或测试；HTTP 重定向被拒绝。请只配置你信任的服务，任务、读取的内容和工具结果会作为上下文发送到该服务。

需要 `reasoning_content` 连续回传或拒绝客户端插入工具调用的服务，可在提供商表显式设置 `preserve_reasoning=true`、`check_history_format="observations"`。推理正文不显示，但会占用请求预算，并在启用私有会话保存时保留；默认关闭，也不自动开启模型思考。示例、参数格式和保存边界见[推理工具历史兼容](docs/reasoning-protocol.md)。

模型请求失败时，按 HTTP 状态提示鉴权、权限、端点、请求大小、限流或服务故障；网络错误区分 socket 超时和 TLS 证书校验失败。错误响应正文、服务端原因和其他诊断头不会显示。对于 429 / 503，有效的 [`Retry-After`](https://www.rfc-editor.org/rfc/rfc9110.html#name-retry-after) 秒数或 UTC HTTP 日期只转成建议等待秒数；提示最多接受 24 小时，过长、非法或重复的字段被忽略。程序不等待或自动重试，也不保证等待后请求一定成功；CLI 使用 `/continue`，TUI 点击“继续当前对话”，明确发起下一次请求。

### 确认流程

- 读取、列出和搜索文件无需额外确认
- 写文件前显示完整 diff；确认默认是拒绝
- 执行每条命令前显示完整参数列表和工作目录；确认默认是拒绝
- 没有自动批准开关。请逐项检查路径、代码和命令，不要机械地全部同意

例如，可让模型提出修改后，在 diff 提示处拒绝写入，先检查建议是否合理。拒绝会成为工具结果，模型可以据此调整方案。

## 命令行参数

```text
sprig [TASK] [--workdir PATH] [--file RELATIVE_PATH ...] [--model MODEL] [--base-url URL]
      [--config PATH | --no-config] [--provider NAME]
      [--init-config | --show-config | --doctor]
      [--max-steps INT] [--timeout SECONDS] [--max-output INT]
      [--stream] [--request-usage] [--metrics] [--chat | --tui] [--check JSON_ARGV ...]
      [--save-session NAME | --resume NAME] [--session-dir PATH]
      [--change-history-dir PATH]
      [--no-project-instructions]
```

`python -m sprig_agent` 使用相同接口。

| 参数 | 默认值 / 说明 |
| --- | --- |
| `TASK` | 新会话必填；使用 `--chat`、`--tui` 或恢复会话时可省略 |
| `--workdir` | `.`，文件工具的工作根目录 |
| `--file RELATIVE_PATH` | 可重复；发送任务前附加完整文件快照，仍检查隐私、大小、路径及上下文预算；TUI 也可 `/add 路径` |
| `--ref-file PATH` | 可重复，别名 `--reference-file`；小文件在每次请求前刷新，大文件只发送路径和大小；TUI/chat 用 `/ref`、`/unref`、`/refs clear` 管理 |
| `--config PATH` / `--no-config` | 显式可信 TOML 路径 / 禁用文件；默认用户 `~/.sprig/config.toml`，不搜索仓库 |
| `--provider` | 选择命名提供商；否则 `SPRIG_PROVIDER` 或配置默认提供商 |
| `--init-config` / `--show-config` / `--doctor` | 确认创建私有模板 / 密钥值不显示的生效配置 / 离线诊断 |
| `--model` | `SPRIG_MODEL`，否则选定提供商的模型；必须配置 |
| `--base-url` | `SPRIG_BASE_URL`、选定提供商，最后为 `https://api.openai.com/v1`；覆盖地址不自动转发配置密钥 |
| `--cli` | 单次执行，覆盖配置里的 TUI/chat 界面偏好 |
| `--max-steps` | `20`，限制本次代理循环的模型调用次数 |
| `--timeout` | `30` 秒；命令的墙钟时限，同时作为未设 `--request-timeout` 时的网络等待上限 |
| `--request-timeout` | 网络空闲/连接阶段上限，最多 120 秒；不延长命令超时 |
| `--request-deadline` | 默认 300 秒，最多 3600 秒；三协议共用的请求总时限，持续心跳也不会延长；取消和 DNS 边界见[请求生命周期](docs/request-lifecycle.md) |
| `--max-output` | `16000` 字节，工具输出上限；读取 / 搜索结果可截断，命令超出上限会被终止 |
| `--stream` | 可选；逐步显示模型文本，工具调用仍在完整响应校验通过后执行 |
| `--request-usage` | 显式请求流末尾的 token 用量，须搭配 `--stream` 或 `--tui`；服务不支持时不自动重试 |
| `--metrics` | 退出 CLI/TUI 时向标准错误输出本地数值统计；不改变任务状态码，不写报告文件 |
| `--read-only` | 只允许文件检查；工具入口强制拒绝所有写入和命令，不可搭配 `--check` |
| `--inherit-env` | 显式允许获批命令继承更多环境变量；已知模型密钥仍移除 |
| `--max-context-bytes` | 默认 131072；限制实际序列化请求大小，范围 4096–2097152；不等同 token 或费用预算 |
| `--compact-context` | 显式省略旧工具正文及已完成编辑文本；用户要求、拒绝、最新失败和最近窗口保留，旧固定检查保留状态 |
| `--no-project-instructions` | 关闭自动加载工作目录内的 `AGENTS.md`；设置绑定到保存的会话 |
| `--chat` | 可选；在同一上下文中连续输入任务，须在交互式终端运行 |
| `--tui` | 全屏终端界面，自动启用流式输出；标准输入和输出都须连接终端 |
| `--check JSON_ARGV` | 可重复指定固定验收命令，最多 16 条；每次执行都需要确认 |
| `--check-before-task` | 可选的任务前基线；要求非空 `checks`，每条命令仍需新批准 |
| `--check-after-edit` | 可选的[编辑后固定检查](docs/edit-checks.md)；每批文件编辑后反馈结果，不增加模型请求预算，不替代最终验收 |
| `--save-session NAME` | 显式启用会话保存 |
| `--resume NAME` | 恢复指定的已保存会话 |
| `--session-dir PATH` | 默认 `~/.local/state/sprig-agent/sessions`，须位于工作目录之外 |
| `--list-sessions` | 列出本地会话元数据，无需模型或密钥，不上传对话 |
| `--delete-session NAME` | 逐次确认后删除本地会话文件；非交互输入默认拒绝 |
| `--change-history-dir PATH` | 显式持久化精确修改备份，目录须位于工作空间/仓库之外，使用所有者私有权限；不加密 |

### 项目开发规则

默认在首个模型请求前加载工作目录根部的 `AGENTS.md`。读取文件后，后续请求会加入该文件所在目录及祖先目录内的规则；规则按作用目录标注，较深目录的规则适用于其子树。每次请求重新检查已发现规则，恢复会话时也重新读取当前文件。不会向工作目录外、用户主目录或远程地址寻找规则。

模型提出编辑或命令时，应用检查目标文件父目录或命令 `cwd` 的规则是否已出现在产生该响应的请求中；新规则或变更会阻止该动作，先把新规则交给模型重新规划。确认动作后还会重读规则，变化时拒绝写入或启动命令，要求重新确认。规则文件不能替用户批准、放开只读或修改固定验收条件；获批命令仍不受沙箱限制，应用不能从任意 argv 推断所有涉及文件的目录规则。

读取遵循现有隐私、取消和文件句柄限制，排除项不加载。每份规则最多 16 KiB、全部规则 JSON 最多 64 KiB、已发现目录最多 128 个；无效 UTF-8、二进制、疑似凭据、不安全文件或超限会明确停止，不截断规则。规则同样占用请求字节预算，`/context` 预览包含它们，`/compact` 保留它们。可修正文件、缩小工作目录，或使用 `--no-project-instructions` 显式关闭。规则文本会发送到配置的模型服务，并进入已启用保存的会话。[详细行为与验证](docs/project-instructions.md)。

### 上下文预算

请求在实际发送前检查字节预算，超限时停止，不会请求模型或自动删除历史。CLI `/context` 和 TUI“查看上下文预算”显示当前请求大小、压缩后大小以及将缩减的消息、字段和字节数。CLI `/compact` 或 TUI“压缩旧工具记录”需要确认；也可启动时明确指定 `--compact-context`。

压缩只影响请求，保存的完整工具输出和编辑参数保留。用户要求、助手解释、拒绝、失败状态、验收状态元数据、调用/结果顺序、调用 ID 和最近六条消息保留；旧成功读取或命令的正文替换为省略标记。固定检查已有较新批准且完整完成的观察时，可省略旧检查的 argv 和正文，保留旧退出码与指向新观察的引用；最新失败及普通失败的详细输出保留。读取正文省略后也省略其旧版本与行位置等附属字段，覆盖标记保留，见[固定检查上下文](docs/check-history-context.md)。`edit_file` / `edit_lines` 仅在旧意图与唯一旧结果对应、结果明确批准且实际改变同一路径时，省略 `old/new` 或 `edits` 正文；历史参数变为含路径和明确说明的不可执行标记，不充当新操作。未完成、拒绝、失败、不确定、无改动或结果处于最近六条消息内的编辑不会缩减。模型需要时应重新检查实际文件；新增写入和命令仍取得新批准。

无法压缩的长要求、最近消息或大量失败记录仍会被预算拦截，不自动删除历史或提升限额；可以由用户增加预算或开始新会话。压缩期间收到取消会在调用 provider 前停止。[大块编辑回放](docs/edit-context-benchmark.md)验证了实际 HTTP 字节、文件效果、固定验收和完整保存；它是脚本化夹具，不代表真实模型的长任务表现。

### 请求测量与用量

CLI `/usage`、TUI“查看请求统计”在本地显示当前 provider 实例的请求统计，不调用模型、不修改对话或验收结果。`--metrics` 在退出时输出相同统计；例如 `uv run --locked sprig --stream --request-usage --metrics --model your-model '说明项目结构'`。模型仍可能提出写入或命令，每次都需要确认；统计选项不改变批准规则。

记录每次网络调用尝试的请求 JSON 字节数、客户端读取到的响应体字节数、总耗时及流式首段非空文本/拒绝片段的接收延迟。计时从发起网络调用前开始，包含响应解析和输出回调，排除请求序列化、工具执行和用户批准等待；首段计时在文本遮盖和界面缓冲前，不能代表界面显示延迟或 token 逐个生成时间。非流式和纯工具调用没有可观测首段文本，记为 `null`。响应字节不含 HTTP 头，也不代表线路流量；HTTP 错误正文不读取或保留。

只保留服务报告的 `prompt_tokens`、`completion_tokens`、`total_tokens`。缺失字段、非法数值或矛盾用量为 `null`，不通过字符数推算 token、费用或计费结果。流式用量按[官方接口约定](https://developers.openai.com/api/reference/resources/chat)通过 `stream_options.include_usage` 请求；断流或取消可能缺失最终用量，兼容服务可能不支持此选项，所以默认不开启。重复且相同的用量片段只计一次；矛盾或非法片段使该请求用量未知。非流式响应中的用量自动记录。

`reported_sum` 仅累计有明确数值的请求，同时给出 `known_requests`、`unknown_requests`；只有全部尝试都有该字段时，`complete_sum` 才是总数，否则为 `null`。请求结果 `success` 仅表示完整响应通过协议校验，不能证明代码正确、通过验收或新要求已采用。网络调用尝试也不证明服务收到请求；失败和取消不自动重试，其未知费用不能当作零。Provider 入口的预算拒绝属于 `not_attempted`，不会计为网络尝试；在 Agent 预算检查阶段已停止的任务没有 Provider 记录。

只在内存保存最近 128 条数值记录，历史累计计数保留；不保存提示词、模型文本、工具参数、URL、请求 ID、错误正文或密钥。统计不进入模型上下文或会话文件，恢复历史不能恢复以往用量。它是请求级测量，完整任务耗时、工具与批准开销、内存及竞品对照仍需另外评测。

可通过 [编码任务诊断](docs/coding-benchmark.md)在一次性项目中运行定位、跨文件修复、新功能等任务，保留逐次批准并记录任务和工具开销。[客户端对照准备](docs/client-comparison.md)提供固定版本的独立 uv / npm 环境与原生入口检查。[固定 Click 仓库任务](docs/repository-task-benchmark.md)让 Sprig 和 mini 从相同源码、任务和外部验收开始，支持各自行动循环中的失败后修复。离线自检和入口检查不是模型成绩，也不能代替真实竞品对照。

### 会话管理

```sh
uv run --locked sprig --list-sessions
uv run --locked sprig --delete-session coding
```

TUI 的操作页也提供会话列表和删除按钮。列表显示名称、模型、目录和消息数量，不显示对话正文。删除默认拒绝，不改变项目文件。

单次列表最多扫描 4,096 个目录项、返回 256 个会话，正文读取预算为 8 MiB。达到任一上限会明确提示结果可能不完整；CLI 保留标准输出的 JSON 数组，同时在标准错误提示并返回 2，TUI 显示上限原因和扫描统计。TUI 可用 Ctrl+C 取消，会话列表不请求模型，取消后释放临时取得的会话锁。列表按实际扫描得到的子集排序，不保证取得全部名称的前 256 项。

新会话名称在使用前即被保留，保存或恢复后会持有独占锁直到切换或退出。同一个会话不能被两个 Sprig 进程同时加载、覆盖或删除。锁由操作系统句柄管理，进程崩溃后自动释放；残留的锁文件无需手动删除，运行期间删除锁文件会破坏并发保护。

保存、检查或加载失败时，释放本次新取得的锁；当前使用中的会话仍保留保护。TUI 换名保存成功后才切换会话，失败保留当前对话和原名称；删除当前会话失败也不释放其已有锁。原子替换前失败不会覆盖旧文件；替换后的同步失败可能已生成新文件，应用会报告错误，可以检查并明确恢复该名称，不自动覆盖已有会话。

### 文件编辑撤销与重做

TUI 提供“查看修改记录”“撤销最近文件编辑”“重做文件编辑”；CLI 连续对话提供 `/changes`、`/undo`、`/redo`。修改记录只包含 `edit_file` / `edit_lines` 的精确前后版本，最多保留 16 次、合计 4 MiB，达到上限会淘汰最旧记录。默认只在当前进程内保留；需要退出后继续撤销时显式启用：

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

CLI 与 TUI 都实时显示命令输出，保留 UTF-8 分段，遮盖跨片段的已知密钥并转义终端控制字符。输出或读取在密钥中间截断时，也遮盖末尾至少八个字符的已知密钥前缀，保存会话遵循相同规则；较短片段、编码后的秘密和未知凭证不在此保证范围内。显示和返回模型的结果受相同输出上限约束，超限会停止命令。

| 工具 | 行为 |
| --- | --- |
| `list_files(path=".")` | 列出目录内容 |
| `read_file(path, offset=1, limit=..., line_numbers=..., cursor=...)` | UTF-8 行范围或版本绑定的字节页；范围默认显示行号，cursor 模式用于超长单行 |
| `search(query, path=".", glob=..., ignore_case=False, regex=False, context=0, limit=100)` | 搜索指定文件或目录，默认字面字符串；可显式启用 RE2 正则、筛选文件、忽略大小写并返回上下文 |
| `project_map(path=".", query=...)` | 返回有界文件地图和九种语言的声明名称与行号；可按路径或声明名筛选，优先保留精确匹配；不支持的语言只列路径 |
| `edit_file(path, old, new)` 或 `edit_file(path, edits=[...])` | 单片段或同文件多片段替换，展示合并 diff，经确认后一次写入 |
| `edit_lines(path, read_id, edits=[...])` | 使用读取返回的短版本标识进行整行替换；内部核对完整摘要，仍需完整 diff 和新批准 |
| `run(argv, cwd=".", timeout=...)` | 运行参数数组所指定的命令；可选超时不能超过全局上限 |
| `file_action(action, path, read_id=..., destination=...)` | 创建单个目录、读取标识关联的删除或复制后删除的移动；逐阶段新批准，不覆盖目标，支持文件撤销 |
| `update_plan(plan, explanation="")` | 更新有界的模型进度清单；TUI 计划页和 `/plan` 查看，随显式保存的会话恢复；不批准操作、不代表验收通过 |

大文本使用[文件与传输说明](docs/text-transfer.md)：超长单行可用 `read_file(cursor="start")`，随后传回 `next_cursor` 续读；页面绑定路径与内容版本，按 UTF-8 码点边界截取。TUI 超限输入保留草稿，待处理引导有数量和字节限额，显示裁剪与模型历史分开。

需要随编辑刷新的文件，可设置 `[context].files = ["src/main.py"]` 或输入 `/ref src/main.py`。完整小文件只进入当前请求；大文件以路径和大小提示按需读取。引用元数据随启用保存的会话恢复；`/refs` 查看，`/unref PATH` 或 `/refs clear` 移除。Windows 终端可传工作区内的本机绝对路径。完整请求预检失败也保留 TUI 草稿；详见[实时文件引用](docs/live-file-references.md)。

目录、删除和移动有[专用文件操作](docs/file-actions.md)。删除/移动最多 512 KiB 的普通 UTF-8 文件，先读取获得当前标识，再分别批准具体变化。移动分复制和删除两步，中断可能保留两份；明确成功后实时引用跟随移动或移除，阶段检查与独立最终验收继续生效。它不是原子 rename 或递归目录工具。

长会话压缩时，`[context].keep_read_bytes` 在原请求硬限额内保留少量完整的旧源码窗口，默认 8192 个编码字节，0 关闭；不足时仍省略，不自动增加请求。保留正文标为历史快照并移除旧读取标识，详见[源码窗口保留](docs/read-retention.md)。

仍超限时，可[折叠已完成的旧只读工具协议](docs/inspection-folding.md)，保留参数、状态与原助手文字，不改写保存历史或最新保护消息。账本不产生当前读取证明、操作批准或自动重试。

`edit_file` 的 `old` 为空时仅用于创建新文件，且父目录必须已存在。已有文件不能用空字符串匹配来覆盖。写入都需先检查 diff 并确认。

`edits` 模式每项为 `{"old": "原文", "new": "替换后文本"}`，最多 32 项，不能同时提供顶层 `old` / `new`。全部片段针对同一个文件的原始文本匹配；每个 `old` 非空且恰好出现一次，重叠或重复的范围拒绝，包括 `aaa` 中的 `aa` 这类重叠出现。先校验全部片段、最终内容、隐私和大小，再展示合并 diff；任何片段失败都不写入。单次合计片段最多 512 KiB，结果与 diff 仍受原有限额限制。确认后再次检查文件和规则变化，并一次替换、记录一项撤销历史。每个文件仍单独取得新批准，跨文件修复依次执行。

`read_file` 不带范围时保留按输出预算读取文件开头的行为。指定 `offset` / `limit` 可精确读取后部代码；范围读取先检查完整的受限文件（最多 8 MiB），避免用干净页面绕过其他位置的凭证、二进制或编码检查。范围默认显示 `N: ` 行号前缀，前缀不是文件正文；`line_numbers=false` 返回原文，true 可显式启用编号视图，附件使用原文。行号从 1 开始，`limit` 最多 10,000，范围模式省略时为 2,000；`next_offset` 指向下一页。编号计入输出预算，返回尽可能完整的行；单行超过预算时标记 `line_truncated`，不提供短读取标识或无法推进的后续行号。源换行和 UTF-8 字符不转换，超限、取消或非法范围会明确失败。整行编辑拒绝拼接前后代码的缺失换行提案，详见[读取绑定编辑](docs/read-bound-edits.md)。

`search` 的 `path` 可指向一个文件。`glob="*.py"` 在任意层级筛选文件名，`glob="src/**/*.py"` 按工作空间相对路径筛选；`*` 不跨 `/`，完整组件 `**` 匹配零层或多层。glob 在所有平台区分大小写，支持 `*`、`?`、字符集合，最多 256 UTF-8 字节、32 个组件，不支持花括号扩展或排除式模式；筛选在读取内容前进行，仍受隐私规则约束。`ignore_case=True` 在字面模式使用完整 Unicode case folding，正则模式使用 RE2 的简单 Unicode 折叠。

`regex=True` 启用 RE2 语法，例如 `query=r"^(?:async )?def (?:load|save)_item\("` 可一次寻找两类函数定义。表达式最多 1,000 个字符，支持分支、分组、锚点、字符类、量词与 Unicode 属性；不支持回溯引用和 lookaround，不支持跨物理 CR/LF 行匹配。`\w`、`\d`、`\b` 按 ASCII 解释，Unicode 字符可用 `\p{L}`、`\p{Han}` 等属性。编译失败返回固定错误，不回显表达式。单次搜索的引擎程序/缓存预算为 1 MiB；编译和逐行匹配累计达到 1 秒时，在原生调用返回后的检查点停止并标记 `timed_out` / `truncated`，只保留已经完整的结果。这是协作式工作预算，不是强制中断原生调用或进程总内存上限。详见[正则检索与测量](docs/regex-search.md)。

`context` 为 0–10，返回每个命中行的 `before` / `after` 字符串数组，分别对应紧邻前后物理行；默认不带这些字段。`limit` 默认 100、最多 1,000，计数的是命中行。同文件多处命中的上下文可能重复。完整文件先通过凭证、编码和二进制检查，再返回任何命中；单条结果连同上下文若超出剩余预算，会整体省略并标记截断，不切出不完整字符串。返回的 `files_read`、`bytes_read`、`entries_scanned` 为实际扫描统计；不可读、过大、二进制或含凭证的候选文件计入 `skipped_files` 并使结果不完整。遍历和逐行匹配都响应取消。

文件工具拒绝越出工作根目录的路径、符号链接，以及路径中包含 `.git`、`.env`、`.env.*` 的组件。它们只处理 UTF-8 文本，不能编辑二进制文件。

工具路径在所有系统上都用 `/` 分隔。Windows 还拒绝 junction 等重解析点、设备名、备用数据流、尾随点和空格，并按大小写不敏感方式保护 `.git` / `.env`；通过文件句柄检查 DOS 短名对应的实际路径。文件编辑保留 POSIX 权限位或原文件的 Windows DACL。

搜索会跳过不可读文件、二进制文件、`node_modules`、`.venv`、`.venv-*` 和 `__pycache__`，最多遍历 5,000 个条目、32 层目录、读取 8 MiB；单文件编辑和搜索读取限制为 512 KiB。结果带有截断标记，但“没有匹配”不代表被跳过的文件也已检查。

代码地图采用相同遍历与读取预算。Python 使用 AST，其他支持的语言使用本地 Tree-sitter 语法包，提取类、函数、方法和类型等声明；Go 方法带接收者名称，Rust 方法带 `impl` 类型名称。`.h` 按 C++ 语法处理，`.C` 表示 C++，`.c` 表示 C；JavaScript 支持 `.js/.mjs/.cjs/.jsx`，TypeScript 支持 `.ts/.mts/.cts/.tsx`。行号从 1 开始，与按行读取的 CR/LF 行号一致。

大文件的声明列表可能超过地图输出预算。此时可用 `project_map(query="Service.get")` 或指定文件名，按大小写不敏感的字面字符串筛选路径和已提取的声明名；最多 256 个 UTF-8 字节，不解释正则表达式。精确名称匹配优先于前缀和子串，同分时优先路径和行号；匹配路径会保留该文件的声明。`symbols_filtered` 表示筛选结果，`symbols_truncated` 表示匹配声明仍有省略，`matched_files` 只统计已遍历范围内匹配的文件。此模式继续扫描受限范围，以免输出先满就漏掉后部的精确匹配；保留的候选元数据和凭据遮盖后的结果大小都计入预算。解析失败或遍历、读取、输出超限会标记 `truncated`，空结果不能证明整个仓库没有该声明。

解析结果按语言和内容摘要缓存到内存（最多 128 项、1 MiB 摘要与编码后的符号元数据）；每次仍读取最新文件并检查隐私规则，文件修改、删除或新增排除规则不会沿用旧符号。缓存不保存源文件正文或 AST，关闭工具时清除缓存和解析器。非 Python 单文件解析限时 1 秒，遍历最多 100,000 个语法节点、返回最多 4,096 个声明，限定后的名称最多 1,024 个 UTF-8 字节；取消在解析前后和遍历期间检查，原生解析中的取消需等它完成或超时。语法错误、超时、超限、隐私拒绝或过大文件会标记 `symbols_unavailable`，超时和超限不会缓存。没有语义解析、类型推断、引用关系或调用图；函数体内的局部声明不加入地图。请继续读取相关源码确认行为。

整个地图扫描会保护扫描开始前的缓存，避免文件数超过容量时重复查询仍重解析所有文件。放不进缓存的结果照常解析和返回，不减少检索范围。完整遍历后淘汰未再使用的旧摘要；取消、失败或遍历/输出超限时保留尚未访问的条目。全部内容改变时可能需要下一轮扫描才能重新填满缓存；容量上限保留。[容量边界对照](docs/scan-cache-benchmark.md)记录实际读取、解析次数和相同结果，不能据此推断模型总耗时或编码成功率。

精确编辑匹配失败时会返回失败片段编号、原因和有界的读取页建议，方便重新定位实际旧文本；不会自动套用近似替换。行为和恢复验证见 [编辑恢复](docs/edit-recovery.md)。

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
- 右侧单独显示“任务结果”，与当前操作状态分开。查看项目地图、修改记录、会话列表、上下文预算或保存会话不会覆盖失败、取消和未验收状态；Ctrl+Q 返回保留的任务状态。未设置 `--check` 时明确显示“未配置验收命令”。操作栏提供 Git diff、Git 状态和项目地图；“事件”标签显示工具结果。这些本地查看结果不会自动发送给模型。
- 每次修改弹出完整 diff，每次命令弹出 argv、工作目录和资源限制。确认窗默认聚焦“拒绝”，此时 Enter 默认拒绝，Esc 拒绝；批准需要选择“批准这次操作”。确认窗中的调整输入框同样使用 Enter 发送，发送会让这次待确认操作作废，模型重新规划后须取得新的批准；Ctrl+L 可聚焦调整输入框。
- 会话名称框配合“保存”“恢复”按钮使用。保存后自动记录后续轮次；恢复替换已有内存对话时先确认，不自动发起模型请求或重放工具。恢复后输入新任务，或点击“继续当前对话”。也支持原有 `--save-session` / `--resume` 参数。
- Ctrl+C 或“取消”停止当前任务，清理正在运行的命令及其正常后代进程；待确认操作不会执行。网络请求采用协作取消，等待中的 socket 可能仍需等到数据返回或超时。已经接收的补充要求保留在对话中，点击“继续当前对话”或发送新消息时采用；取消不会自动启动下一轮。
- Ctrl+Q 退出；任务运行时先取消并等待清理，不立即丢下工作线程。Ctrl+L 聚焦任务输入。

新任务或执行中引导使当前任务结果重新进入待处理状态；恢复已保存的会话不会恢复“验收通过”标记，需重新运行。撤销、重做及结果不确定的文件恢复立即使先前验收失效，包括后续会话保存失败的情况。获批本地命令同样保守地使验收失效，因为命令可能调用外部程序并改变文件；该失效提示进入后续会话，命令输出仍不自动上传。程序暂不监控其他进程修改文件，因此验收结果只描述当时实际运行的固定命令。

TUI 的模型片段和命令日志约每 50 毫秒合并刷新；待显示缓冲最多 64,000 字符，满时暂停生产并允许取消。用户消息、确认弹窗和任务结束前刷新已有输出，保持对话顺序。可见对话保留最近 64,000 字符，事件区最多 1,000 行；这些显示上限不会压缩模型上下文或改变完整会话保存。暂不提供并行任务队列、逐行命令输出或工具权限自动批准。

### 简单连续对话

```sh
uv run sprig --chat --stream --model your-model --workdir ./my-project
```

每次输入的任务沿用当前对话上下文，`--max-steps` 仍按每次任务计算。`/help` 查看用法，`/status` 查看本地上下文和最近状态码，`/exit` 退出。任务中按 Ctrl+C 取消当前任务并保留对话；输入提示处按 Ctrl+C 清除本次输入，继续等待。网络、工具或保存错误也保留内存对话，不自动开始下一轮；输入 `/continue` 明确继续已有历史，或输入新要求。待定操作记为结果不确定，不会重放；历史校验失败时阻止后续模型请求，需退出后开始新会话。

`/diff` 和 `/git` 在确认后分别执行 `git diff -- .`、`git status --short -- .`；这些本地查看结果不会自动发送给模型。已有任务之后获批本地命令，会保守地使先前结论和验收失效，失效提示进入对话；中断的本地命令也标记为可能改变文件。撤销或重做造成实际或不确定的文件变更，同样使旧验收失效。后续保存错误不会把这些变更标为已验收。

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

保存、恢复和删除会话都遵守操作系统独占锁；同一会话已被另一进程占用时拒绝操作。原子替换保证完整文件，独占锁防止并发覆盖，退出或崩溃后释放。

## 安全地验证

先运行本地测试，无需 API 密钥：

```sh
uv run python -m unittest discover -s tests -v
```

2026-10-02 已在原生 Windows 上使用 Agnes 3.0 Flash 完成普通和流式 API 的端到端测试：读取临时项目、修复函数、执行固定验收命令并保存恢复会话。[验证记录](docs/validation.md)列出实际测试范围，不代表任意兼容服务都能正常工作。

性能目标、基线测量与尚未验证的差距见[性能记录](docs/performance.md)。`uv run --locked python scripts/benchmark-tools.py --compare-ref e9e8a8d` 可在相同临时项目中对照旧提交的检索能力和开销，不调用模型。

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
- “OpenAI-compatible” 不保证模型、工具格式和响应行为兼容；所选服务与模型必须支持配置的协议和客户端函数工具
- Windows 需要支持受保护 DACL 的文件系统及 Job Object API（Windows 10+）；不支持 FAT/exFAT 会话目录，或权限/作业策略禁止必要 API 的环境

本项目用标准库 `urllib` 支持三种 HTTP 协议，默认 Chat Completions、非流式，`--stream` 使用各协议的 SSE 事件。Messages 和 Responses 保留必要的原生推理状态，仍共用 Sprig 的批准、取消、请求预算和私有会话边界。详见[协议支持范围](docs/provider-protocols.md)。

## 源码导览

- `src/sprig_agent/cli.py` / `__main__.py`：参数解析与命令行入口
- `src/sprig_agent/tui.py`：Textual 全屏终端界面、确认窗、任务取消与本地会话操作
- `src/sprig_agent/config.py`：环境变量与运行配置校验
- `src/sprig_agent/settings.py` / `diagnostics.py`：私有 TOML、提供商选择、密钥引用、初始化及离线诊断
- `src/sprig_agent/provider.py`：三种协议共享的有界 HTTP 传输
- `src/sprig_agent/native_protocols.py`：Messages / Responses 请求、完整响应和 SSE 适配
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
- [OpenCode 源码阅读与基础能力补齐](docs/research/opencode-source-reading-20261020.md)：固定源码版本、实现与测试体、Sprig 对照及分批验收顺序
- [执行阶段与运行结果](docs/runtime-status.md)：CLI/TUI 共用的步骤、工具批准、检查及取消状态；区分模型结束和独立验收
- [TUI 补全与文件选择](docs/tui-completion.md)：Tab 补全本地命令和文件参数，模糊选择快照或实时引用；主输入保持 Enter 发送
- [消息中的本轮文件](docs/tui-mentions.md)：`@关键词` + Tab 明确选取，发送时读取，编辑标记撤销选择；正文不作为附件跨轮次保存
- [TUI 长文本粘贴](docs/tui-paste.md)：自动折叠大段粘贴，F4 展开检查与编辑，Enter 发送完整正文，保留缩进并执行完整预算预检
- [显式摘要与原文回查](docs/summary-checkpoints.md)：一次额外请求需确认，按来源绑定检查点并保留完整历史；支持恢复、清除与只读分页回查
- [产品能力与达标差距](docs/development-readiness.md)：完整基础能力盘点、未完成项和验收条件，区分功能交付与模型性能证据
- [差异化调研与产品提案](docs/sprig-differentiation.md)：现有先例、需求变更工作台与分阶段验收标准；提案功能尚未实现
- [其他产品方向调研](docs/sprig-alternative-directions.md)：首次跑通项目、失败复现卡、编程陪练等较小范围的产品方案

## 许可

[MIT License](LICENSE)，Copyright © 2026 Sprig contributors。
