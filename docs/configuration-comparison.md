# 开源 agent 配置调研与 Sprig 的对应实现

核对日期：2026-10-15；2026-10-16 补充 Gemini CLI，2026-10-17 重新核对 OpenCode/Aider/Gemini CLI/Pi 的官方资料，见[日期调研记录](configuration-research-20261016.md)。选择有代表性的开源编程 agent 作为样本；这不是排行榜，也不是完整性能对照。以下接口不代表本地已经运行了样本的每个选项。功能配置与模型编码成绩分别验收。

2026-10-20 的 [OpenCode 固定版本源码阅读](research/opencode-source-reading-20261020.md)进一步追踪当前开发分支的配置发现、权威策略、领域插件和运行时；下表保留此前官方文档口径，不将其中每个配置名称视为当前分支已连接的后端。后续实现按源码对照及验收顺序推进。

| 样本 | 文件与加载方式 | 文档中的主要设置范围 |
| --- | --- | --- |
| [Codex 官方配置参考](https://developers.openai.com/codex/config-reference/) / [配置层级](https://developers.openai.com/codex/config-basic/) | 用户/受信项目 `config.toml`；当前 profile 为 `~/.codex/NAME.config.toml`；组织要求单独约束 | 提供商/模型、推理、上下文、项目指令、审批/沙箱、命令环境、MCP/skills/多代理、TUI keymap/主题/通知 |
| [OpenCode](https://opencode.ai/docs/config/) | JSON/JSONC；用户、项目、显式路径等层级合并；TUI 使用单独的 `tui.json` | 模型/提供商、工具与权限、压缩、指令、网络、主题、MCP、插件、LSP、格式化和快捷键 |
| [Aider](https://aider.chat/docs/config/aider_conf.html) | `.aider.conf.yml`；通常按 home、仓库、当前目录加载，CLI 优先；`--config` 显式选择文件 | 模型/别名、连接与生成参数、仓库地图、聊天历史、输出、Git、lint/test；具体字段见[选项表](https://aider.chat/docs/config/options.html) |
| [Pi settings](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/settings.md) / [models](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/models.md) | JSON；用户与项目设置，另有模型目录与自定义提供商配置 | 默认模型与思考选项、引导队列、工具、会话、压缩、终端显示、重试/网络、自定义模型参数与扩展资源 |
| [Gemini CLI 配置](https://geminicli.com/docs/reference/configuration/) | 用户/项目 `settings.json`，系统默认和强制层；受信目录机制可另行启用 | 模型、压缩阈值、工具权限、MCP/hooks、界面、会话保留和通知 |

## Sprig 已接入的设置

配置统一使用 TOML。完整可复制示例为 [`config.example.toml`](../config.example.toml)，类型提示为 [`config.schema.json`](../config.schema.json)，运行说明见[配置文档](configuration.md)。这些设置都连接到运行时，不仅列在示例中。

2026-10-24 校正 Sprig 的当前状态：下列对照不再沿用早期“仅 Chat Completions、缺 MCP/LSP/图片”的实现盘点；三协议与扩展的具体验证范围以各功能文档为准。另补上[可复用任务模板](prompt-templates.md)，不将模板展开当成自动执行后端。

| 范围 | Sprig 入口 | 实际行为 |
| --- | --- | --- |
| 用户/项目层级 | `--config`、`SPRIG_CONFIG`、`--project-config` | 用户文件需显式信任；项目层显式加载，不能重定向凭据或扩大工具权限 |
| 提供商/模型目录 | `[providers.NAME]`、`.models.ALIAS`、`--models` | 别名解析成真实 API model ID，可配置每模型的生成与请求预算；离线列出目录 |
| 命名工作模式 | `[profiles.NAME]`、`--profile`、`SPRIG_PROFILE` | 一次选择提供商、模型、只读模式、工具与终端偏好 |
| 常用任务模板 | `[prompts.NAME]`、profile 同名表、`/NAME 参数`、`--list-prompts` | 用户/profile 中的有界正文按名称合并，Tab 补全、Enter 发送或执行中引导；展开一次后进入普通任务链，不执行 shell/文件插值或切模型，项目层禁止定义 |
| 生成参数 | `[generation]`、提供商/模型/profile 下的 `generation` | token 上限字段、温度、top-p、seed、reasoning effort、thinking、受限额外 JSON；计入真实请求预算并绑定会话 |
| 传输与鉴权 | 提供商的 `request_timeout`、`proxy`、`api_key_header/scheme`、`headers/headers_env` | 可分离 HTTP/命令超时，模型工具定义反映生效的命令上限；支持直连/系统/显式代理与兼容服务自定义头；禁止重定向和保留头覆盖 |
| 服务模型元数据 | `--provider NAME --list-models` | 明确联网 GET `/models`，有界解析；不调用 completion，也不混入 completion 用量统计 |
| 工具与隐私 | `[tools]`、`[files].exclude`、`--disable-tool` | 同时限制模型 schema、分发和直接方法；排除规则与 `.sprigignore` 合并；内置凭据与配置文件保护不能关闭 |
| 命令环境 | `[execution].env_allowlist/env` | 最小环境上增加指定变量；模型凭据即使被加入允许列表也不继承；命令仍逐次批准 |
| 会话/备份 | `[storage]` | 自定义私有存储目录，可显式启用自动会话保存，默认关闭；恢复校验模型、生成设置与工具/文件策略 |
| TUI | `[tui]` | 主题、侧栏、输入框高度、一次注入全部/一条中途引导、退出用量；Enter 发送保持不变 |
| 诊断/编辑器 | `--show-config`、`--doctor`、`--config-schema` | 生效设置、离线检查、编辑器 JSON Schema；未知字段和无效组合停止启动，错误不打印原文密钥 |
| 附加规则与语言 | `[instructions].files/response_language`、`--instruction-file`、`--response-language` | 规则使用工作区内的明确文件，完整校验、刷新并检查审批期间变动；偏好语言进入实际请求并绑定会话 |
| 快捷键 | `[tui.keybindings]`、`/keys` | 按动作配置附加组合键，支持帮助、侧栏、输入焦点、取消、退出和新对话；不替换 Enter 发送或审批行为 |
| 设置来源 | `--show-config` / TUI `/config` 的 `setting_sources` | 主要运行字段标出 default/user/provider/model/profile/project/CLI/environment/TUI 来源；不显示凭据值 |
| 任务计划 | `update_plan`、TUI 计划页、`/plan`、`[tools].update_plan` | 短计划随私有历史恢复，支持新要求后的修订；模型进度不提供操作授权或验收证明 |

## 按用途核对设置和配置方法

官方选项的名字不同，并不能直接把它们的配置文件复制给 Sprig。下列映射说明用途、入口与真实限制；可复制的 Sprig 示例见完整 TOML。

| 用途 | 样本中的典型入口 | Sprig 当前支持及边界 |
| --- | --- | --- |
| 连接、认证、模型 | Codex model_providers；OpenCode provider；Pi models.json；Aider API/alias/model metadata | 多提供商、环境密钥/私有文件、模型别名、鉴权头、额外头、超时、代理；Chat Completions、Messages、Responses 的客户端函数工具和 SSE，协议回环证据不等于所有兼容模型均实测 |
| 模型行为 | Codex reasoning；Pi thinking/每模型设置；Aider reasoning-effort | token 字段、温度、top-p、seed、reasoning_effort、受限额外请求体；服务未支持的选项不会因为配置可解析就变得可用 |
| 工作模式与覆盖 | Codex profiles；OpenCode 用户/项目层；Pi 用户/项目设置；Aider home→repo→cwd→CLI | 命名 profile，用户→提供商→模型→profile→显式项目→CLI；提供商/模型环境选择器另有明确优先级 |
| 项目知识、语言 | OpenCode instructions 路径/glob；Codex 项目文档；Aider read/chat-language | 分层 AGENTS.md、明确附加规则文件、回复语言、CLI/TUI 文件快照；附加规则暂不支持 glob、远程或工作区外路径 |
| 历史和上下文 | Pi compaction/会话；Aider history/map-tokens；Codex context 配置；Gemini sessionRetention | 实际请求字节上限、压缩触发比例/保护窗口/读取保留、显式有损摘要和原文回查、私有会话/恢复/目录、保留年龄/数量与显式清理；没有精确 token/费用上限或长期摘要收益结论 |
| 测试工作流 | Aider lint/test command；样本的用户/项目指令 | 固定 checks、可选任务前基线、完成后独立验收、失败反馈；所有命令逐次批准，没有自动 lint/format 后端 |
| 工具和命令环境 | OpenCode tools/permissions；Pi defaultTools；Codex environment/sandbox | 工具硬禁用、只读、路径排除、环境白名单/字面值、超时/输出上限/取消；整行工具继承基础编辑禁用，计划不授予权限，命令仍无 OS 沙箱 |
| 界面与快捷键 | OpenCode tui.json；[Pi 按动作 keybindings.json](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/keybindings.md)；Codex tui.keymap | 主题、侧栏、高度、引导队列、用量、按动作附加快捷键；固定输入/审批键不改，没有完整 Vim 模式 |
| 扩展、网络和 IDE | Codex/OpenCode MCP/skills；Pi 扩展资源；OpenCode LSP/formatters | 已有明确配置的 MCP HTTP/stdio、渐进技能文本、逐次批准的 LSP 查询及逐文件批准的语义重命名、公开网页文本读取与图片输入/获批模型读图；OAuth、hooks、formatter、浏览器与多代理仍缺 |
| Git 和外部交互 | Aider Git/编辑器；Codex/OpenCode 工作区/进程设置 | 状态/diff、获批文件工具撤销、明确创建 detached worktree、当前进程内受管后台任务；已有可信配置的 TUI 外部草稿编辑器；仍缺命令改动回滚、worktree 合并、PTY 和通知后端 |

Aider 的 `--config` 只加载指定的一个文件；默认文件按 home、仓库根、当前目录依次加载，后者优先。Pi 的用户/项目设置与资源合并规则见[官方配置说明](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/configuration.md)。Sprig 不隐式读取项目配置，避免下载的仓库改变凭据路由；`--project-config` 只接受受限层。

## 明确保留的边界与后续差距

Sprig 通过明确 `wire_api` 选择 Chat Completions、Messages 或 Responses，不从 URL 猜测协议，也没有 Google 原生协议。`--list-models` 只证明元数据接口可读，不证明工具调用、流式、推理选项或价格可用。

MCP OAuth/订阅/Apps、可执行插件/hooks、完整编辑器键位/Vim 模式、操作系统沙箱、PTY、跨进程后台恢复、通知和多代理还缺执行后端。未知或未接入的设置继续拒绝，也不会把普通获批 shell 命令当作这些专用功能已完成。技能文本与任务模板不能转成持续授权。

Sprig 不提供配置自动批准、自动模型回退、隐式联网配置、关闭 TLS 验证、凭据获取命令或自动重试；保留逐次审批、明确端点和计费请求的边界。部分样本中的这些可选行为不适合直接照搬。

## 验证含义

回归覆盖真实回环 HTTP、原生子进程环境、文件读取/编辑拒绝、会话恢复与 Textual UI。它们证明实现行为，不证明模型编码能力达到上述样本。真实 USTC `deepseek-flash` 的固定简单任务另有七项外部验收通过记录；后续仍需要多仓库、多任务、同模型同预算的结果。
