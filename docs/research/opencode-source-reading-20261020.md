# OpenCode 源码阅读：先补齐 Sprig 的基础能力

阅读日期：2026-10-20。本文记录源码事实、测试断言和适用于 Sprig 的实现取舍，不把功能数量当作实际编码能力的证明。

阅读结论：Sprig 应先完善任务状态与失败恢复、TUI 上下文操作、长会话延续和工具结果读取，再扩张扩展系统。现有的 Enter 发送、中途引导、三协议、版本关联编辑、逐次批准、会话锁和独立最终检查已经有实现，后续工作应在这些路径上补齐体验与验证。具体对照见第 12 节，实施批次和验收条件见第 13 节。

## 1. 取样与证据范围

| 项目 | 本次固定值 |
| --- | --- |
| 官方仓库 | <https://github.com/anomalyco/opencode> |
| 默认分支 | `dev` |
| 固定提交 | `9d90d892dcbfc52b47e95b2ea31b8aba877128fd` |
| Sprig 对照基线 | `0aa8afce7144bd05ef824e35b4bce57eccccb1ae`，源码调研提交只修改文档，后续实施另记 |
| 本地只读研究副本 | `C:\Users\25079\AppData\Local\Temp\sprig-opencode-source-20261020-c465e7f4` |
| 获取方式 | `git clone --depth 1 --filter=blob:none`，随后记录 HEAD 与 `origin/HEAD` |
| 仓库声明版本 | `2.0.32`；包管理器 `bun@1.4.2` |
| 许可证 | MIT；若后续直接复制代码，必须保留版权与许可声明 |
| 验证方式 | 阅读连接起来的实现与测试体；未安装、构建、运行 OpenCode，未调用任何模型 |

版本信息来自 [package.json](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/package.json#L1-L10)，许可来自 [LICENSE](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/LICENSE#L1-L21)。这是固定提交的开发分支快照，不代表所有已发布版本，更不代表默认分支未来状态。

此前 Sprig 客户端基准安装的 OpenCode `1.18.34` 与本次开发分支不同；本次没有运行那份客户端，也没有逐项回溯稳定版。尤其不能把当前目录中未确认的功能写成“所有 OpenCode 版本都没有”。

本文用“源码事实”表示直接读到的路径与分支，用“推论”表示由调用关系得出的判断，用“建议”表示 Sprig 的选择。测试只说明上游写了什么可执行断言；本次没有执行这些测试，因此不报告“上游测试通过”。

## 2. 当前架构已经改变，不能沿用旧路径猜实现

本次快照没有 `packages/opencode`。核心运行时在 `packages/core`，协议适配在 `packages/ai`，界面在 `packages/tui`，HTTP 服务在 `packages/server`，另有 `schema`、`protocol`、`client`、`cli`。旧版 `processor.ts`、旧 `packages/opencode/src/session/...` 的经验不能直接当作当前实现证据。仓库还明确约束 Schema/Core/Protocol/Server/Client 的依赖方向。[依赖约束](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/AGENTS.md#L1-L4)

一次正常交互可以沿这条链阅读：

```text
终端输入 / HTTP 请求
  → Session.prompt：规范化附件与 hook，持久接收输入
  → session_inbox：尚未递送的 steer / queue / control
  → SessionExecution：进程内按 Session ID 协调 drain
  → SessionRunner：边界递送输入、重新装载上下文、压缩、准备一次请求
  → LLMClient / Protocol / Route：协议编码、传输、语义事件
  → SessionStep：发布流事件、执行已完成的工具调用、持久结算
  → 再装载历史，决定下一 step 或空闲
```

HTTP handler 调用 Core Session 服务，自己处理协议错误映射；它不是另一套模型循环。[HTTP Session handler](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/server/src/handlers/session.ts#L32-L55)；运行接口与实际 drain 分别见 [runner 接口](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/runner/index.ts#L21-L39)、[runner 主循环](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/runner/llm.ts#L56-L192)。

**对 Sprig 的启发：**值得先明确“输入是否已接收”“何时对模型可见”“哪次执行持有会话”“工具是否结束”这些状态。它们不要求立即改成 HTTP 服务或 Effect 框架。照搬多包组织不能自动改善模型解题能力。

## 3. 输入、执行与中途引导是分开的状态机

### 3.1 先接收，再递送

`SessionPrompt.prepare` 默认把新请求设为 `steer`，允许 hook 调整输入与 metadata；文件在入队前具体化，技能附件在此解析。存储有独立 `session_inbox` 表，保留 payload、delivery、入队序号；可见消息另有 session 内唯一序号。[输入准备](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/prompt.ts#L29-L90)；[消息与 inbox 表](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/sql.ts#L79-L144)

测试体明确断言：`resume: false` 的 prompt 已在 inbox，却尚未出现在可见消息中；相同 ID 被换成另一份正文重试时，第一次接收的正文仍然有效，入队记录仍只有一条。这是接口幂等语义，不是靠客户端禁止连点。[先入队测试](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/test/session-prompt.test.ts#L233-L253)；[第一次接收获胜测试](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/test/session-prompt.test.ts#L679-L725)

### 3.2 在安全边界递送，而不是随时拼接上下文

Runner 在 step 边界重新选择上下文，并区分当前继续执行时的 `steer` 与空闲边界的普通队列输入。`compaction`、`move` 是显式控制项；递送新的输入会重置 step allowance。一次 step 的重试重新加载上下文，但不会再次递送新输入。[边界推进](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/runner/llm.ts#L73-L179)；[请求与重试准备](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/runner/llm.ts#L203-L241)

这说明“中途继续输入”并非一个输入框选项。需要同时定义输入顺序、取消、排队、递送和剩余步数；否则容易出现 UI 已显示接收、模型却没看到，或旧响应在引导后仍落地的问题。这里是设计推论，不能由此认定 Sprig 当前引导不正确。

### 3.3 同会话协调的边界

`SessionExecution` 按 Session ID 路由到对应 Location 的 runner，协调器是**进程内**的。启动时先记录 write-ahead claim；终止、失败、用户取消释放 claim，服务关闭保留它供重启恢复。它不提供集群 owner fencing 或外部副作用 exactly-once。[执行与 claim](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/execution.ts#L85-L156)

协调测试实际断言同 key 的并发 resume 只调用一次 drain，等待者被取消不会取消共享执行，而不同 key 可以同时运行。[同会话合并](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/test/session-run-coordinator.test.ts#L10-L27)；[等待者取消与不同会话并发](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/test/session-run-coordinator.test.ts#L504-L541)

## 4. 流式协议：统一语义，保留差异

### 4.1 协议与供应商部署分离

`Protocol` 负责统一 `LLMRequest` 到原生请求体的转换、Schema 验证、响应事件状态机；URL、认证、headers、framing 属于 Route/Endpoint/Auth。Chat Completions、Responses、Messages 可以被多个兼容供应商复用，而不是为每家供应商复制一套解析器。[Protocol 接口与职责](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/ai/src/route/protocol.ts#L4-L67)

Chat 解析器保留 provider metadata，处理兼容服务省略 tool index 的情形：优先显式 index/已知 ID，再按 batch position 或最近调用回退。收到 `length` 或 `content-filter` 不确认待执行工具；缺少 id/name 也有独立错误分支。[Chat 工具组装](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/ai/src/protocols/openai-chat.ts#L1096-L1187)

这类容错适合用真实录制或合成协议 fixture 验证；不能把“更宽松”直接当成“更正确”。Sprig 应继续限定可接受变体，并对身份冲突、截断和非文本 arguments 拒绝执行。

### 4.2 展示流、完整工具输入与完整响应不是同一件事

`SessionStep` 消费统一事件。只有语义 `tool-call` 事件会启动本地工具，先发布调用事件再 fork 工具 fiber；原始 argument delta 不直接触发工具。Step 在流结束后等待工具结算，再记录快照、usage、finish 或 failure。[事件与工具启动](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/runner/step.ts#L91-L153)；[step 结算](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/runner/step.ts#L227-L283)

需要保留协议细节：Chat 的 `finishAll` 结果先进入状态，在 `finishEvents/onHalt` 输出，且受 `requireFinishReason` 控制；不能简单描述成“OpenCode 一边收到参数一边执行 shell”。也不能把所有协议都等同于 Sprig 的“整个响应验证完后才执行工具”。[Chat 最终事件](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/ai/src/protocols/openai-chat.ts#L1193-L1234)

测试体断言：不完整 JSON 与非法 escape 产生 `tool-input-error`，不生成可执行 `tool-call`；一个 batch 中无效调用不会消灭已经完整的另一调用。[工具输入测试](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/ai/test/tool-stream.test.ts#L129-L203)

**Sprig 的选择：**继续保留当前较严格的整响应屏障。学习统一事件与分类错误，不为了降低延迟而放松副作用前置验证。

### 4.3 传输恢复有递送语义与降级状态

OpenCode 的 Session WebSocket transport 将递送分为 `send-attempted`、`provider-observed`、`terminal`，会标记 `not-sent`、`accepted`、`ambiguous`；连接绑定 URL/headers affinity，改变认证环境会重连。连接默认 55 分钟轮换，连续五次流失败转为 HTTP。[传输状态](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/model-transport.ts#L22-L57)；[关闭与递送标记](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/model-transport.ts#L116-L205)

这是面向多种传输的基础设施，不意味着 Sprig 现在需要 WebSocket。更小的可借鉴点是错误类型应告诉用户“发送前失败 / 已开始输出 / 递送未知”，而不能笼统自动重试一个可能收费或已产生副作用的请求。

## 5. 上下文压缩是一次有成本、有失败结果的操作

### 5.1 预算不是只看字符数

OpenCode 默认开启自动 compaction，保留最近约 15,000 token；触发上限预留 context/input window 的 15%，大窗口至少保留 16,000。预算估计先找**相同 provider** 的最近有效 usage anchor，再估计新增消息、工具 schema、媒体；模型限制未知时不会主动触发自动压缩。[默认与触发](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/compaction.ts#L198-L235)；[预算算法](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/compaction.ts#L812-L871)

这仍是估计，源码也注明图片密集会话可能估高。它不替代 Sprig 的 UTF-8 请求字节硬限制，两者约束对象不同。增加 token 预算时应保留字节、附件与响应上限。

### 5.2 摘要加原文尾部，避免只留下模型概述

摘要模板要求 objective、requirements、decisions、work state、next move、relevant files、important context，明确保留用户范围和未完成事项；最多列 15 个继续工作所需文件，并要求不要执行任务。规则不要求把 AGENTS.md 和环境设置反复塞进摘要，因为它们独立重装载。[摘要模板和规则](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/compaction.ts#L107-L178)

尾部选择以 user message 边界对齐，避免在一次工具交互中间切开。文本摘要保留 `keepID`，并重建保留尾部的文本；不能据此声称完整保留原来的富消息与媒体结构。原生 compaction 则保存 provider 产生的 replacement window；它与文本摘要不同，并受原生内容可重放范围限制。[尾部切分](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/compaction.ts#L714-L759)；[原生 compaction](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/compaction.ts#L334-L383)

摘要没有任何预期标题时追加一次格式修正请求；`filled()` 实际只要求出现至少一个预期标题，并不校验所有字段齐全或事实正确。输出达到 token 限制、内容过滤或未知 finish 会失败，不把截断响应作为成功摘要。每次结束的压缩请求立即记录花费，失败与中断也可能付费。[格式检查与修正](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/compaction.ts#L276-L315)；[失败分支](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/compaction.ts#L552-L579)；[成本记录](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/compaction.ts#L628-L638)

### 5.3 超长输入要有渐进降级与停止点

当 provider 拒绝长度时，压缩请求按首个被拒绝请求估计量的 70%、50%、35% 缩小；payload too large 会先剥离媒体改成文本。降级仍不能丢掉最新 exchange/checkpoint 时停止并报错。摘要信息损失和额外模型费用不能被 UI 的“正在压缩”掩盖。[缩小策略与停止条件](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/compaction.ts#L386-L455)

相关测试体断言保留工作状态标题、工具媒体只留下描述不把 base64 塞入摘要、Unicode 截断不切开 surrogate pair，并禁止摘要继续执行任务。[内容与 Unicode 测试](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/test/session-compaction.test.ts#L99-L138)；[摘要格式测试](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/test/session-compaction.test.ts#L210-L235)

**对 Sprig 的建议：**先把当前确定性上下文裁剪的“保留哪些内容、丢弃多少、是否还能继续”展示清楚，再考虑可选的摘要 checkpoint。不能静默增加付费摘要调用；完整历史、用户原话、失败检查和工具因果关系要可恢复。

## 6. 权限设计可学习，但不能照搬默认授权

OpenCode 权限按 action/resource 通配匹配，最后匹配的规则生效，未匹配默认 `ask`。先处理配置中的 deny，再合并项目保存的 allow，随后触发 permission evaluation hook。[规则解析](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/permission.ts#L87-L101)；[合并与 hook](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/permission.ts#L148-L188)

`assert` 在 `allow` 时直接返回，`ask` 时等待 Deferred；关闭 Location 会拒绝未处理请求。`always` 可把资源规则持久化到项目，并重新评估其他待批请求。拒绝附带 feedback 与单纯拒绝不同：前者可以作为纠正反馈交给模型继续，后者使 step 停止。[关闭待批请求](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/permission.ts#L130-L145)；[等待批准](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/permission.ts#L235-L265)；[拒绝与持久授权](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/permission.ts#L269-L325)

**安全取舍：**`ask/allow/deny` 是授权机制，不能据此声称任意 shell 已被操作系统沙箱隔离。Sprig 用户要求每次写文件和执行命令都新鲜显式批准，因此不应照搬 `always` 自动放行。可借鉴的部分是请求来源、结构化资源、取消清理、拒绝理由与明确的待批状态。

## 7. 持久会话与崩溃恢复要承认副作用不确定性

Session 表有 claim 时间和恢复次数；`SessionRestart` 先持久增加计数，默认最多恢复十次，避免跨重启死循环。它明确声明恢复是 **at-least-once**：进程内协调不能防止崩溃后外部副作用重复。托管服务需要先确认旧进程死亡，非注册服务不负责扫描这些 claim。[数据库字段](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/sql.ts#L60-L75)；[恢复边界与次数](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/execution/restart.ts#L23-L98)

恢复后台 shell 时，旧状态若仍是 running，会被报告为“服务重启导致取消”，而不是重新执行命令；完成通知被接收后再确认标记。后台通知与恢复 execution 的唤醒分开，避免空闲会话被通知无意复活。[shell 恢复](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/execution/restart.ts#L101-L135)

测试体验证启动时已有 claim、正常完成释放、关闭保留；用户取消释放 claim，使下次启动不复活；超恢复预算写入失败事件并清零计数。[执行生命周期测试](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/test/session-execution.test.ts#L75-L142)；[恢复预算测试](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/test/session-execution.test.ts#L291-L345)

**Sprig 的选择：**恢复历史与恢复待执行命令分开。继续保留“不重放未批准、未结算副作用”的原则；借鉴显式 interrupted/unknown/complete 状态和有界恢复，而不是自动重跑旧工具。

## 8. 配置是分层、解释与热更新，不只是文件格式

### 8.1 格式、来源和优先级

当前读取 `opencode.json` / `opencode.jsonc`，发现系统、全局、显式路径、项目直达文件、`.opencode`、`.claude`、`.agents` 等来源。Windows 的系统目录使用 `%ProgramData%\opencode`，不是把 POSIX 路径硬搬过来。[配置发现](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/config/discovery.ts#L12-L31)；[来源发现](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/config/discovery.ts#L59-L144)

配置解析包含 JSONC 诊断、规范化、Schema 验证，未知字段按明确策略处理。实际加载顺序是 managed policies、well-known、global、explicit、direct、project supplementary、content、managed settings；policy 又有独立权威顺序，不能总结成“项目配置永远覆盖用户配置”。更新通过 watcher 与各领域插件重建状态。[解析](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/config.ts#L108-L148)；[加载顺序和 watcher](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/config.ts#L220-L310)；[policy 的独立顺序](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/config/plugin/policy.ts#L25-L47)

环境变量 `{env:VAR}` 和 `{file:path}` 插值会针对 Windows 路径做 JSON escaping；缺失 env/file 可退为空值。这是 OpenCode 的行为，不是 Sprig 应无条件采用的策略，关键模型凭据缺失时 Sprig 仍应给出可操作诊断。[变量展开](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/config/variable.ts#L25-L80)

### 8.2 Provider、model 与 variant 分开

provider 配置负责 canonical alias、package、compatibility、settings、headers、body；model 可以覆盖能力、limits、cost 和启用状态，variant 另有 settings/headers/body overlay。不是所有设置都作为不受控字典直接传给 SDK。[provider fold](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/config/plugin/provider.ts#L54-L103)；[model 与 variant fold](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/config/plugin/provider.ts#L105-L168)

对 Sprig 而言，应先验证三种主流协议、配置优先级、无密钥诊断、配置来源解释和安全选项信任边界，不需要复制整个供应商目录。预算/能力字段只有真正影响请求、附件校验、工具列表或 UI 才算落实。

## 9. 附件、大文本与扩展的实现边界

### 9.1 附件是快照，不是活文件引用

Prompt 入队前读取 `file:` 或 `data:`，检查单附件 20 MiB 限制，嗅探 MIME，图片交给 image service 规范化，文件内容转为持久附件。目录变成排序后的目录列表。`start/end` 仅对读入后识别为 text/plain 的内容切片；原文件大小先通过上限检查，不是任意大文件的按行流式读取。[附件具体化](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/prompt.ts#L93-L148)；[文件、目录、范围与上限](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/prompt.ts#L151-L203)

因此“输入框可附文件”与“长期会话始终读取最新文件”不同。Sprig 应分别表述附件快照、活文件引用、工具分段读取，并同时说明磁盘大小、序列化字节和模型 context 限制。

模型 `read` 的文本读取另外采用 2,000 行/50 KiB 分页边界，单个长行只显示前 2,000 个 JavaScript 字符单元并加截断标记；按下一行继续并不能取回该行余下正文。Sprig 已有带版本游标的 UTF-8 字节分页，应保留这种可续读路径。[读取限额](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/tool/read-filesystem.ts#L14-L21)；[分页与单行截断](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/tool/read-filesystem.ts#L298-L327)

### 9.2 Skills 与 hooks 是有生命周期的服务

技能扫描最多递归六层，遇到 `SKILL.md` 就把该目录视为技能根停止下探；读取技能工具前执行权限检查。选中技能作为 prompt 附件与模型主动调用 skill tool 是两条不同入口。[扫描](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/config/plugin/skill-scan.ts#L7-L45)；[skill 工具](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/tool/plugin/skill.ts#L32-L67)

Hook 分为 session、permission、shell、tool、aisdk 等 domain；注册绑定 Scope，卸载时移除；调用按注册顺序等待，可按 provider 过滤。这意味着扩展代码处在运行时信任边界内，不只是给模型增加文字。[Hook 注册与执行](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/plugin/hooks.ts#L13-L58)；[生命周期](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/plugin/hooks.ts#L63-L104)

### 9.3 MCP 不只枚举 tool schema

MCP 是 Location 范围服务，管理 server 状态、连接、工具、prompts、resources/templates、server instructions 和 OAuth integration；状态区分 disabled/needs_auth/failed，接口支持显式连接与断开。配置插件合并全局与 per-server timeout，并订阅配置更新后 reload。[MCP 服务边界](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/mcp/index.ts#L24-L93)；[工具与资源接口](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/mcp/index.ts#L113-L175)；[MCP 配置](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/config/plugin/mcp.ts#L17-L56)

本轮只追到服务接口、连接状态与配置注册，未完整审计每种 transport/OAuth 路径，不据此给 MCP 安全性评级。Sprig 当前没有 MCP 后端；后续接入时应先覆盖断连、超时、取消、输出限制、资源读取与权限边界，再扩张服务器数量。

## 10. 编辑、命令和撤销如何反馈给下一步

### 10.1 编辑匹配容错与批准后变化是两个问题

当前 `edit` 先在 LF 视图精确查找；没有精确命中时依次尝试排版引号/横线/空格归一化、逐行尾空格容错。多个命中默认失败，显式 `replaceAll` 才批量替换。它把匹配位置映射回原文，用行对齐保留 BOM、CRLF 和混合换行中未改动部分。[匹配与换行处理](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/tool/plugin/edit.ts#L48-L193)

执行链是路径解析及外部目录授权 → 读取 → 匹配并生成预览 → `permission.assert` → 写入 → formatter → 返回最终 diff。路径互斥锁串行化同进程中合作的修改，但注释明确外部写入仍可竞态；测试甚至明确断言匹配后外部内容改变时仍应用原提案。[编辑执行链](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/tool/plugin/edit.ts#L244-L320)；[锁的边界](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/file-mutation.ts#L61-L104)；[外部变化测试](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/test/tool-edit.test.ts#L779-L800)

**Sprig 的选择：**保留唯一精确替换、`read_id` 版本关联整行编辑，以及批准后再次检查原字节/文件身份。可以改进失败定位和重新读取提示，但不让模糊匹配悄悄决定写入位置。formatter 是额外命令和额外修改；后续接入时必须进入 Sprig 的新批准、取消、备份和检查流程，不能以批准编辑代替批准 formatter。上游 formatter 会执行程序并返回格式化后的内容，不能把它等同于无副作用的语法检查。[formatter 执行](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/formatter.ts#L62-L104)

### 10.2 长命令有身份、日志和完成通知

`shell` 接收命令字符串，解析后分别检查外部目录和命令资源权限，批准后重新检查 cwd；默认前台超时 120 秒。它创建 Shell/Job 身份，可以显式后台执行；完成后向会话写 synthetic 通知，携带完成/取消/失败和退出信息，避免靠模型不断轮询。[命令准备](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/tool/plugin/shell.ts#L118-L157)；[任务与通知](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/tool/plugin/shell.ts#L159-L274)

通用工具默认以 2,000 行/50 KiB 作为正文截断阈值，超出时把完整文本保存到 `tool-output`，返回预览和文件路径，保留期七天。Shell 有自己的完整输出文件和尾部预览，退出码与 capture 是否仍存在分开报告。这里的 50 KiB 是返回预览上限，不能当作子进程总输出或磁盘日志配额；本轮没有完整审计其磁盘配额。[通用输出保存](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/tool-output.ts#L14-L127)；[Shell 尾部结果](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/shell.ts#L239-L262)

测试检查有界预览确实能对应完整文件，并保留混合内容顺序；不能只检查输出里出现了“truncated”。[输出测试体](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/test/tool-output.test.ts#L39-L104)

**Sprig 的缺口：**已有实时输出、首尾展示、独立总量上限、超时/取消和进程后代清理；没有完整输出 artifact 的受控再读取，也没有后台任务/交互式 stdin。补日志时需要私有目录、总磁盘配额、清理和脱敏，不得把普通文件工具的工作区限制改成任意读取外部路径。

### 10.3 Step 快照可包含命令造成的工作区改动

虽然 edit leaf 仍有快照集成 TODO，实际 runner 已在 step 开始/结束捕获 Snapshot，记录变更文件；Session revert 使用消息边界后的快照构造需恢复的路径。不能仅凭 leaf 的 TODO 就写成“快照未实现”。[step 捕获与结算](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/runner/step.ts#L77-L94)；[变更文件记录](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/runner/step.ts#L227-L260)；[revert 计划](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/revert.ts#L85-L113)

Snapshot 在 Git 工作区默认启用，使用单独 Git 元数据保存内容寻址树，按 Location 限定范围，未跟踪单文件上限 2 MiB；关闭、不支持或捕获失败可返回 undefined。它不是整个文件系统、网络、数据库或任意命令副作用的撤销保证。[快照范围与捕获](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/snapshot.ts#L97-L158)

后台 Shell 可在返回后继续执行，因而之后的写入未必落在启动它的 step 快照区间内，也不保证能完全归因或回滚。[后台返回](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/tool/plugin/shell.ts#L254-L270)

Sprig 当前有文件工具备份、批准的 undo/redo、未结算动作阻断；命令造成的改动尚未纳入这份历史。后续消息级回滚应先定义外部改动冲突、部分恢复、忽略文件和隐私，而不是把 Git reset 接到撤销按钮。

## 11. TUI 输入、补全和大粘贴的实际路径

### 11.1 Enter 与排队有不同入口

默认 `input.submit` 是 Enter，换行支持 Shift/Ctrl/Alt+Enter 和 Ctrl+J；`prompt.submit` 本身没有另设默认快捷键，由输入控件提交路径调用。普通 `submit()` 默认 delivery=`steer`，单独 `prompt.queue` 才提交 queue。不要把配置中的 `prompt.submit=none` 误读成“没有 Enter 发送”。[键位](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/tui/src/config/keybind.ts#L197-L211)；[提交入口](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/tui/src/component/prompt/index.tsx#L1096-L1163)；[控件回调](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/tui/src/component/prompt/index.tsx#L1775-L1805)

提交有防重入 guard；异步创建会话/发送前先拍下输入快照并清空 composer，后续输入不会被旧回调清掉。请求失败时只在输入框仍空时恢复原草稿，避免覆盖用户新打的文字；乐观显示与服务端接收失败的回滚由 data layer 配合。[草稿快照](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/tui/src/component/prompt/index.tsx#L1180-L1220)；[乐观提交](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/tui/src/component/prompt/index.tsx#L1359-L1398)

Sprig 已有 Enter、换行、单 worker、FIFO 引导、批准窗内改要求、预算拒绝保留草稿，以及“旧模型响应不落地”的回归。缺的是独立 queue 产品语义与队列管理；不能将引导数量上限误称为完整任务队列。

### 11.2 补全、快照附件与活引用应共用上下文规则

自动补全组件处理 `@` 文件/agent/skill、slash 命令和目录参数；文件候选通过 client 的 find 接口异步查询，limit=20，选中后形成带 mention 范围的附件元数据，可包含行范围。它不是简单把候选路径拼到正文里。[补全与附件构造](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/tui/src/component/prompt/autocomplete.tsx#L246-L382)

Sprig 已有 `/add` 快照和 `/ref` 请求前刷新；应给这些现有后端增加统一选择入口、补全与状态说明，再扩大上下文类型。选中路径不等于已上传正文，较大文件只有路径/大小时需要明确显示；候选枚举同样必须遵守排除规则，不能让补全暴露模型文件工具原本看不到的路径。

### 11.3 折叠大粘贴只改变显示

粘贴清理 ANSI 并规范化 CRLF/CR。默认三行及以上或超过 150 字符的内容以 extmark 占位显示，提交时展开保存的正文；这有助于编辑与回看，但不减少发给模型的上下文。折叠路径先 `trim()` 首尾空白，因此不是字节保真还原；Sprig 不应无条件移植这一行为。Windows ConPTY 的 CR 换行在粘贴边界单独处理。[粘贴显示](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/tui/src/component/prompt/index.tsx#L1445-L1484)；[提交时展开](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/tui/src/component/prompt/index.tsx#L1128-L1140)

本地图片/PDF 路径粘贴有最多 32 个路径、累计 20 MiB 的读取边界；http(s) 路径不在该入口自动抓取。SVG 作为文本，其他受支持附件编码为 data URI。这和通用远程文件传输不是同一能力。[本地附件读取](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/tui/src/component/prompt/local-attachment.ts#L1-L96)；[路径数量与格式](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/tui/src/component/prompt/local-attachment.ts#L117-L174)

上游还提供 `$VISUAL/$EDITOR` 外部编辑器，暂停并恢复终端；它会创建临时文件、启动进程。本功能对 Sprig 需要独立批准和私有临时文件设计，不能只复制启动逻辑。[外部编辑器](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/tui/src/editor.ts#L13-L43)

实际测试体包含模拟终端粘贴控制字符、过滤 escape sequences，以及 shell 请求失败后恢复输入框。这些是后续 Sprig 应复用的验收维度，不是本轮运行成功的结论。[终端粘贴测试](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/tui/test/prompt-paste.test.tsx#L5-L16)；[草稿恢复测试](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/tui/test/prompt-shell-failure.test.tsx#L59-L76)

## 12. Sprig 实现对照：已有、部分和缺失

以下仅以第 1 节的 Sprig SHA 为准。“已有”表示读到实现及相应测试体，不把历史测试结果说成本轮重新通过。OpenCode 机制的固定链接在前述各节；Sprig 代码也固定到同一基线，避免以后文件变化使本次结论漂移。

| 范围 | Sprig 代码与具体行为 | 判定及缺口 |
| --- | --- | --- |
| 执行循环 | [Agent.run](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/agent.py#L403-L530)：保存工具意图后执行，逐结果 checkpoint，固定检查失败返回模型，受 max_steps 约束 | 已有；缺统一类型事件/运行记录，部分 UI 状态仍依赖文本前缀；无需为了分层先重写为服务端 |
| 中途引导 | [Agent._steer](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/agent.py#L238-L252)、[TUI 输入](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/tui.py#L663-L724)：FIFO，在安全边界递送，旧响应弃用，未执行调用补 skipped 结果 | 已有；独立 queue、编辑/取消队列和待递送输入的持久状态缺失；内存 pending 队列不是持久 inbox |
| 三种协议与流式 | [Provider](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/provider.py#L196-L283)、[native adapters](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/native_protocols.py#L127-L202)：Chat/Messages/Responses，完整响应校验，原生状态回传 | 已有；继续做兼容 fixture 与错误归因；精确 token 能力限制、总请求期限与空闲超时分离仍欠缺，不需要逐个测付费模型 |
| 配置 | [Settings.resolve](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/settings.py#L348-L497)：TOML、provider/model/profile、来源解释，项目层只能收紧安全策略 | 已有；缺热更新和 TUI 内安全切模型；任何新入口需保持端点/协议/会话绑定 |
| 项目指令 | [ProjectInstructions](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/instructions.py#L1-L95)：有界 AGENTS.md/显式规则，动作前和批准后检查 | 已有；规则不能授予执行权限；不自动执行工作区扩展 |
| 文件理解与读取 | [Tools 读取/搜索](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/tools.py#L270-L460)、[地图](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/tools.py#L589-L690)：版本、行/字节分页、命名声明与语法调用线索 | 已有；没有语义 LSP。上游当前 read 会截长行，不能用其 2,000 字符规则替换 Sprig 可续读字节分页 |
| 编辑 | [edit_lines/edit_file](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/tools.py#L783-L919)、[失败定位](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/edit_match.py#L14-L72)：版本绑定、唯一精确匹配、审批后复查、备份 | 已有；不要重复新增“版本编辑”；先优化失败后的有效重读/修复路径，formatter 尚无专用后端 |
| 自动检查 | [Agent._verify](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/agent.py#L350-L401)：可选任务前/编辑后固定 argv、独立最终检查，每条都新批准 | 已有；项目检查配置引导和真实复杂任务通过证据不足，模型写“完成”不算验收 |
| 长会话 | [prepare_context](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/context.py#L217-L438)：精确请求字节、确定性裁剪、最新失败/最近消息保护、旧读取窗口与闭合只读批次折叠 | 部分；没有模型摘要 checkpoint、模型窗口 token 估计和完整长任务状态重建；保存历史仍有 8 MiB 上限 |
| 快照/活文件引用 | [Agent 附件](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/agent.py#L106-L171)、[引用准备](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/references.py#L1-L167)：/add 快照、/ref 小文件刷新、大文件路径/大小 | 部分；缺 @ 文件选择、可视粘贴折叠、图片/PDF；快照和活引用已有，无须重造附件文字协议 |
| 命令与输出 | [Tools.run](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/tools.py#L921-L1031)、[Windows runner](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/windows.py#L326-L442)：argv、审批、有界实时输出、超时/取消、进程组/Job Object | 部分；缺完整日志 artifact、后台 job/PTY/stdin；获批命令仍没有 OS 沙箱 |
| 会话恢复 | [SessionStore](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/session.py#L96-L280)：私有可选保存、OS 锁、原子替换、配置绑定，不重放待执行动作 | 已有；缺消息级分支/时间线、持久待递送队列；不复制自动重放副作用的恢复策略 |
| 撤销与隔离 | [ChangeHistory](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/changes.py#L119-L180)、[worktree_action](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/worktrees.py#L137-L181)：文件备份、冲突拒绝、显式创建 detached worktree | 部分；命令改动快照、消息级回滚、worktree 合并/冲突处理未实现；工作树不是 OS 沙箱 |
| 扩展与外部信息 | 当前 [九个工具定义](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/src/sprig_agent/tools.py#L49-L94) 不含 MCP/skills/hooks、网页或 question 工具 | 缺失；可文字提问/获批命令不等于对应产品后端，先完成内置流程再接入 |

代表性的 Sprig 测试体已检查以下行为：

| 现有验收维度 | 代码证据 |
| --- | --- |
| Enter/换行、流式途中新要求、旧工具不执行且只有一个 worker | [test_tui.py](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/tests/test_tui.py#L730-L792) |
| 读取版本改变、审批期间外部改动拒绝覆盖 | [test_read_versions.py](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/tests/test_read_versions.py#L79-L94) |
| 非完整原生流无副作用、三协议多轮 read/edit/独立检查 | [test_native_protocols.py](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/tests/test_native_protocols.py#L206-L247)、[多轮](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/tests/test_native_protocols.py#L302-L329) |
| 活引用请求前刷新、请求预算失败保留草稿且不重复任务 | [test_references.py](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/tests/test_references.py#L444-L491) |
| 编辑后失败回模型修复，再独立批准最终检查 | [test_edit_checks.py](https://github.com/qinyin233/sprig-agent/blob/0aa8afce7144bd05ef824e35b4bce57eccccb1ae/tests/test_edit_checks.py#L52-L88) |

真实编码表现仍按现有[能力盘点](../development-readiness.md)和[实测记录](../performance.md)报告；本次源码研究没有产生新的通过率、速度或成本成绩。

## 13. 先完成哪些已有能力

下面是建议批次，**均未由本次文档更新实现**。前两批先完善现有执行与输入路径；不要求先搭建 Server/数据库/MCP，也不要求扩大付费模型测试。

| 顺序 | 实施范围与目的 | 完成时必须展示的验收 |
| --- | --- | --- |
| P0-A：执行状态和失败恢复 | 让已有循环提供有界类型事件，区分已接收、待递送、模型处理中、待批准、工具运行、未验证/检查通过/失败/取消；CLI/TUI 共用同一状态。把步骤、工具调用、失败原因和剩余预算对应起来，减少依赖显示文本推断状态 | 一次真实 UI 的固定回复回放走完读取→过期编辑拒绝→新读取→新批准→检查失败→修复→独立最终检查；流中、批准中、命令中分别插入引导/取消，无过时写入、重复动作或假完成。保存失败/退出时准确显示哪些输入仅在内存、哪些动作结果未知 |
| P0-B：编辑和检查反馈 | 沿用 read_id、整行/精确编辑与固定检查，把可修复错误的原因、当前范围和下一种有效动作说清楚；明确检查配置与基线/阶段/最终检查的差别 | 用 Unicode/CRLF、同名声明、重复片段、行边界、外部改动和不同失败退出的回放证明可以修正提案；拒绝/取消不能触发换工具绕过或自动执行；不改验收测试来制造通过 |
| P1-A：现有 TUI 上下文入口 | 为 /add、/ref 和本地 slash 命令补 @/命令补全、快照/活引用状态、大粘贴可展开显示；继续 Enter 发送和执行中引导 | 中文/空格/Windows 路径、终端粘贴 CRLF、删除占位、光标编辑、补全时 Enter、重复发送、网络/预算失败时的新旧草稿均覆盖；展开后的完整请求按原字节硬限制预检，候选不暴露排除路径，不因显示折叠声称减少 token |
| P1-B：长会话延续 | 先验证现有确定性裁剪在连续任务中保留要求、最新失败与待办；再设计显式可选的结构化摘要 checkpoint，摘要与原历史关联、原生协议状态分开处理，token 估计明确标注 | 缺字段、输出截断、取消、超预算、切协议/模型、恢复、嵌套工具结果有失败回退与原文回查；摘要仍可能语义失真，不宣称能自动判断任意事实一致性，不能保存授权或成为 read_id/检查通过证据；额外模型请求和费用在启用前可见，默认不静默调用 |
| P1-C：可重新读取的工具日志 | 为已有命令输出建立私有、有总量上限的 artifact，返回截断说明和受控读取句柄；先完成前台日志，再设计后台 Job/完成通知 | 日志总磁盘预算、逐页输出、Unicode、跨块秘密、取消/崩溃/过期清理、会话隔离与排除路径有测试；只读日志能力不能扩大任意路径访问；“没有保留输出”与“命令失败”分开显示 |
| P2：扩展及较大工作流 | 在前述路径稳定后，再分别接入 question、MCP、skills/hooks、后台任务、消息级回滚和语义工具；每项先定义运行时与权限边界 | 有后端、可审查操作、断连/取消/恢复与隐私证据后才对外标已支持；不先堆配置项，不直接复制持久自动授权或崩溃后重放副作用 |

每批先用 uv 锁定环境和离线 HTTP/模型 fixture，走正常 CLI/TUI；功能提交按 AGENTS.md 跑完整 unittest 与相关检查。Windows 行为改变时要有原生 Windows，POSIX 行为改变时要有对应 runner；平台 skip 不算通过。小更新使用 `[skip ci]` 并明确无该 SHA 的远程 CI，积累成大批更新再运行对应 CI、等待精确 SHA 全部必需作业成功。源码调研提交仅文档，不触发 CI。

### 后续实施记录

2026-10-20 第一批实现 [P0-A 的运行阶段观察](../runtime-status.md)：运行层直接提供不可变事件和最近快照，接入 CLI/TUI，消除依赖模型文本前缀识别运行阶段；覆盖真实 HTTP/TUI 的过期编辑、修复和独立检查链，以及批准/取消/保存失败边界。原表是调研基线，这条记录不把整项 P0-A 或后续批次标为完成。持久待递送队列、独立任务队列、长期摘要和完整事件存储仍未实现。

同日下一批实现 [P1-A 的命令与文件选择入口](../tui-completion.md)：本地命令菜单、`/add` / `/ref` 参数补全、有界模糊文件选择，接到现有快照/活引用后端；保留主输入 Enter 发送。查询变化使旧候选立即失效，候选不读正文，实际选择再次检查隐私。它不等于已完成内联 `@`、多选和大粘贴折叠，亦不改变本研究的历史基线表。

后续小批次补充[内联本轮文件](../tui-mentions.md)：采用带范围的明确选择元数据，编辑标记撤销选择；文件正文在发送时准备，只用于本次运行，不作为附件跨轮次保存。保留既有 `/add` 和 `/ref`，没有复制上游的全部附件能力。光标、撤销、失败草稿和三协议请求预算有离线回归；多选弹窗和大粘贴折叠仍未完成。历史基线表保持研究时状态，当前进度以产品文档及机器记录为准。

随后实现[长文本粘贴](../tui-paste.md)：重读固定源码的 `pasteInputText` 后保留范围元数据和提交展开思想，选择三行或超过 200 字符自动折叠，保留缩进与首尾空白，不复制上游 `trim()`。F4/按钮展开、多块与文件选择、撤销/重做、失败草稿和三协议实际预算有回归。它只改变输入显示，不能节省模型 token；图片/PDF 和多选弹窗仍缺，历史研究表不改写为新能力声明。

继续重读 `compaction.ts` 的 `SUMMARY_TEMPLATE`、`SUMMARY_RULES` 和 `buildPrompt` 后实现[显式结构化摘要检查点](../summary-checkpoints.md)：采用原文前缀绑定与完整尾部，默认不自动增加付费请求。Sprig 要求全部字段、完整响应、实际净缩小和新确认，原历史保持不变，另提供受限原文分页；不采用“至少一个标题就通过”的宽松判断。语义失真仍可能发生，原生 token 窗口、自动压缩策略与真实长任务表现仍待验证，不能将这一批视为 P1-B 全部完成。

随后重读 `model-transport.ts` 的连接期限、流空闲期限和取消清理：上游分别限制 WebSocket 建连与帧等待，并在取消/不完整交换时清理通道。Sprig 据此补充自己的[HTTP 请求生命周期](../request-lifecycle.md)，为三协议共用总时限、空闲上限和可轮询取消，保留逐次请求与完整响应校验；这不表示支持上游 WebSocket 传输。同步 DNS 仍无法强制中断，返回后必须复查以拒绝迟到连接；不把未知交付的失败自动重试。实际 HTTP/TLS/CONNECT 和 TUI 测试覆盖相应边界，平台与精确 CI 结果另列。

真实模型验证在实现与离线闭环之后按预算另行安排。USTC 仍仅使用 `deepseek-flash` / `deepseek-flash-2`；三种协议的兼容性不需要对所有付费模型逐一消费。本次没有新增模型调用或“已追平顶尖 agent”的结论。

## 14. 阅读清单与尚未验证的事项

本次沿调用关系阅读的实现文件（部分为目标行段，不表示逐行审计全部代码）：

- 根 `AGENTS.md`、`package.json`、`LICENSE`。
- `packages/server/src/handlers/session.ts`。
- `packages/core/src/session/{prompt,sql,execution,compaction,model-transport}.ts`、`execution/restart.ts`、`runner/{index,llm,step}.ts`。
- `packages/ai/src/route/protocol.ts`、`protocols/openai-chat.ts`。
- `packages/core/src/permission.ts`、`config.ts`、`config/{discovery,variable}.ts`、`config/plugin/{provider,policy,mcp,skill-scan}.ts`。
- `packages/core/src/plugin/hooks.ts`、`tool/plugin/{skill,opencode}.ts`、`instructions/builtins.ts`、`mcp/index.ts`。
- 测试体：`packages/ai/test/tool-stream.test.ts`，`packages/core/test/{session-prompt,session-run-coordinator,session-execution,session-compaction}.test.ts`；并检索相邻 compaction、runner retry、route/response 测试定位覆盖面。
- `packages/core/src/tool/plugin/{edit,shell,read,question}.ts`、`file-mutation.ts`、`formatter.ts`、`tool-output.ts`、`tool/read-filesystem.ts`、`shell.ts`、`shell/result.ts`、`snapshot.ts`、`session/revert.ts`；测试体 `tool-edit.test.ts`、`tool-output.test.ts` 及 `tool-shell.test.ts` 的拒绝、输出和平台分支。
- `packages/tui/src/component/prompt/{index,autocomplete,local-attachment}.tsx/ts`、`config/keybind.ts`、`editor.ts`；测试体 `prompt-paste.test.tsx`、`prompt-shell-failure.test.tsx` 及 `prompt/autocomplete.test.ts` 的目录补全范围。
- Sprig 对照：`agent.py`、`tools.py`、`edit_match.py`、`line_edits.py`、`context.py`、`context_folding.py`、`settings.py`、`config.py`、`session.py`、`changes.py`、`worktrees.py`、`policy.py`、`tui.py`，以及第 12 节列出的测试体。

未运行 upstream 程序、测试或 CI；未测首 token 延迟、真实仓库成功率、token/费用、长会话信息保真度；没有把兼容适配数量转换成“性能领先”结论。Native Windows/POSIX 的具体执行行为必须由真实 runner 验证，静态阅读和平台分支存在均不等于测试通过。

本轮没有复制上游实现或引入其依赖。建议与已实现项明确分开，后续按第 13 节逐批落实，并在实际交付后回填提交和验证结果。
