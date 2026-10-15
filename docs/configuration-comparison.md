# 开源 agent 配置调研与 Sprig 的对应实现

核对日期：2026-10-15。选择 OpenCode、Aider、Pi 作为成熟开源编程 agent 的样本；这不是排行榜，也不是完整性能对照。以下是官方文档所述接口，不代表本地已经运行了它们的每个选项。

| 样本 | 文件与加载方式 | 文档中的主要设置范围 |
| --- | --- | --- |
| [OpenCode](https://opencode.ai/docs/config/) | JSON/JSONC；用户、项目、显式路径等层级合并；TUI 使用单独的 `tui.json` | 模型/提供商、工具与权限、压缩、指令、网络、主题、MCP、插件、LSP、格式化和快捷键 |
| [Aider](https://aider.chat/docs/config/aider_conf.html) | `.aider.conf.yml`；通常按 home、仓库、当前目录加载，CLI 优先；`--config` 显式选择文件 | 模型/别名、连接与生成参数、仓库地图、聊天历史、输出、Git、lint/test；具体字段见[选项表](https://aider.chat/docs/config/options.html) |
| [Pi settings](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/settings.md) / [models](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/models.md) | JSON；用户与项目设置，另有模型目录与自定义提供商配置 | 默认模型与思考选项、引导队列、工具、会话、压缩、终端显示、重试/网络、自定义模型参数与扩展资源 |

## Sprig 已接入的设置

配置统一使用 TOML。完整可复制示例为 [`config.example.toml`](../config.example.toml)，类型提示为 [`config.schema.json`](../config.schema.json)，运行说明见[配置文档](configuration.md)。这些设置都连接到运行时，不仅列在示例中。

| 范围 | Sprig 入口 | 实际行为 |
| --- | --- | --- |
| 用户/项目层级 | `--config`、`SPRIG_CONFIG`、`--project-config` | 用户文件需显式信任；项目层显式加载，不能重定向凭据或扩大工具权限 |
| 提供商/模型目录 | `[providers.NAME]`、`.models.ALIAS`、`--models` | 别名解析成真实 API model ID，可配置每模型的生成与请求预算；离线列出目录 |
| 命名工作模式 | `[profiles.NAME]`、`--profile`、`SPRIG_PROFILE` | 一次选择提供商、模型、只读模式、工具与终端偏好 |
| 生成参数 | `[generation]`、提供商/模型/profile 下的 `generation` | token 上限字段、温度、top-p、seed、reasoning effort、thinking、受限额外 JSON；计入真实请求预算并绑定会话 |
| 传输与鉴权 | 提供商的 `request_timeout`、`proxy`、`api_key_header/scheme`、`headers/headers_env` | 可分离 HTTP/命令超时，支持直连/系统/显式代理与兼容服务自定义头；禁止重定向和保留头覆盖 |
| 服务模型元数据 | `--provider NAME --list-models` | 明确联网 GET `/models`，有界解析；不调用 completion，也不混入 completion 用量统计 |
| 工具与隐私 | `[tools]`、`[files].exclude`、`--disable-tool` | 同时限制模型 schema、分发和直接方法；排除规则与 `.sprigignore` 合并；内置凭据与配置文件保护不能关闭 |
| 命令环境 | `[execution].env_allowlist/env` | 最小环境上增加指定变量；模型凭据即使被加入允许列表也不继承；命令仍逐次批准 |
| 会话/备份 | `[storage]` | 自定义私有存储目录，可显式启用自动会话保存，默认关闭；恢复校验模型、生成设置与工具/文件策略 |
| TUI | `[tui]` | 主题、侧栏、输入框高度、一次注入全部/一条中途引导、退出用量；Enter 发送保持不变 |
| 诊断/编辑器 | `--show-config`、`--doctor`、`--config-schema` | 生效设置、离线检查、编辑器 JSON Schema；未知字段和无效组合停止启动，错误不打印原文密钥 |

## 明确保留的边界与后续差距

Sprig 仍使用 Chat Completions function tools；配置其他协议的 URL 不会自动得到原生 Anthropic、Responses 或 Google 协议支持。`--list-models` 只证明元数据接口可读，不证明工具调用、流式、推理选项或价格可用。

MCP、LSP、可执行插件/skills/hooks、外部编辑器、自定义快捷键、操作系统沙箱、PTY、持久后台任务和多代理还缺执行后端。本轮不会接受相应空壳配置，也不会把普通获批 shell 命令当作这些专用功能已完成。

Sprig 不提供配置自动批准、自动模型回退、隐式联网配置、关闭 TLS 验证、凭据获取命令或自动重试；保留逐次审批、明确端点和计费请求的边界。部分样本中的这些可选行为不适合直接照搬。

## 验证含义

回归覆盖真实回环 HTTP、原生子进程环境、文件读取/编辑拒绝、会话恢复与 Textual UI。它们证明实现行为，不证明模型编码能力达到上述样本。真实 USTC `deepseek-flash` 的固定简单任务另有七项外部验收通过记录；后续仍需要多仓库、多任务、同模型同预算的结果。
