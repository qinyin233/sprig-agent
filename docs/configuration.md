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
- 提供商：`--provider NAME` > `SPRIG_PROVIDER` > 顶层 `provider`。可定义至多 32 个命名提供商，没有自动切换或失败后备用模型。
- 模型与地址：`--model` / `--base-url` > `SPRIG_MODEL` / `SPRIG_BASE_URL` > 选定提供商。没有模型时明确报错；未选提供商时保留原 OpenAI 地址默认值。
- 密钥：显式 `SPRIG_API_KEY` > 选定提供商的 `api_key_env` 或 `api_key` > 旧环境别名。声明了 `api_key_env` 却未设置时停止，不挪用另一个提供商的全局密钥。旧别名仍为 Agnes HTTPS 原点的 `AGNES_API_KEY`，然后 `OPENAI_API_KEY`。
- 配置密钥绑定该提供商的完整基础地址。覆盖地址时，必须另设显式 `SPRIG_API_KEY`，避免把原密钥转发到别处。`AGNES_API_KEY` 的特殊引用始终限于其 HTTPS 原点。
- 运行参数：显式 CLI > `[agent]` > 原有默认值。布尔参数支持 `--no-stream`、`--no-thinking`、`--no-compact-context` 等覆盖。TUI 始终流式；选择 `--cli --no-stream` 使用非流式单次执行。

Sprig 不自动读取项目或父目录的配置，也不加载 `.env`、远程配置、配置脚本或动态环境替换。这样下载的仓库不能改变接收密钥和代码的服务地址。`--config` 是对该文件的显式信任，应先检查其内容。

`[agent]` 支持 `max_steps`、`timeout`、`max_output`、`stream`、`request_usage`、`read_only`、`inherit_env`、`max_context_bytes`、`compact_context`、`project_instructions`、`max_tokens`、`thinking`、`checks`。均执行现有类型、范围和组合校验；未知设置报错，不能通过配置自动批准、关闭隐私检查或绕开只读。`checks` 是 argv 数组的数组，启动参数 `--check` 替换整组；恢复会话仍核对模型、地址、工作目录、固定验收和生成参数。

## 离线检查

```sh
uv run --locked sprig --show-config
uv run --locked sprig --doctor
uv run --locked sprig --config /trusted/path/config.toml --show-config
uv run --locked sprig --no-config --model local-model --base-url http://localhost:8000/v1 --doctor
```

`--show-config` 显示生效设置、密钥是否存在及来源类别，密钥值不显示。`--doctor` 检查配置、工作目录/`.sprigignore`、必要依赖和平台，不连接模型、不执行项目命令、不创建会话。它不能证明模型支持工具调用、服务在线或实际 OS 隔离。无效配置的错误不会回显 TOML 原文。

活动配置若显式位于工作区内，文件工具仍排除它；`.sprig` 目录始终排除。获批子进程即使开启 `inherit_env`，也移除标准模型密钥变量、所有配置声明的密钥变量及包含当前密钥的值。**这些保护不构成命令沙箱**：获批任意代码仍以当前用户权限运行，可自行读取主目录文件。

## TUI 本地命令

`/help` 列出命令；`/config` 和 `/status` 查看本地状态。`/context`、`/usage`、`/map [查询]`、`/git`、`/diff`、`/compact`、`/changes`、`/undo`、`/redo`、`/discard-history` 对应侧栏操作，保留原先的审批和只读限制。

`/save 名称` 保存或另存当前对话；另存新名称可分支后续对话，旧会话保留。`/restore 名称`、`/sessions`、`/delete 名称` 管理本地会话。`/new` 确认后开始新对话，解除原会话的自动保存和锁，不覆盖它，也不回滚工作区。提供商/模型通过配置或下次启动参数选择，当前会话不偷偷换服务。

任务进行中可输入普通文字调整要求；`/status`、`/config`、`/help` 可以本地查看，`/cancel` 取消，`/exit` 取消后退出。需要空闲的本地命令会提示等待，不会排队成为模型请求。未知 `/命令` 不发送给模型；要发送以 `/` 开头的普通文本，用 `//` 开头。

这些 slash 命令用于 TUI；旧 `--chat` 保留自己的 `/help` 所列命令。
