# 编程 agent 配置调查与 Sprig 支持顺序

核对日期：2026-10-16。本记录只采用项目官方文档、官方仓库与协议规范；核对了网页当前内容，也读取了 Sprig 的 `config.example.toml`、`docs/configuration-comparison.md`、`settings.py`、`config.py`、`context.py`、`session.py` 和 TUI 实现。下面的差距表以本轮实现前为基线，末尾说明本轮落实项。没有读取用户私有配置、调用模型接口或测试第三方 agent。这里的“样本”表示有代表性的产品，不表示编码成绩排名。

Codex、OpenCode、Gemini CLI、Aider 的官方仓库分别提供 Apache-2.0、MIT、Apache-2.0、Apache-2.0 许可；Claude Code 作为公开文档充分的补充产品，其仓库许可保留所有权利、使用受商业条款约束，不应归入完全开源的 agent 实现。[Codex 许可](https://github.com/openai/codex/blob/main/LICENSE)、[OpenCode 许可](https://github.com/anomalyco/opencode/blob/dev/LICENSE)、[Gemini CLI 许可](https://github.com/google-gemini/gemini-cli/blob/main/LICENSE)、[Aider 许可](https://github.com/Aider-AI/aider/blob/main/LICENSE.txt)、[Claude Code 许可](https://github.com/anthropics/claude-code/blob/main/LICENSE.md)。

## 配置文件、层级和信任

| 样本 | 文件和覆盖规则 | 对 Sprig 有用的经验 |
| --- | --- | --- |
| Codex | 用户 `~/.codex/config.toml`；受信项目 `.codex/config.toml` 从项目根到当前目录覆盖；当前文档的命名 profile 使用独立 `~/.codex/NAME.config.toml`；CLI 优先。未受信项目跳过项目配置、hooks、rules。组织要求属于约束层，区别于默认值。[官方基础配置](https://developers.openai.com/codex/config-basic/) | 配置来源必须可见；“更具体”不应自动获得放宽安全约束或改变凭据路由的权力。不能继续依赖旧文档对 profile 形状的描述。 |
| OpenCode | JSON/JSONC；依次合并组织远程、用户、`OPENCODE_CONFIG`、项目、`.opencode` 目录、`OPENCODE_CONFIG_CONTENT`；冲突项后者优先。TUI 已分离到 `tui.json/jsonc`。[官方配置](https://opencode.ai/docs/config/) | 模型与界面偏好需要清晰分组，但 Sprig 不必为相同用途同时支持多种语法或隐式远程配置。 |
| Gemini CLI | `~/.gemini/settings.json`、项目 `.gemini/settings.json`；系统默认在下，系统强制设置高于普通文件，环境和 CLI 另有优先级。提供生成 Schema；Windows 系统位置在 `C:\ProgramData\gemini-cli`。[配置参考](https://geminicli.com/docs/reference/configuration/) | 系统策略与个人偏好应分开；跨平台路径应明确。普通项目配置不能成为凭据或自动命令执行的入口。 |
| Aider | `.aider.conf.yml` 默认依次加载 home、Git 仓库根、当前目录，后者优先；`--config` 只加载指定文件。[YAML 配置](https://aider.chat/docs/config/aider_conf.html) | 文档要精确说明“替换文件”还是“叠加层”，列表是替换还是合并。 |
| Claude Code | JSON 用户、共享项目 `.claude/settings.json`、项目私有 `.claude/settings.local.json`、CLI `--settings`、组织 managed 层；若字段重复，存在明确优先级，普通列表通常合并，特殊字段另有规则。环境变量不是一个统一的层级，而按具体变量处理。[设置层级](https://code.claude.com/docs/en/settings) | 不能笼统宣称所有环境变量一律最高优先；应逐个说明选择器、值和来源。 |

Gemini 的 Trusted Folders 文档说明该功能默认关闭；启用后，未受信目录不加载项目配置、项目 `.env` 或 MCP，并限制扩展等行为。不能仅因存在信任机制就假定默认启动已经获得相同保护。[官方受信目录文档](https://geminicli.com/docs/cli/trusted-folders/)。Sprig 当前显式 `--project-config` 的受限加载方式应保留，不照搬样本的隐式加载。

## 主要设置与实际后端

| 用途 | 已核实的代表性设置 | Sprig 已有支持及真正差距 |
| --- | --- | --- |
| 模型、端点、凭据 | Codex `model_providers` 下有 `base_url/env_key/http_headers/env_http_headers`；当前 `wire_api` 只接受 `responses`。[配置参考](https://developers.openai.com/codex/config-reference/) Aider 的模型 YAML 可配置 `extra_params`、编辑格式和辅助模型。[高级模型设置](https://aider.chat/docs/config/adv-model-settings.html) | 已有多提供商、环境密钥、模型别名、profile、受限生成参数、鉴权头、额外头与代理。仍仅有 Chat Completions function-tools 后端；配置 URL 不能提供 Responses、原生 Anthropic 或 Google 协议。 |
| 请求和流式超时 | OpenCode 提供请求 `timeout`、等待头的 `headerTimeout`、流式块间的 `chunkTimeout`。[提供商配置](https://opencode.ai/docs/config/#models) | 现有 `request_timeout` 与命令 `timeout` 已分开。进一步区分总期限、连接/响应头期限、流式空闲期限需要传输实现和无块/慢块测试，不能只增加三个字段。 |
| 模型选择限制 | OpenCode 有 `enabled_providers/disabled_providers`，禁用优先于启用。[配置参考](https://opencode.ai/docs/config/#disabled-providers) Claude Code 有 `availableModels/deniedModels`，并说明选择、别名、恢复等入口如何受约束。[模型配置](https://code.claude.com/docs/en/model-config) | Sprig 目前没有自动搜环境加载提供商或自动模型回退，因此白名单不是最急缺口。后续若引入切换/回退，限制必须覆盖 CLI、profile、恢复、TUI 和别名最终解析值。 |
| 权限、路径、工具 | OpenCode `permission` 有工具级及路径/命令规则，并有 `external_directory/doom_loop`；规则最后匹配者生效。[权限文档](https://opencode.ai/docs/permissions/) Claude Code 有 `allow/ask/deny`，并区分工具名与输入匹配。[权限文档](https://code.claude.com/docs/en/permissions) | Sprig 已有独立执行的只读、工具禁用、路径排除、取消、输出限制和逐次批准。可增加更细的“拒绝”策略，但不可照搬自动批准。普通命令审批与文件限制不构成 OS 沙箱。 |
| 子进程环境 | Codex 可从 core/none 环境开始，再应用值和过滤器。[环境规则](https://developers.openai.com/codex/config-advanced/#shell-environment-policy) | Sprig 已有最小环境、额外变量名白名单、可信字面值和模型密钥剥离，不缺基础配置。任何扩展仍不能恢复模型凭据。 |
| 上下文和压缩 | OpenCode 有 `compaction.auto/prune/reserved`。[压缩配置](https://opencode.ai/docs/config/#compaction) Gemini 有 `model.compressionThreshold`。[配置参考](https://geminicli.com/docs/reference/configuration/#model) Aider 有历史软 token 上限及 repo-map token/刷新设置。[选项参考](https://aider.chat/docs/config/options.html) | Sprig 已有实际序列化请求字节上限与确定性旧工具记录省略。当前压缩主要是布尔开关，保护末尾六条消息；缺可解释的触发阈值、保护窗口和省略类别设置。不能把 bytes 改名为模型 token 窗口，也不能将省略等同于经过实测的长期语义摘要。 |
| 指令、语言和 skills | OpenCode `instructions` 接受路径/glob；skill 使用单独 `SKILL.md`、按需加载并有访问规则。[规则配置](https://opencode.ai/docs/config/#instructions)、[skills](https://opencode.ai/docs/skills/) Claude skills 也区分按需正文与始终加载指令。[skills 文档](https://code.claude.com/docs/en/skills) | Sprig 已有分层 AGENTS.md、明确附加文件、回复语言、规则变化检查。缺 skill 发现/加载工具和扩展执行后端；加入 `instructions.files` 不等于实现 skill。可先做受限本地 Markdown 按需加载，禁止借 frontmatter 自动执行命令。 |
| 会话和存储 | Gemini 提供 `general.sessionRetention.enabled/maxAge/maxCount/minRetention`。[配置参考](https://geminicli.com/docs/reference/configuration/#general) Codex 有 `history.persistence/history.max_bytes`。[配置参考](https://developers.openai.com/codex/config-reference/) | Sprig 已有私有原子会话、恢复绑定、可选自动保存、独立会话/备份目录与手动删除。仍缺用户可配置的保留期限、数量或总占用策略。不可用自动删除破坏当前会话/备份或绕过批准；先做预览和明确批准清理更符合现有边界。 |
| TUI 和终端体验 | OpenCode 有 `/details`、`/thinking`、外部编辑器、`scroll_speed/diff_style`；`/thinking` 只控制展示，不启停推理。[TUI 文档](https://opencode.ai/docs/tui/) Gemini 有 `ui.compactToolOutput`。[配置参考](https://geminicli.com/docs/reference/configuration/#ui) | Sprig 已有 Enter 发送、中途引导、主题、侧栏、高度和按动作快捷键。当前追加输出总是滚到底部；更直接的缺口是可关闭自动滚动、紧凑工具展示、独立思考展示偏好。展示设置不得改变真实请求、批准或工具结果。 |
| 通知 | Gemini 支持完成/需操作事件、终端通知方法。[配置参考](https://geminicli.com/docs/reference/configuration/#general) Codex 有 `tui.notifications/notification_method/notification_condition`。[配置参考](https://developers.openai.com/codex/config-reference/) | Sprig 暂无通知后端。可先支持默认关闭的终端 bell，并限定完成/需批准等状态，不发送提示词、文件名或命令到外部进程。Windows Terminal 与 POSIX 终端需分别核验，不能只接受布尔值。 |
| 检查、格式化、Git | Aider 提供按语言 `lint-cmd`、`auto-lint`、`test-cmd/auto-test`。[检查选项](https://aider.chat/docs/config/options.html#fixing-and-committing) | Sprig 已有固定 checks、可选任务前基线、完成后独立验收、批准后执行以及编辑撤销/备份。仍缺按语言 formatter/linter 集成；任何自动触发仍必须进入批准、环境、取消和超时边界。 |
| MCP 和 hooks | OpenCode MCP 有本地 command/environment、远程 URL/headers/OAuth、启停/超时及工具控制。[MCP 文档](https://opencode.ai/docs/mcp-servers/) Gemini hooks 目前执行 command，文档明确它们以用户权限执行任意代码。[hooks 文档](https://geminicli.com/docs/hooks/) | Sprig 尚无 MCP 客户端与 hooks 生命周期。其配置必须与连接、进程、工具发现、超时、凭据隔离、私有存储和批准实现一起交付。不能把普通 `run` 命令或一个 TOML 空表算作支持。 |

## 按收益和实现边界排序

以下为本次调查的工程判断，不是样本的性能结论，也不是当前已接受的 Sprig 配置字段。

1. **先完成现有设置的可靠性与可解释性。** 保持单一 TOML、Schema、未知字段拒绝、字段来源、离线 doctor、无需密钥的本地模型目录和私有初始化模板。它们已经存在，应完善回归与错误说明，不重新发明平行配置文件。每一项设置都要在运行时、CLI/TUI、Schema、示例、profile/项目层和恢复绑定中一致生效。
2. **优先扩展既有压缩后端。** 设置保护最近多少条消息/完整工具轮次、何时开始省略，以及是否允许省略成功的旧输出/获批编辑文本。保留用户要求、拒绝、失败、未完成工具调用和最新计划；省略仅影响待发请求，保存历史不改写。阈值沿用实际 bytes 度量，并显示压缩前后数值。固定请求预算的测试不能为了新字段膨胀而提高预算。
3. **补齐终端日常操作。** 允许暂停自动滚动以查看历史、压缩工具显示、控制思考块显示。默认 Enter 发送、换行组合键与批准键保留稳定。紧凑显示只影响用户看到的布局，模型仍收到经过同一边界校验的工具结果。
4. **提供安全的存储维护。** 先增加有界只读占用/年龄清单及“清理预览→逐次批准→重查文件版本/租约→删除”的操作，再考虑显式开启保留策略。项目文件不得设置私有存储目录或触发清理；活动租约、重解析点、无关文件和审批期间变化必须阻止删除。
5. **随后交付扩展后端，而不是空壳开关。** MCP、按需 skills、hooks、PTY、操作系统沙箱、LSP/formatter 应各自作为完整功能批次。MCP 的 `readOnlyHint` 等 annotations 默认不可信，不能凭服务端自称只读就绕过本地只读或批准；协议也要求显示输入、限制调用和处理超时。[MCP Tools 规范](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)。

提供商/模型自动回退、自动重试付费请求、全局任意环境替换、项目 `.env` 自动加载、凭据获取命令、TLS 验证关闭和工具自动批准，不应为追求选项数量而加入。本地模型列表与 `/models` 元数据也不能证明工具调用、流式、推理能力或价格有效。

## 后续验收要求

支持一个配置项至少要证明：合法值改变真实行为，无效值启动即拒绝；覆盖和列表合并结果有来源；项目不能扩大信任；命令/写入仍需新批准；恢复会话不能携带旧授权；日志/公开配置不泄露凭据。涉及终端、路径、进程、私有存储的变化，需要相应原生 Windows 与 POSIX runner 的结果。研究笔记和回环协议测试本身不能证明编码能力达到这些样本。

已有 Sprig 使用方法和可复制字段以[配置文档](configuration.md)、[配置示例](../config.example.toml)、[Schema](../config.schema.json)为准。本笔记中的后续设计不应在实现前作为已支持字段写进示例。

## 本轮落实的范围

调查后接入 `[context].max_bytes/compact/trigger_ratio/keep_recent_messages`，复用已有确定性压缩；原 `[agent]` 写法保持兼容。同层新分组优先，用户、profile、显式项目与 CLI 的来源可查看。保护窗口至少六条，压缩后仍检查实际请求字节，不把省略等同模型摘要。

接入 `[storage].session_max_age_days/session_max_count` 与 CLI/TUI 清理入口；`--prune-sessions --dry-run`、`/prune --dry-run` 预览，真实清理逐项批准并复查版本。它们不会在启动/保存时自动删除，不清理活动会话或修改备份；项目文件不能设置存储策略。实现与约束见[配置说明](configuration.md)。

同时完成读取版本标识的独立 `edit_lines` 工具和继承的编辑禁用策略，见[读取绑定编辑](read-bound-edits.md)。上述后端可以接受真实设置；自动滚动、独立思考展示、MCP、hooks、其他原生模型协议和 OS 沙箱仍属后续差距，不宣称已支持。功能验证与模型编码表现分别报告。
