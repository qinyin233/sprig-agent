# OpenCode 补丁准备、匹配与执行

2026-10-29，读取既有固定副本 `9d90d892dcbfc52b47e95b2ea31b8aba877128fd` 的三个补丁文件，并逐一与提交 blob 在归一 CRLF 后核对。副本其他路径有未检出的文件，本文只依据核对过的文件，不声称整个工作目录干净。Sprig 基线为 `19e71408c450ed54334d1df2b1a6f9c3f08d0aa0`。没有执行上游、调用模型或触发 CI。

OpenCode `Patch.parse` 接受 `*** Begin Patch`、Add/Delete/Update 与可选 Move 等自定义指令。工具先解析和准备各目标的候选内容，再组合文件差异请求 edit 权限；批准后依次写入/删除。一次多个文件不构成跨文件原子事务，失败可保留已完成的前缀。[解析](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/util/src/patch.ts#L49-L126)、[准备与批准](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/tool/plugin/patch.ts#L150-L208)、[执行](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/tool/plugin/patch.ts#L210-L257)。测试文件 `packages/core/test/tool-patch.test.ts` 也已核对身份；未运行其断言。

`derive` 分离 BOM，按 LF 拆开文本、应用 replacements，并给结果补尾部 LF。`computeReplacements` 从当前位置继续寻找上下文；`seek` 依次使用精确、去尾部空白、trim、字符规范化比较，普通分支还逐位置搜索。它有自己的容错语义，不能假定不会移动匹配或归一文字。[派生结果](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/util/src/patch.ts#L128-L140)、[匹配](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/util/src/patch.ts#L311-L374)。这是固定版本源码事实，不是漏洞复现或其他版本结论。

Sprig 已有路径限制、完整版本关联的 `edit_lines` 和逐文件批准/备份。这里的实现选择是在该入口提供标准编号 unified hunks：由既有 `path` 定唯一文件，严格验证坐标、上下文、数量、EOF 与换行，转换为原有整行替换。保留全部现有副作用门禁；文件路由仍走既有工具。统一 hunk 格式可以避免发送整段未改动的源码，但何时更紧凑取决于修改分布，不据此宣称普遍 token 或模型成绩收益。

这一取舍保留 Sprig 的精确匹配与 Windows/POSIX 文本约定。遇到偏移、混合换行或不完整提案时明确拒绝，等待重新读取/提案；不照搬上游的多级宽松搜索。最终功能与验证范围见[使用说明](../line-patches.md)。
