# 配置与首次启动

Sprig 从用户目录的 `~/.sprig/config.toml` 读取设置。Windows 为 `%USERPROFILE%\.sprig\config.toml`，Linux/macOS/WSL 为各自用户的 `~/.sprig/config.toml`。Windows 与 WSL 不会自动共享配置或密钥。

在仓库根目录使用 uv：

```sh
uv run --locked sprig --init-config
uv run --locked sprig --doctor
uv run --locked sprig
```

第一步显示路径及完整模板，逐次确认后才创建，非交互输入默认拒绝。文件已存在时拒绝覆盖。模板选择用户要求的 `agnes-3.0-flash`，引用 `AGNES_API_KEY`，不请求模型、不写入真实密钥。第三步打开 TUI，Enter 发送，Shift+Enter / Ctrl+J 换行。

```toml
provider = "agnes"
interface = "tui" # tui、chat、cli；也可用启动参数覆盖

[providers.agnes]
model = "agnes-3.0-flash"
base_url = "https://apihub.agnes-ai.com/v1"
api_key_env = "AGNES_API_KEY"

[agent]
stream = true
timeout = 60
max_steps = 20
max_tokens = 2048
thinking = false
# read_only = true
# compact_context = true
# checks = [["uv", "run", "python", "-m", "unittest", "discover", "-s", "tests", "-q"]]
```

`api_key_env` 引用当前进程环境变量。也可删去这一行，改用 `api_key = "你的密钥"`，但两者不能同时定义。明文配置不加密，建议保留环境变量引用，勿把密钥写进任务或提交到 Git。

初始化创建所有者私有的文件；所有配置都要求 POSIX `0600` / Windows 所有者私有 ACL，拒绝符号链接或重解析点路径。编辑时保留权限。POSIX 手动创建后可用 `chmod 600 ~/.sprig/config.toml`；Windows 可先用初始化入口创建，再就地编辑。不会为方便读取而放宽权限。

## 选择与优先级

- 文件：`--no-config` 禁用全部文件；否则 `--config PATH` > `SPRIG_CONFIG` > 用户默认路径。显式文件缺失时报错；缺失的默认文件允许使用旧环境变量/参数方式。
- 提供商：`--provider NAME` > `SPRIG_PROVIDER` > 选定 profile 的 `provider` > 顶层 `provider`。可定义至多 32 个命名提供商，没有自动切换或失败后备用模型。
- 模型：`--model` > `SPRIG_MODEL` > 显式项目文件的 `model` > profile 的 `model` > 选定提供商的 `model`。地址：`--base-url` > `SPRIG_BASE_URL` > 选定提供商的 `base_url`。没有模型时明确报错；未选提供商时保留原 OpenAI 地址默认值。
- 密钥：显式 `SPRIG_API_KEY` > 选定提供商的 `api_key_env` 或 `api_key` > 旧环境别名。声明了 `api_key_env` 却未设置时停止，不挪用另一个提供商的全局密钥。旧别名仍为 Agnes HTTPS 原点的 `AGNES_API_KEY`，然后 `OPENAI_API_KEY`。
- 配置密钥绑定该提供商的完整基础地址。覆盖地址时，必须另设显式 `SPRIG_API_KEY`，避免把原密钥转发到别处。`AGNES_API_KEY`（含大小写变体）的特殊引用始终限于其 HTTPS 原点。
- 运行参数：显式 CLI > 显式项目文件 > profile > 模型条目 > 提供商生成设置 > 用户全局设置 > 内置默认值。布尔参数支持 `--no-stream`、`--no-thinking`、`--no-compact-context` 等覆盖。TUI 始终流式；选择 `--cli --no-stream` 使用非流式单次执行。

Sprig 不自动读取项目或父目录的配置；`--project-config PATH` 显式启用受限项目层。也不加载 `.env`、远程配置、配置脚本或动态环境替换。这样下载的仓库不能改变接收密钥和代码的服务地址。`--config` 是对该文件的显式信任，应先检查其内容。

`[agent]` 支持 `max_steps`、`timeout`、`max_output`、`stream`、`request_usage`、`read_only`、`inherit_env`、`max_context_bytes`、`compact_context`、`project_instructions`、`max_tokens`、`thinking`、`checks`。均执行现有类型、范围和组合校验；未知设置报错，不能通过配置自动批准、关闭隐私检查或绕开只读。`checks` 是 argv 数组的数组，启动参数 `--check` 替换整组；恢复会话仍核对模型、地址、工作目录、固定验收和生成参数。

## 离线检查

`[agent].check_before_task=true` 可在模型请求前执行已有固定检查；默认关闭，要求非空 `checks`，每次执行仍需批准，完成后的验收仍独立运行。CLI 可用 `--check-before-task` / `--no-check-before-task` 覆盖，详见[任务前基线检查](baseline-workflow.md)。

`[agent].check_after_edit=true` 可在模型一次工具响应中的文件编辑完成后运行同一组固定检查；默认关闭，要求非空 `checks`。CLI 用 `--check-after-edit` / `--no-check-after-edit` 覆盖。它让失败证据在模型宣布完成前进入正常循环，每条命令仍需新批准；最后一次模型请求后也不增加请求预算。阶段检查通过不能替代最终验收，详见[编辑后检查](edit-checks.md)。

```sh
uv run --locked sprig --show-config
uv run --locked sprig --doctor
uv run --locked sprig --config /trusted/path/config.toml --show-config
uv run --locked sprig --no-config --model local-model --base-url http://localhost:8000/v1 --doctor
```

`--show-config` 显示生效设置、密钥是否存在及来源类别，密钥值不显示。`--doctor` 检查配置、工作目录/`.sprigignore`、必要依赖和平台，不连接模型、不执行项目命令、不创建会话。它不能证明模型支持工具调用、服务在线或实际 OS 隔离。无效配置的错误不会回显 TOML 原文。

`setting_sources` 标明主要运行字段来自 `default`、`user`、`provider`、`model`、`profile`、`project`、`CLI`、`environment` 或强制流式的 `TUI`；合并的排除规则会保留组合来源。字段名为运行时名称，例如 `instruction_files` 对应 `[instructions].files`、`tui_editor_height` 对应 `[tui].editor_height`。凭据仍只显示是否存在和来源类别。

活动配置若显式位于工作区内，文件工具仍排除它；`.sprig` 目录始终排除。获批子进程即使开启 `inherit_env`，也移除标准模型密钥变量、所有配置声明的密钥变量及包含当前密钥的值。**这些保护不构成命令沙箱**：获批任意代码仍以当前用户权限运行，可自行读取主目录文件。

## TUI 本地命令

`/help` 列出命令；`/config` 和 `/status` 查看本地状态，`/models` 离线查看启动时加载的模型目录。`/context`、`/usage`、`/map [查询]`、`/git`、`/diff`、`/compact`、`/changes`、`/undo`、`/redo`、`/discard-history` 对应侧栏操作，保留原先的审批和只读限制。

`/save 名称` 保存或另存当前对话；另存新名称可分支后续对话，旧会话保留。`/restore 名称`、`/sessions`、`/delete 名称` 管理本地会话。`/new` 确认后开始新对话，解除原会话的自动保存和锁，不覆盖它，也不回滚工作区。提供商/模型通过配置或下次启动参数选择，当前会话不偷偷换服务。

任务进行中可输入普通文字调整要求；`/status`、`/config`、`/help`、`/plan` 可以本地查看，`/cancel` 取消，`/exit` 取消后退出。`/plan` 显示模型报告的任务进度，TUI 侧栏也有计划页。需要空闲的本地命令会提示等待，不会排队成为模型请求。未知 `/命令` 不发送给模型；要发送以 `/` 开头的普通文本，用 `//` 开头。

这些 slash 命令用于 TUI；旧 `--chat` 保留自己的 `/help` 所列命令。

## 主动附加文件上下文

### 开发规则与回复语言

用户文件和 profile 可配置：

```toml
[instructions]
files = ["CONTRIBUTING.md", "docs/coding-style.md"]
response_language = "简体中文"
```

路径以 `--workdir` 为根，使用相对 POSIX 路径，Windows 也用 `/`；至多 16 个明确文件，不展开 glob、不读取工作区外文件或远程内容。数组在高层整体替换。受限项目层可指定工作区规则和语言，不能借此改变工具审批、凭据、只读或路径策略。`--instruction-file PATH` 可重复并替换文件列表，`--response-language` 覆盖语言偏好；本次任务的明确要求优先于偏好。

文件默认适用于整个工作区；文件名为 AGENTS.md 时始终保留其目录作用域和更深规则优先级，包括第一次请求前或关闭自动发现时。`--no-project-instructions` 只关闭自动 AGENTS.md 发现，显式列出的文件仍加载。要完全关闭规则，应同时清空 `[instructions].files`。每个文件最多 16 KiB，全部规则序列化后最多 64 KiB，另受完整请求预算限制。

列出的文件必须存在并通过原有 UTF-8、隐私、无符号链接/重解析点、普通文件校验；缺失、排除、敏感或超限会在模型请求前失败，不静默跳过或截断。`--doctor` 离线检查当前规则是否可读，但不输出正文。规则在后续请求前刷新；模型提案或审批期间内容变化时，先停止操作，要求读取新规则并重新提议。规则不能批准操作。文件列表和语言绑定会话，改变后不能直接恢复发送旧历史；正文仍可能进入显式保存的未加密会话。

```sh
uv run --locked sprig --cli --file src/main.py --file tests/test_main.py "检查这两个文件的接口是否一致"
uv run --locked sprig --tui --file "src/说明 file.py"
```

TUI 内也可输入 `/add src/main.py`；路径有空格时整段作为一个相对路径，无需 shell 引号。使用工作区相对 POSIX 路径（Windows 也用 `/`）。附件不执行命令、不请求模型、不改变工作区；下一条消息或显式继续时才发送。`--chat` 启动时只附加文件、没有任务或恢复要求时，也等待下一条消息。

内容作为明确标注的不可信 JSON 数据快照加入对话，之后文件改变不会自动刷新；再次 `/add` 获取新快照，编辑前模型仍须检查当前文件。附件进入显式启用的本地会话保存，不加密。

每次最多 16 个文件，完整文本快照单文件至多 1 MiB、整批至多 2 MiB 原始字节，独立于模型工具的 `max_output` 和 10000 行展示窗口；实际序列化请求仍受原 `max_context_bytes` 约束。完整 UTF-8、隐私、活动配置及路径检查保留，批末重查版本与隐私；任一失败都不加入部分正文，不提高预算或发请求。模型文件工具的原扫描/展示限制以及实时引用的大文件仅路径/大小规则保持。具体生命周期、取消和预算说明见[完整文本附件](file-snapshots.md)。

需要后续修改自动刷新的文件时，用 `/ref PATH` 或启动参数 `--ref-file PATH`，配置为 `[context].files = ["src/main.py"]`。每次请求前刷新完整小文件，较大文件只提供路径和大小，按需读取；正文不因引用本身复制到保存历史。引用集合随会话保存与恢复，新建对话清空。配置是新会话的初始选择；恢复会话沿用已保存选择，显式 CLI 标志再合并。完整范围、Windows 路径和隐私说明见[实时文件引用](live-file-references.md)。TUI 也支持 `@关键词` 后按 Tab 选择[仅本轮使用的文件](tui-mentions.md)，主输入框 Enter 发送；手动粘贴同样的标记不会自动附加正文。

超长单行可由文件工具使用版本绑定的字节游标继续读取；附件仍要求完整文件。TUI 单条发送文本和待处理引导分别受 UTF-8 字节限制，超限保留草稿；具体上限、续读行为和传输格式见[文件与大文本](text-transfer.md)。

`[context].keep_read_bytes` 默认 8192（0–262144），只在压缩触发时，在完整请求硬限额内额外保留少量完整读取快照。额度包含结果的 JSON 转义和元数据；不足时继续省略，不截断或增加请求。0 关闭保留，CLI 为 `--context-keep-read-bytes`；profile、显式项目层和生效来源同步支持。旧快照不含当前 `read_id`，编辑仍需重新读取与新批准。详见[源码窗口保留](read-retention.md)。

压缩后仍超限时，会尝试把已完成且正文已省略的旧只读批次合并为历史数据，保留参数、状态和原助手文字；错误、拒绝、写入、命令、保留源码和最近消息不折叠。没有新的模型调用或自动重试，完整会话不变。`/context` 显示 `folded_inspections`，细节见[历史协议折叠](inspection-folding.md)。


## 完整配置与模型目录

提供商可配置 `preserve_reasoning`（默认 false）与 `check_history_format`（默认 `"tool_calls"`，可选 `"observations"`），用于显式保留助手推理协议字段及投影应用固定检查历史。两者按提供商/CLI 解析、显示来源并绑定会话；项目层不能自行开启，默认不影响原有请求。启用保留会使后续请求和已启用的私有保存包含该字段，终端不显示正文。CLI 参数与 DeepSeek 格式示例见[推理工具历史兼容](reasoning-protocol.md)。

可复制的所有设置见 [`config.example.toml`](../config.example.toml)；开源样本的文件格式、设置范围与 Sprig 对应情况见[调研表](configuration-comparison.md)。示例不含真实密钥。旧 `[agent].max_tokens/thinking` 继续兼容；同层 `[generation]` 的同名设置优先。表以键合并，标量和数组以高优先级整体替换；`extra_body` 整个对象替换，不隐式深合并。文件排除在项目层与用户/profile 排除合并。

```toml
provider = "USTC"
interface = "tui"

[providers.USTC]
model = "fast"
base_url = "https://api.llm.ustc.edu.cn/v1"
api_key_env = "USTC_API_KEY"
request_timeout = 60
proxy = "system"

[providers.USTC.models.fast]
id = "deepseek-flash"
max_context_bytes = 65536

[providers.USTC.models.fast.generation]
max_tokens = 2048
temperature = 0.2

[profiles.review]
provider = "USTC"
model = "fast"
[profiles.review.agent]
read_only = true
checks = []
```

`--profile review` > `SPRIG_PROFILE` > 顶层 `profile`；profile 不继承其他 profile。最多 32 个提供商、32 个 profile，每提供商 64 个模型别名。同提供商每条模型 ID 唯一。`--model fast` 或该条目的真实 ID 都应用条目设置；未列入目录的真实模型名仍可显式传入。显式换提供商时，不携带另一个提供商 profile 的模型别名。

```sh
uv run --locked sprig --models
uv run --locked sprig --provider USTC --list-models
uv run --locked sprig --profile review
uv run --locked sprig --provider USTC --model fast --show-config
uv run --locked sprig --config-schema
```

`--models` 只读本地目录，未设置密钥也能使用。`--list-models` 是用户明确发起的 GET `/models`；返回至多 256 个去重 ID、总数与截断标记；服务不支持时会报错。它不发送任务或 completion，不计为 completion 请求，不推断模型能力、价格或自动选择模型。TUI `/models` 只显示启动时加载的本地目录。

编辑器可用 TOML 插件配置本地 `config.schema.json`，示例首行提供 schema 关联。也能用 `--config-schema` 导出；该操作即使配置损坏也可运行。Schema 辅助补全，运行时仍检查组合、权限、路径与端点。配置上限 64 KiB；无效 TOML 尽可能给出行列位置，内容一律不回显。

## 生成与传输

`[generation]`、`[providers.NAME.generation]`、`[providers.NAME.models.ALIAS.generation]` 和 `[profiles.NAME.generation]` 支持同一组设置：

| 字段 | 范围与实际请求 |
| --- | --- |
| `max_tokens` | 1–65536，单次输出上限，不是总费用预算 |
| `token_limit_field` | `max_tokens`（默认）或 `max_completion_tokens`；只发所选字段 |
| `temperature` / `top_p` | 有限数值 0–2 / 0–1；未配置时不发送 |
| `seed` | 0–2147483647 的整数；服务不一定保证可复现 |
| `reasoning_effort` | none/minimal/low/medium/high/xhigh/max；服务支持范围可能更窄 |
| `thinking` | true 时发送 `chat_template_kwargs.enable_thinking=true`；不是通用推理协议 |
| `extra_body` | 额外 JSON/TOML 表，至多 16 KiB、深度 12、32 个顶层键；模型、消息、工具、流式、鉴权、token 等保留键禁止覆盖 |

生成设置计入实际序列化请求预算，也绑定保存的会话；变更后不能直接发送旧会话。`thinking=true` 与额外 `chat_template_kwargs` 同时定义会报错。CLI 支持 `--temperature`、`--top-p`、`--seed`、`--reasoning-effort`、`--token-limit-field` 等覆盖。

提供商可配置 `request_timeout`（0–120 秒，不含 0），限制网络空闲与连接阶段，未配置时沿用 `agent.timeout`；后者仍控制命令。`request_deadline` 默认 300 秒（0–3600 秒，不含 0），限制一次请求总时长，服务持续发送少量字节或 SSE 心跳不能延长它；不把 DNS 的同步系统调用宣称为可强制中断，详见[请求生命周期与取消边界](request-lifecycle.md)。CLI 可用 `--request-timeout`、`--request-deadline` 覆盖；两者均不进入模型 JSON，不改变会话绑定。

`proxy` 为 `system`、`direct` 或不含账号、查询、片段的 HTTP(S) 代理地址，CLI 可用 `--proxy` 覆盖。默认系统代理仍由 Python 标准库处理；explicit proxy 同样遵循标准库的 NO_PROXY 行为。TLS 校验和禁止重定向始终保留。

模型看到的 `run` 工具超时上限使用生效的命令 `timeout`，不固定显示120秒，也不取提供商的 HTTP `request_timeout`。提案省略 `timeout` 时使用命令上限；超限值在审批前拒绝，错误给出实际数字，不自动延长或改写请求。不同 Agent 的工具定义独立，修改一个实例不影响其他实例。

`api_key_header="Authorization"` 与 `api_key_scheme="bearer"` 为默认；兼容服务可改成 `api_key_header="X-Api-Key"`、`api_key_scheme="raw"`。`headers` 提供不插值的字面值，`headers_env` 提供环境变量名；合计最多 16 个。Host、Content-Length、Content-Type、Authorization 等保留头与鉴权头不能通过额外 headers 覆盖；头名不区分大小写，拒绝换行注入。所有值都按敏感信息隐藏、脱敏、从子进程过滤；配置的头也不能随基础地址覆盖转发。

## 工具、文件与执行环境

`[tools].file_action=false` 禁用目录创建、读取标识删除和复制后删除的移动；`edit_file=false` 也同时禁用 `file_action` 与 `edit_lines`，显式项目层不能重新打开。CLI 为 `--disable-tool file_action`；只读模式在 schema 和实际执行层阻止这些写入。移动的两个阶段分别取得新批准，不提供原子 rename，见[专用文件操作](file-actions.md)。

`[tools]` 对十八个名称设置 true/false（含兼容入口）：`list_files`、`read_file`、`search`、`grep`、`project_map`、`edit_file`、`edit_lines`、`file_action`、`run`、`update_plan`、`question`、`skill`、`mcp`、`web_fetch`、`job`、`lsp`、`read_history`、`read_log`。false 同时移除对应模型 schema，并由实际执行层检查；CLI `--disable-tool NAME` 可重复。`question` 仅在交互式 TUI/终端 CLI 或显式嵌入回调中提供，支持需求选项与自填；答案不能批准操作，同批其他提案跳过后重新规划，见[澄清流程](questions.md)。`skill` 仅在显式配置 `[skills].dirs` 时提供，按需加载技能说明和资料；不是执行或授权入口，见[技能配置](skills.md)。`read_history` 仅在有显式摘要检查点时提供，按消息编号回查当前对话被概括的原文；`read_log` 仅在配置 `command_log_dir` 时提供，按不透明句柄回查当前会话的历史命令输出。两者都不接受任意文件或其他会话路径，本地 `/history`、`/log` 是用户查看控制。无可选配置的非交互任务仍为九种工具，交互式任务增加 `question` 后为十种；摘要、日志、技能、MCP、网页、后台和 LSP 都启用时最多十七种。`lsp` 需明确配置服务器并允许 read_file，非只读时向模型提供，详见[语义查询](lsp.md)。`job` 需用户/profile 的 `[execution] background=true` 或 CLI `--background` 明确开启，run/job 同时可用且不为只读；启动统一使用 run 的 background 参数，详见[后台命令](background-jobs.md)。`web_fetch` 需要用户/profile 的 `[web] enabled=true` 或 CLI `--web` 明确开启；逐次批准公开 HTTP(S) 请求，缓存分页不联网，见[网页读取](web-fetch.md)。关闭 `read_file` 也阻止 `/add` 附件；关闭 `edit_file` 同时禁止 `edit_lines` 与 `file_action`，项目不能重新启用。整行编辑的短版本标识仍关联完整摘要并要求新批准，见[读取绑定编辑](read-bound-edits.md)。`run=false` 或 `read_only=true` 不能配置非空 `checks`。`update_plan` 只维护模型报告的进度，不授权执行或替代验收，详见[任务计划](task-plans.md)。

模型内容搜索使用默认 RE2 正则的 [grep](grep.md)，`literal=true` 使用原文；旧 `search` 仍默认字面值、`regex=true` 启用正则。两个名称任一个 false 同时禁用两者，项目层不能通过另一名称重新开启。兼容入口不另占模型工具数。禁用单个工具只控制该工具接口，例如 `grep` 仍会读取匹配内容。要禁止某些路径通过所有文件工具访问，使用 `[files].exclude`；要禁用全部命令，使用 `run=false` 或只读模式。任意获批命令仍无 OS 沙箱，不能把文件排除当作命令隔离。

`[files].exclude=["private/", "*.local.json"]` 与 `.sprigignore` 合并，总计最多 256 条。使用相对 POSIX glob，Windows 也用 `/`；不支持否定、`..`、绝对路径或反斜杠。内置敏感路径与所有活动配置路径的保护始终叠加。

`[execution]` 可设置 `inherit_env`、`timeout`、`max_output`（同层优先于 agent 中旧字段），以及 `env_allowlist=["CC", "CFLAGS"]` 和 `env={BUILD_MODE="debug"}`。数组只额外继承具名变量，表提供字面覆盖；不执行替换、取密钥命令或 shell。即使允许列表或继承全部环境，也移除所有配置提供商的凭据变量、头变量及包含已知模型凭据的值。禁止用 env 注入模型凭据变量或凭据值。配置的字面值不显示在日志中；值若用于执行代码、模块搜索路径等，应按正常命令审批审阅其影响。

`[storage].command_log_dir` 或 `--command-log-dir PATH` 显式启用专用私有命令日志目录，绑定会话，默认不保留。每次命令批准包含存储与候选淘汰；每份最多 8 MiB、专用 journal 最多 16 MiB/16 条，七天后不可读，磁盘清理仍需获批操作。模型 `read_log` 和本地 `/logs`、`/log ID [OFFSET]` 支持回查，`/logs clear` 新批准后只清理当前会话；不提高原始输出上限，不替代验收，详见[日志回查与隔离边界](command-logs.md)。

长测试日志默认采集至多 1 MiB，`max_output` 展示预算仍独立（默认 16000 字节），超出展示预算保留有界首尾。用户或 profile 可设置 `[execution].output_limit`，范围 0 或 256–8388608，CLI 为 `--command-output-limit`；0 明确恢复在 `max_output` 处停止的旧策略，不表示无限输出。新默认及显式采集上限均绑定会话；没有旧采集上限绑定的历史会话须使用 0 及原来的展示预算恢复，不静默迁移。首尾展示的 `output_truncated` 与进程被停止的 `output_limited` 含义不同，见[命令输出与验收边界](command-output.md)。

## 后台命令

`[execution] background=false` 为默认值，background_timeout 默认 300 秒、最大 3600 秒；普通前台 timeout 不变。用户/profile 可配置，CLI `--background` / `--no-background`、`--background-timeout` 覆盖；项目层不能声明 execution。后台受管进程、内存输出和任务状态不随会话恢复重启；与固定验收的边界见[后台命令](background-jobs.md)。

## 网页文档

`[web] enabled=false` 为默认值；用户和 profile 可开启，CLI `--web` / `--no-web` 覆盖，项目配置禁止声明此组。`[tools] web_fetch=false` 或 `--disable-tool web_fetch` 进一步禁用；只读模式阻止执行。没有额外依赖，不继承认证/代理或保存单独网页缓存，详见[网页读取的地址、分页与限额](web-fetch.md)。

## MCP 外部服务

`[mcp.servers.NAME]` 支持明确的 `streamable_http` 端点，或 `stdio` 的固定 argv / 相对 cwd / env / env_from；协议版本、超时/总期限共用。HTTP 可配置代理、headers / headers_env，stdio 禁止混入 HTTP 字段。用户/profile 可配置，项目层禁止改变，profile 按服务名替换完整定义，最多八个服务。配置和 `--list-mcp` 不连接或启动进程；TUI/chat `/mcp` 管理状态，连接和每次 RPC 单独批准。只读模式在执行层阻止两种后端。版本、Schema 子集、结果分页和未实现的 OAuth 等见 [HTTP MCP](mcp.md) 与 [stdio](mcp-stdio.md)。

## LSP 语言服务器

`[lsp.servers.NAME]` 只接受已安装服务器的明确 command argv 与 languages 后缀映射，另可设置 enabled、相对 cwd、env/env_from、timeout/deadline。最多八个；用户/profile 按名称替换完整定义，项目层不能注入。`--show-config` 和 TUI/chat `/lsp` 只读本地状态，启动和每次语义查询新批准。配置模型供应商不会自动选择语言服务器，模型切换仍不能改变其权限。服务器不是系统沙箱；只读模式、私有路径、已知密钥正文和关闭 read_file/lsp 都在执行层阻止同步/查询。详见[配置示例、协议范围和取消/恢复](lsp.md)。

## 存储与终端

### 上下文策略

```toml
[context]
max_bytes = 131072
compact = true
trigger_ratio = 0.8
keep_recent_messages = 10
```

`max_bytes` 是完整序列化请求的字节上限，不是模型 token 窗口。启用 `compact` 后，到达 `max_bytes * trigger_ratio` 才尝试省略旧成功工具输出和已批准完成的编辑文本；比例为 0–1，默认 0.8，先保留预算内的完整观察；显式设为 0 保留旧的每轮尝试省略行为。关闭 compact 时不会自动开启。`keep_recent_messages` 为 6–1000，默认 6，只能增加保护窗口。拒绝、用户要求、未完成调用与最新计划保留；最新失败和普通失败命令的详细输出保留。固定检查已有更新的可靠观察时，旧检查可省略 argv 和正文，但保留失败/批准状态及新观察引用；旧读取正文省略时也省略其附属版本/行位置元数据，见[固定检查上下文](check-history-context.md)。完整保存历史不改写，压缩后仍超限时不发请求。当前没有模型摘要请求或自动付费重试。

同层 `[context].max_bytes/compact` 覆盖旧 `[agent].max_context_bytes/compact_context`；旧写法继续支持，未知或无效低层设置也会报错。用户、profile、显式受限项目层均可设置，CLI 的 `--max-context-bytes`、`--compact-context`、`--context-trigger-ratio`、`--context-keep-recent-messages` 优先。`--show-config`/TUI `/config` 展示实际值与来源。`/context` 和 `/compact` 的本地预览显示强制压缩的潜力，真实请求仍遵循配置阈值；关闭压缩时阈值不使它自动开启。

### 会话保留与显式清理

```toml
[storage]
session_max_age_days = 30
session_max_count = 64
```

这两个可选字段分别接受 1–36500 天和 1–256 个有效会话；未设置时不提供保留清理。使用最后修改时间，按新到旧保留数量；超过年龄或数量任一条件即为候选。当前进程活动会话、其他进程占用和无效/不安全文件均保留，因此数量是候选选择规则，不是磁盘硬配额。仅清理会话 JSON，不删除修改备份、锁文件或其他文件；项目层不能配置存储策略。

```sh
uv run --locked sprig --prune-sessions --dry-run
uv run --locked sprig --prune-sessions
```

TUI 使用 `/prune --dry-run` 和 `/prune`。预览只显示候选名称、大小、修改时间，不显示对话正文。实际删除逐个显示候选和策略、取得新批准，再核对租约、私有权限、文件身份与内容；拒绝即停止剩余清理，取消也停止后续动作，已经删除的会话不会回滚。审批期间文件改变会跳过。只读模式允许预览，禁止清理。扫描有 4096 项目录、256 会话、8 MiB 总读取等界限；结果不完整时整个清理不执行，不能据局部列表判断最旧会话。

启动、自动保存和列会话均不自动清理，也不调用模型。CLI 可用 `--session-max-age-days` / `--session-max-count` 覆盖；配合显式 `--session-dir` 时同样无需有效模型配置或密钥。生效值和来源由 `--show-config` 显示。两个策略均不改变会话绑定或恢复授权。

`[storage]` 可设置 `session_dir`、`change_history_dir` 和 `auto_save_session`。目录支持 `~`，相对目录以声明它的配置文件目录为基准；启动参数路径以当前目录为基准。会话和精确编辑备份仍要求私有目录、位于仓库外，内容不加密。`auto_save_session` 默认 false；开启后每次新启动自动分配唯一会话名，显式 `--save-session` / `--resume` 优先。TUI `/new` 仍解除当前自动保存，避免覆盖旧会话。列会话无须模型密钥；显式 `--session-dir` 可绕过损坏的模型配置进行本地管理。

`[tui]` 支持：

- `theme`：textual-dark/textual-light/nord/gruvbox/dracula/monokai/tokyo-night/solarized-dark/solarized-light。
- `sidebar`：显示侧栏，默认 true；隐藏后仍可用 slash 命令。
- `editor_height`：3–12，默认 5；Enter 发送、Shift+Enter/Ctrl+J 换行。
- `steering_mode`：all（默认）或 one-at-a-time，决定在 agent 安全边界一次注入全部或一条已排队的新要求；不会放行待审批操作。
- `metrics`：退出时输出本地用量统计；CLI `--metrics` / `--no-metrics` 可覆盖。

`[tui.keybindings]` 可给本地动作设置附加快捷键：

```toml
[tui.keybindings]
help = ["f1"]
toggle_sidebar = ["ctrl+b"]
focus_input = ["f3"]
cancel = ["f6"]
quit = ["ctrl+f10"]
new_conversation = ["ctrl+shift+n"]
```

支持上述六个动作，每动作至多四个键。用户/profile 按动作合并，较高层的同名数组整体替换；设置 `help=[]` 仅关闭该动作的附加别名。F1–F12 可单独绑定；字母、数字、home/end/pageup/pagedown/up/down/left/right 需带 Ctrl 或 Alt，可另加 Shift。修饰键顺序归一化，跨层重复或冲突也报错；终端是否报告某个组合仍取决于终端。`/keys` 查看生效别名。配置采用启动快照，修改后重启；规则/语言以外的界面偏好不改变会话绑定。

Enter、Shift+Enter、Ctrl+J、Esc、Tab、Ctrl+C/Q/L 和选择/剪贴板键保持固定，不提供审批动作绑定。模态审批中，取消/退出别名走原有取消流程，其余别名无效；不会因快捷键批准操作。新对话仍执行已有确认。项目层不能改变可信键位。

## 显式项目设置

```sh
uv run --locked sprig --project-config /trusted/private/project.toml
```

项目文件也需私有权限，并显式指定；不会沿工作区父目录搜索。允许 `model`、`interface`、`generation`、`context`、`agent`、`tools`、`files`、`instructions`、`tui`（不含 keybindings）、`schema_version=1`。禁止 providers、profile、execution、storage；不能添加 checks 命令或开启 inherit_env，不能关闭用户/profile 只读或已启用基线、启用已关闭的工具或移除其文件排除。CLI 的显式用户选择仍最高优先级。

`[skills].dirs` 最多八个明确的技能集合目录，相对路径以声明配置文件为基准，CLI 重复 `--skill-dir` 以工作区为基准并覆盖整个数组。用户和 profile 可以设置，项目层禁止改变目录；目录绑定会话。`--list-skills` 无须模型凭据即可离线检查，TUI/chat `/skills` 和 `/skill NAME [PATH [OFFSET PAGE_SHA256]]` 提供本地预览。详见[按需技能、分页与权限边界](skills.md)。

MCP OAuth/订阅、插件、hooks、沙箱、自动批准等未提供后端的字段仍会报错。技能加载不会运行脚本或授予权限；后续扩展需要同时实现后端与安全测试，不能只添加一个可解析开关。

模型协议由供应商表的 wire_api 选择，支持 chat_completions（默认）、messages、responses；CLI 对应 --wire-api。认证、生成参数映射、原生推理状态、会话绑定与限制见 [模型协议](provider-protocols.md)。

运行中可用 TUI“切换模型”或 `/model PROVIDER/ALIAS` 选择启动配置中的模型；`/model @startup` 恢复启动模型。切换需要明确确认，不自动请求，已保存会话分支重新绑定；凭据、CLI 覆盖和历史投影规则见[模型选择](model-selection.md)。配置文件和环境修改仍需重启。
