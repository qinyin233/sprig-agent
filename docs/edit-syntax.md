# 编辑提案的本地语法反馈

获批的 `edit_file` 和 `edit_lines` 写入成功后，会向模型和终端显示支持语言的解析观察。它让明显语法问题更早进入修复循环；固定的任务前、阶段和最终检查仍按原有设置运行，每条命令仍需新批准。

覆盖 Python、JavaScript、TypeScript、TSX、Rust、Go、C、C++ 和 Java，扩展名沿用 `project_map`。Python 使用当前解释器的 `ast.parse`，其余使用已有固定版本的 tree-sitter 语法。无新配置、依赖、模型工具或外部请求。

## 返回与显示

成功修改的工具结果新增 `syntax`：

```json
{
  "language": "python",
  "parser": "python_ast",
  "before": {"status": "parsed"},
  "after": {
    "status": "parse_error",
    "locations": [{"kind": "parse_error", "line": 3, "column": 12}]
  },
  "sha256": "提案实际 UTF-8 字节的 SHA-256",
  "not_acceptance": true,
  "python_version": "当前解释器版本"
}
```

`parsed` 仅表示该解析器没有报告解析错误；`parse_error` 表示发现问题；新建文件的 `before.status` 为 `absent`。位置使用从 1 开始的物理行号和 Unicode 字符列，忽略开头 BOM，并把 CRLF/CR 统一看作换行；列不是终端可视宽度。tree-sitter 最多返回三处非重叠错误节点/缺失 token 的起点，`limit_reached=true` 表示达到报告上限，不保证已列出所有问题或精确根因。

解析限额、栈限制和解析器不可用返回 `unavailable`，不伪造 `parsed`，也不覆盖已获批修改的真实 `approved/changed`。完整报告不能放进现有输出预算时，返回 `omitted/output_limit`；连这个标记也放不下时省略 `syntax`。不支持的文件类型也没有该字段，缺少字段不能视为解析成功。

CLI 和 TUI 共用 `[syntax; parser observations, not acceptance]` 输出。诊断不包含源码、语法异常正文、绝对路径或 AST，保存历史时诊断部分仅保留上述元数据；编辑提案和备份仍按原有会话/备份设置处理。`sha256` 绑定批准提案字节；文件此后可能变化，报告不是当前读取版本或新的编辑授权，后续仍应读取当前内容。

## 安全与实际限制

解析发生在批准之后、写入之前，不运行目标代码，不执行 import、formatter、测试或语言服务器。取消检查、路径/指令复核和原字节/文件身份复查仍在实际写入之前；解析期间外部修改不会被旧提案覆盖。拒绝、只读、无变化、预检和不支持的类型不会进入解析。每次修复仍走新的批准。

输入最多 512 KiB；tree-sitter 沿用现有一秒原生解析期限，遍历和待处理节点均有数量上限。Python AST 受输入/栈限制，取消在解析前后检查，没有可强制打断 Python 解析器的独立计时器。临时源码和树不进入索引缓存。

Python AST 可能接受编译器拒绝的代码，例如顶层 `return`；宿主版本与目标语言版本也可能不同。tree-sitter 的错误恢复和语法版本同样不能代表编译器、类型检查或运行测试。`not_acceptance=true` 明确保留这一边界；语法反馈不能替代行为验收，上一轮 Click 的 640/642 结果没有因此变成完成。

## 验证

十项新增回归覆盖全部已安装语法、BOM/CRLF/Unicode 列、缺失 token、报告/遍历/输入上限、不可用解析器、无源码执行、取消/拒绝/只读/外部改动、输出省略，以及三种真实回环 HTTP/SSE 协议的错误→修复→独立批准检查。另验证解析成功仍不能覆盖失败的固定验收。

80×24 实际 Textual 测试以 Enter 发送，经两次文件批准和一次独立检查批准完成修复；用户界面和后续模型都看到解析错误。最初发现工具结果未显示诊断后，已修复公共输出链路并重新验证。完整本地范围见[机器记录](benchmarks/edit-syntax-20261023.json)。无新增外部模型成绩，远端 CI 仍按冻结要求待验证。
