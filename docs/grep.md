# 默认正则的内容检索

更新：2026-10-22。模型使用 `grep(pattern, path=".", literal=false, ...)`，默认按 RE2 正则解释表达式。例如 `pattern="NoSuchCommand|NoSuchOption"` 查找任意一类异常，`literal=true` 则搜索包含竖线的原文。`glob`、`ignore_case`、`context`、`limit` 和 `output_mode` 延用既有搜索参数；不静默修正表达式、扩大范围或自动补一次搜索。

```json
{"pattern":"^def (?:load|save)_item\\(","path":"src","glob":"*.py","context":1}
```

`output_mode` 可选 `content`、`files`、`count`、`read` 和 `windows`。文件与计数模式要求 `context=0`。普通检索返回覆盖、跳过、截断和超时信息。要修改普通检索命中的文件，先通过 `read_file` 取得当前版本，再提交差异供审批。

## 一次查看多个文件的命中窗口

`windows` 默认从每个命中文件返回首个匹配及其上下文。`matches_per_file` 可设为 1 至 8，在同一次检索中查看一个文件里的多个定义或调用点。相邻及重叠的源码窗口会合并。`path` 和 `glob` 可缩小范围，`limit` 控制文件数量，最多八个。

```json
{"pattern":"^class NoSuchOption\\(|^\\s+def resolve_command\\(","path":"src/click","glob":"*.py","output_mode":"windows","context":3,"matches_per_file":3}
```

每条 `files` 记录返回完整编号窗口以及适用时的 `read_id`。这两个结果来自同一份文件内容，可直接用版本标识提议 `edit_lines`。各文件独立取样和审批。单文件记录最多 4096 字节，也受总输出预算限制。多个匹配放不下时，返回能放下的最长完整前缀；首个窗口也放不下时，返回路径、命中行和 `window_omitted=true`。外层 `truncated` 表示检索范围或总输出受限，单文件内的 `truncated` 表示还有匹配。若有 `next_cursor`，切换到该文件的 `read` 模式，沿用查询及匹配选项，省略 `glob` 和 `matches_per_file`。这两种源码模式均要求启用 `read_file`。

在[固定 Click 初始快照](repository-task-benchmark.md)中，以 `NoSuchOption` 的公开导出、定义、抛出和 `resolve_command` 的定义为锚点，比较 `files` 后逐文件 `read(limit=1)` 与一次 `windows`，上下文均为前后三行。两边得到四个完全相同的源码窗口和版本标识。工具调用从 5 次降为 1 次，完整文件读取从 21 次降为 17 次，工具结果 JSON 从 2828 字节降为 2530 字节。快照在回放前后通过同一摘要校验。这些是离线工具指标，真实模型完成率仍需实测。

## 在单个文件内搜索并读取

当文件路径已知时，`read` 模式一次完成匹配和编号源码读取。

```json
{"pattern":"NoSuchCommand|NoSuchOption","path":"src/example.py","output_mode":"read","context":4,"limit":10}
```

此模式要求启用 `read_file`，只接受一个明确的工作区文件路径。它使用同一份完整校验的 UTF-8 快照匹配并读取，合并相邻和重叠窗口，保留原行号和换行字节。每次最多返回八段、合计 10000 行，`context` 仍为 0 至 10。`limit` 限制用于生成窗口的命中行数。命中计数 `matched_lines` 只统计这些行，窗口内还可能包含其他命中。

命中超过条数、窗口数量或输出字节预算时，返回能放入预算的完整窗口前缀，并以 `truncated=true` 标记还有匹配未处理。成功结果的 `range_complete=true` 表示本页的窗口完整。单个匹配窗口仍放不下时，减少 `context` 后重新搜索，很长的单行可走字节分页。

有 `next_cursor` 时，把它作为 `cursor` 传入同一个搜索。它绑定工作区、文件路径与内容、模式及匹配选项，`limit` 可以调整。文件内容变更后，从 `cursor="start"` 或省略游标重新搜索。后续页面从首个未处理的匹配继续，前后文仍可能重叠。每页都完整复查文件大小和隐私策略。未命中、凭据需遮盖或匹配超时都没有可用的 `read_id` 或续页标识。

成功返回的 `read_id` 可直接用于同一文件的 `edit_lines`。写入仍展示完整差异、需要新批准，并复查文件版本和策略。历史压缩可保留单文件或跨文件搜索的完整源码窗口。明确继续任务时，Sprig 可为内容仍一致的窗口发放新标识。 [源码保留与恢复](read-retention.md)

在固定 Click 初始快照的 `src/click/core.py` 上，用同一正则定位六个 `invoke` 或 `resolve_command` 声明。普通 grep 和随后一次编号读取共返回 4699 字节 JSON。单次搜索读取返回 2163 字节，包含相同的四段源码和相同文件版本。该离线测量只统计工具结果，不包括模型请求、token 或任务完成率。[固定快照](repository-task-benchmark.md)

当指定路径不存在时，检索返回 `error`，并可在 `suggested_directory` 提供已确认的最近上级目录，工作区根目录表示为 `.`。可用 `list_files` 查看该目录，再显式选择检索路径。排除规则、敏感路径检查和输出大小限制同样约束此提示。

旧会话和嵌入调用的 `search(query, regex=false, ...)` 仍按字面值搜索，显式 `regex=true` 才启用正则。新模型请求只公布 `grep`，配置同时接受两个名称。

`[tools] search=false`、`grep=false` 或相应 `--disable-tool` 任一个均同时禁用两者；另一入口的 true 不能抵消合并策略中仍存在的 false。受限项目配置不能通过另一名称重新开启用户/profile 禁用的检索。生效设置来源会显示别名禁用的原因。文件排除作用于整个文件读取层，关闭单个工具不等于禁止其他读取工具或隔离获批命令。

检索复用现有本地 RE2 和文件扫描器。各模式都进行完整文件的隐私校验，保留工作区路径限制、取消、512 KiB 单文件上限和序列化输出限制。目录检索另外受 8 MiB 总读取、5000 条目和 32 层目录限制。RE2 不支持 lookaround、回溯引用或跨物理 CR/LF 行匹配。1 MiB 引擎预算与协作式 1 秒工作预算的精确定义见[正则搜索](regex-search.md)。只读搜索无需批准，文件写入和命令仍需逐次批准。

来源为固定 OpenCode `dev@9d90d892dcbfc52b47e95b2ea31b8aba877128fd` 的 [grep 输入实现](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/tool/plugin/grep.ts)，借鉴明确的 `pattern` 与 `literal` 语义；Sprig 继续使用自己的受限扫描器和审批策略。源码阅读范围见[调研文档](research/opencode-source-reading-20261020.md)。

这次调整由[真实 Click 失败](live-evaluation-20261022.md)驱动。原会话六个正则形式的字面搜索均为零命中，既有提示并未使模型纠正参数。只读离线重放将这些公开查询显式改为 `grep`，分别得到 8、8、2、3、2、3 个命中，读取文件数与旧入口相同且没有截断/超时/跳过，原候选保持不变，见[数值记录](benchmarks/grep-replay-20261022.json)。随后[真实模型复测](live-evaluation-grep-20261022.md)采用了 9 次正则 grep，8 次完成搜索，其中 6 次有命中；仍未编辑、仍为 635/642。接口语义与实际调用已验证，编码成功率或费用收益没有得到证明。

回归覆盖默认正则与显式字面模式、旧入口、输出模式/过滤/限额、隐私、取消、已关闭资源、双名称禁用和项目来源，以及三协议 HTTP/SSE 恢复旧历史→搜索→版本读取→批准编辑→单独固定验收；历史折叠与数值诊断同时识别两种名称。远程 CI 按用户要求暂停到 2026-11-01，之后仍先确认账户和旧 gate。

最终本地测试、平台跳过、安装包与源码摘要见[验证记录](benchmarks/grep-20261022.json)。工具 schema 为 4880 字节，保留既有小上下文预算；新接口没有增加模型工具数量或请求额度。
