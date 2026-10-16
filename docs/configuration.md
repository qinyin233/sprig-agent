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

每次最多 16 个文件、每个至多 32 KiB 且受 `max_output` 限制、合计至多 128 KiB。读取还执行完整的 8 MiB 扫描和 10,000 行窗口上限。二进制、敏感文件、活动配置、路径越界和不完整内容都拒绝；先检查完整受限文件，不能借附件上传干净前缀而绕过文件后部的凭据检查。任何一个失败，该批都不加入对话；实际序列化上下文超限时也不加入、不发请求。大文件可在任务中明确路径，让模型按范围读取必要部分。

目前没有 `@` 模糊选择、实时引用集合或自动文件补全。


## 完整配置与模型目录

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

提供商可配置 `request_timeout`（0–120 秒，不含 0），未配置时沿用 `agent.timeout`；后者仍控制命令。`proxy` 为 `system`、`direct` 或不含账号、查询、片段的 HTTP(S) 代理地址。CLI 可用 `--request-timeout`、`--proxy` 覆盖。默认系统代理仍由 Python 标准库处理；explicit proxy 同样遵循标准库的 NO_PROXY 行为。TLS 校验和禁止重定向始终保留。

`api_key_header="Authorization"` 与 `api_key_scheme="bearer"` 为默认；兼容服务可改成 `api_key_header="X-Api-Key"`、`api_key_scheme="raw"`。`headers` 提供不插值的字面值，`headers_env` 提供环境变量名；合计最多 16 个。Host、Content-Length、Content-Type、Authorization 等保留头与鉴权头不能通过额外 headers 覆盖；头名不区分大小写，拒绝换行注入。所有值都按敏感信息隐藏、脱敏、从子进程过滤；配置的头也不能随基础地址覆盖转发。

## 工具、文件与执行环境

`[tools]` 对七种工具设置 true/false：`list_files`、`read_file`、`search`、`project_map`、`edit_file`、`run`、`update_plan`。false 同时移除模型工具 schema，并在分发、直接方法和撤销/重做处执行限制；CLI `--disable-tool NAME` 可重复。关闭 `read_file` 也阻止 `/add` 附件。`run=false` 或 `read_only=true` 不能配置非空 `checks`。`update_plan` 只维护模型报告的进度，不授权执行或替代验收，详见[任务计划](task-plans.md)。

禁用单个工具只控制该工具接口，例如 `search` 仍会读取匹配内容。要禁止某些路径通过所有文件工具访问，使用 `[files].exclude`；要禁用全部命令，使用 `run=false` 或只读模式。任意获批命令仍无 OS 沙箱，不能把文件排除当作命令隔离。

`[files].exclude=["private/", "*.local.json"]` 与 `.sprigignore` 合并，总计最多 256 条。使用相对 POSIX glob，Windows 也用 `/`；不支持否定、`..`、绝对路径或反斜杠。内置敏感路径与所有活动配置路径的保护始终叠加。

`[execution]` 可设置 `inherit_env`、`timeout`、`max_output`（同层优先于 agent 中旧字段），以及 `env_allowlist=["CC", "CFLAGS"]` 和 `env={BUILD_MODE="debug"}`。数组只额外继承具名变量，表提供字面覆盖；不执行替换、取密钥命令或 shell。即使允许列表或继承全部环境，也移除所有配置提供商的凭据变量、头变量及包含已知模型凭据的值。禁止用 env 注入模型凭据变量或凭据值。配置的字面值不显示在日志中；值若用于执行代码、模块搜索路径等，应按正常命令审批审阅其影响。

## 存储与终端

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

项目文件也需私有权限，并显式指定；不会沿工作区父目录搜索。允许 `model`、`interface`、`generation`、`agent`、`tools`、`files`、`instructions`、`tui`（不含 keybindings）、`schema_version=1`。禁止 providers、profile、execution、storage；不能添加 checks 命令或开启 inherit_env，不能关闭用户/profile 只读或已启用基线、启用已关闭的工具或移除其文件排除。CLI 的显式用户选择仍最高优先级。

MCP、插件、hooks、沙箱、自动批准等没有对应执行后端的字段都会报错。后续功能需要同时实现后端与安全测试，不能只添加一个可解析开关。
