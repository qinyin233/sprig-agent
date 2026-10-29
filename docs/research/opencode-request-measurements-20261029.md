# OpenCode 请求用量源码与 Sprig 的退出统计缺口

2026-10-29 继续读取原有只读副本的 `dev@9d90d892dcbfc52b47e95b2ea31b8aba877128fd`。以下先记录实施前的源码依据与约束，后续实现见末节；没有运行 OpenCode 或新增外部模型测试。固定版本范围沿用[最初源码调研](opencode-source-reading-20261020.md)。

[`session/usage.ts`](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/usage.ts)将供应商用量拆为输入、可见输出、推理、缓存读取和缓存写入，再按模型价表计算成本；缺失和非有限值在这一归一化入口落为零。成本是由价表计算的值，源码另有实际计费金额契约尚未接入的 TODO，不能把它解释成账单核对结果。

[`runner/publish-llm-event.ts`](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/runner/publish-llm-event.ts)在 step-finish 结算步骤的原生结束原因与 token，并拒绝重复结算；[`session/projector.ts`](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/session/projector.ts)把用量累加到会话数据库，再发布会话用量。由调用关系可知，它把步骤结算和会话累计分开；这不是仅在退出时打印当前进程计数。没有执行上游测试，因此不声称其任意故障下都不丢统计。

实施前的 Sprig 已在 `metrics.py` 提供允许列表数值、128 条最近请求和累计统计，缺失 token 明确保持未知；三协议供应商适配、SSE 和错误归因也已接入。当时 `Agent.usage_report()` 只返回当前 Provider 的内存状态，`SessionStore` 未保存请求统计，CLI/TUI 恢复也不恢复旧用量。[Click 中断实测](../live-evaluation-click-interrupted-20261029.md)中七个完整回复仍在，但前段 HTTP、token、时长统计缺失，临时测量 wrapper 不能算产品已支持持久统计。这个缺口的后续实现见末节。

下一批应沿用显式私有会话存储，不为查看统计发模型请求。持久状态需要区分请求开始、完整结算和无法确认，按本地身份去重；未知值不得转成零。保存失败时不能继续执行尚未可靠保存的工具意图，硬退出留下的开始记录也不能假装已结算，更不能自动重发请求。

跨进程恢复、模型切换和历史分支还需要明确统计归属：原会话事实保留，新分支不能在汇总中被当作新消费再次累加；缺少旧统计的历史需标注覆盖缺口。只保存严格校验、有界的数值与固定状态，拒绝任意字符串、URL、请求头、提示、工具参数及原生推理，不把统计放入模型上下文或当作操作批准、文件版本、验收通过或实际账单。

验收应覆盖三协议 JSON/SSE、失败响应、用量缺失、重复保存、分支/模型切换、保存不确定与真实进程中断恢复；CLI/TUI 仍须 Enter 发送、中途引导和逐次审批。现有正确的行尾与搜索范围拒绝保持，统计持久化也不等同于真实编码完成率提高。CI 按既有冻结规则处理。

## 后续实施

接入[会话请求统计](../request-usage.md)：沿用现有显式私有保存，为每次尝试保存开始/结算状态，恢复未结算项为未知；原生 Provider 数值与会话累计分列，分支继承快照单列，不把它计作新消费。`--session-usage NAME` 可以离线检查数值，无需选择原模型，不输出历史或改写原文件。保留 Sprig 对未知 token 的表示，没有复制上游缺失值归零或价表费用估算。

三协议 JSON/SSE、真实进程被终止后的恢复、保存失败/同步不确定、旧会话缺口及 80×24 TUI 有独立覆盖；完整最终范围、首轮旧 checkpoint 次数断言的失败与修正见[本批记录](../benchmarks/request-usage-20261029.json)。该补齐未建立模型编码能力排名，也没有实现 OS 沙箱或改变逐次审批规则。
