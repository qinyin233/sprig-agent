# 2026-10-19 源码上下文保留调研

本次只核对官方文档和固定提交源码，没有运行这些 agent、调用模型或比较修复成功率。提交通过公开 Git remote 的 `HEAD` 获取，再按 SHA 读取源码；下文是本次观察的开发分支快照，不代表已发布版本。Pi 的旧仓库地址 `badlogic/pi-mono` 当前重定向到 `earendil-works/pi`。

| 项目与快照 | 已确认的选择、保留和触发机制 |
| --- | --- |
| Aider `5dc9490bb35f9729ef2c95d00a19ccd30c26339c` | 源文件与历史分开构造：每轮 `format_messages()` 分别加入旧对话、repo map、只读文件、当前选中文件正文和当前对话。map 用定义/引用图的 PageRank 排序，当前对话提及的文件、标识符及已选文件影响权重；默认 map 预算 1024 token，无已选文件时可扩大。旧 `done_messages` 超过模型的历史预算才启动模型摘要；摘要尝试保留较新的尾部。见[消息构造与触发](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/base_coder.py)、[排序与预算实现](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py)、[历史摘要实现](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/history.py)。 |
| OpenCode `afc68054c3b39cbc0d580a92184e00e2b8a97ae9` | 当前 v2 默认 `auto=true, keep=15000`。输入估计达到模型限额减缓冲时触发；默认缓冲取窗口 10%，较大窗口至少留 16000 token，未知窗口不自动触发。普通摘要保留较新消息的文本部分，优先以用户 exchange 边界切分，让调用和结果保持在一起；工具正文序列化时限制为 1250 字符。较旧部分送模型摘要，也支持提供商原生压缩。见[当前实现](https://github.com/anomalyco/opencode/blob/afc68054c3b39cbc0d580a92184e00e2b8a97ae9/packages/core/src/session/compaction.ts)、[后续请求投影](https://github.com/anomalyco/opencode/blob/afc68054c3b39cbc0d580a92184e00e2b8a97ae9/packages/core/src/session/runner/to-llm-message.ts)。 |
| Pi `1402973a9dbe8c627341e04c9744c54953e285ca` | 默认窗口余量 16384 token、近期保留 20000 token。超阈值或手动 `/compact` 触发；从后向前计数，切点不落在工具结果上，较旧部分生成模型摘要，原始历史仍保存。摘要还累积读取/修改文件列表；该列表从工具调用参数提取，不是文件正文或执行成功证明。送摘要的工具结果截到 2000 字符。见[压缩算法](https://github.com/earendil-works/pi/blob/1402973a9dbe8c627341e04c9744c54953e285ca/packages/coding-agent/src/core/compaction/compaction.ts)、[文件列表与序列化](https://github.com/earendil-works/pi/blob/1402973a9dbe8c627341e04c9744c54953e285ca/packages/coding-agent/src/core/compaction/utils.ts)、[官方说明](https://github.com/earendil-works/pi/blob/1402973a9dbe8c627341e04c9744c54953e285ca/packages/coding-agent/docs/compaction.md)。 |

版本边界：网上常见 OpenCode `PRUNE_PROTECT=40000`、`PRUNE_MINIMUM=20000` 的说明对应另一套实现路径，不能据此描述本次读取的 v2。三者的 token 预算也不能直接换算成 Sprig 的序列化请求字节上限。

调研时 Sprig 基线为 `91edc5a81a99022544a7de3495baba791119cf9b`，证据是[续跑记录](live-evaluation-resume-20261018.md)与 [prepare_context](https://github.com/qinyin233/sprig-agent/blob/91edc5a81a99022544a7de3495baba791119cf9b/src/sprig_agent/context.py)：同一候选连续十次请求仍只有读取和搜索，未编辑；解除预算阻碍后，仍没有产生修复。当时压缩保留最近消息、失败和拒绝，但会省略更早的成功读取正文。**推断**：关键的唯一完整源码窗口可能随消息老化被移除，导致模型重新取证；这值得验证，但既有轨迹不能证明它是停滞原因。Aider 的做法提示“当前代码上下文”和“对话历史”可以采取不同保留策略；不说明其策略在 Sprig 上一定更好。

建议先做不增加模型请求的确定性改进：

1. 在硬字节限额内，优先保留少量最近的完整源码读取结果，而不是统一按消息年龄省略。仅选成功、来源匹配、内容完整且未标记省略的结果；原始调用与结果位置保持不变。候选不放得下就回退到现有省略，不提高预算，不自动补读。
2. 在唯一窗口之前淘汰确实重复的旧读取；不能仅凭同一路径判断重复，不同版本、不同覆盖范围和截断读取不能互相替代。保留的旧正文仍是历史观察；编辑前继续执行现有版本校验。正文省略后不保留可误当当前编辑证据的 read ID。
3. 用户要求、拒绝、最新失败、近期保护窗口、批准/取消/输出受限状态和隐私脱敏优先于上述保留候选；策略只改变发送上下文，不改写保存历史，不执行历史命令或复用批准。模型摘要若将来加入，额外请求、用量和失败必须计入同一个总预算，不能作为本轮离线改进的隐形开销。

离线验收应固定历史、工具 schema、模型设置和请求上限，重放压缩前后的真实编码请求：报告保留的完整窗口/不同文件数、覆盖范围、实际字节和拒绝发送情况；同时逐项核对保护消息与原会话文件完全不变。再用回环 HTTP 检查下一请求确实收到完整正文，修改与检查仍分别获得新批准。可以证明保留策略及预算行为，不能把字节变化换算成已节省的模型 token、费用或真实编码成功率。

本调研没有审计各项目所有插件、权限实现或完整生命周期，也没有运行它们的测试。推荐方案是 Sprig 的待验证设计，不是对其他项目能力缺失或 Sprig 性能接近它们的结论。
