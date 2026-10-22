# 默认正则的内容检索

更新：2026-10-22。模型使用 `grep(pattern, path=".", literal=false, ...)`，默认按 RE2 正则解释表达式。例如 `pattern="NoSuchCommand|NoSuchOption"` 查找任意一类异常，`literal=true` 则搜索包含竖线的原文。`glob`、`ignore_case`、`context`、`limit` 和 `output_mode` 延用既有搜索参数；不静默修正表达式、扩大范围或自动补一次搜索。

```json
{"pattern":"^def (?:load|save)_item\\(","path":"src","glob":"*.py","context":1}
```

`output_mode` 可选 `content`、`files`、`count`；后两种要求 `context=0`。结果仍包含覆盖范围、跳过、截断和超时信息。搜索不能提供写入的读取标识，修改前要通过 `read_file` 取得当前版本并重新批准。

旧会话和嵌入调用的 `search(query, regex=false, ...)` 仍按字面值搜索，显式 `regex=true` 才启用正则。只向新模型请求公布 `grep`，不把同一搜索工具的两种入口同时加入工具列表。默认非交互任务仍为九种模型工具，全开最多十七种；配置接受十八个名称，含兼容入口。

`[tools] search=false`、`grep=false` 或相应 `--disable-tool` 任一个均同时禁用两者；另一入口的 true 不能抵消合并策略中仍存在的 false。受限项目配置不能通过另一名称重新开启用户/profile 禁用的检索。生效设置来源会显示别名禁用的原因。文件排除作用于整个文件读取层，关闭单个工具不等于禁止其他读取工具或隔离获批命令。

执行复用现有本地 RE2 和文件扫描器：完整受限文件隐私校验、工作区句柄、取消、512 KiB 单文件、8 MiB 总读取、5000 条目、32 层目录，以及序列化输出限制均保留。RE2 不支持 lookaround、回溯引用或跨物理 CR/LF 行匹配；1 MiB 引擎预算与协作式 1 秒工作预算的精确定义见[正则搜索](regex-search.md)。只读搜索不请求批准，文件写入和命令仍逐次批准；未修改原生 OS 后端。

来源为固定 OpenCode `dev@9d90d892dcbfc52b47e95b2ea31b8aba877128fd` 的 [grep 输入实现](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/tool/plugin/grep.ts)，借鉴明确的 `pattern` 与 `literal` 语义；Sprig 继续使用自己的受限扫描器和审批策略。源码阅读范围见[调研文档](research/opencode-source-reading-20261020.md)。

这次调整由[真实 Click 失败](live-evaluation-20261022.md)驱动。原会话六个正则形式的字面搜索均为零命中，既有提示并未使模型纠正参数。只读离线重放将这些公开查询显式改为 `grep`，分别得到 8、8、2、3、2、3 个命中，读取文件数与旧入口相同且没有截断/超时/跳过，原候选保持不变，见[数值记录](benchmarks/grep-replay-20261022.json)。随后[真实模型复测](live-evaluation-grep-20261022.md)采用了 9 次正则 grep，8 次完成搜索，其中 6 次有命中；仍未编辑、仍为 635/642。接口语义与实际调用已验证，编码成功率或费用收益没有得到证明。

回归覆盖默认正则与显式字面模式、旧入口、输出模式/过滤/限额、隐私、取消、已关闭资源、双名称禁用和项目来源，以及三协议 HTTP/SSE 恢复旧历史→搜索→版本读取→批准编辑→单独固定验收；历史折叠与数值诊断同时识别两种名称。远程 CI 按用户要求暂停到 2026-11-01，之后仍先确认账户和旧 gate。

最终本地测试、平台跳过、安装包与源码摘要见[验证记录](benchmarks/grep-20261022.json)。工具 schema 为 4880 字节，保留既有小上下文预算；新接口没有增加模型工具数量或请求额度。
