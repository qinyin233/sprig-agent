# 精确编辑失败后的定位

`edit_file` 的旧文本必须在原文件中精确且唯一地匹配。原先“找不到”和“出现多次”只有同一条错误消息，模型难以判断应重新读取哪里。现在失败结果另含 `edit_failure`：

```json
{
  "error": "old must match exactly once in an existing file",
  "changed": false,
  "edit_failure": {
    "index": 2,
    "reason": "not_found",
    "exact_matches_min": 0,
    "read_suggestions": [{"offset": 1799, "limit": 7}],
    "suggestions_truncated": false,
    "note": "Navigation hints only. Read current lines and submit a unique exact old block; no edit was applied."
  }
}
```

`index` 是本次 `edits` 中从 1 开始的失败片段编号，单片段为 1。`reason` 区分 `empty_old`、`not_found`、`ambiguous` 和 `line_endings_mismatch`。`exact_matches_min` 为已知下界，重复时报告 2，不把它当作完整出现次数。行尾差异还报告 `normalized_matches_min`，仅帮助诊断，不把归一化匹配作为写入依据。

建议页可直接传给同一路径的 `read_file`。定位以完整文件的 CR/LF 物理行号计数，最多返回三个页面，每页请求最多 50 行；从旧文本前 64 行中选最多三个不超过 256 字符的非空行作为导航锚点。锚点比较可忽略首尾空白，实际编辑仍要求逐字符精确匹配，包括缩进和行尾。锚点可能不对应预期代码；应检查实际页面并提交唯一的旧片段。`suggestions_truncated` 表示还有未返回的导航页，没有建议也不能证明不存在相似代码。

诊断只返回固定说明、编号及读取参数，不返回源代码、旧文本或新文本，不推断替换内容。完整文件的隐私/UTF-8/大小检查仍先执行；只读模式、路径限制、项目规则及取消同样适用。诊断使用原编辑操作的受限快照，不另读其他文件。任意片段匹配失败时全部片段保持未写入，不请求批准，不建立撤销记录；重新提交成功的精确编辑仍展示完整 diff、请求新批准并复查文件。

[Aider 0.86.2](https://github.com/Aider-AI/aider/blob/v0.86.2/aider/coders/editblock_coder.py)会在失败时展示相似代码位置；[Pi 1.0.0](https://github.com/earendil-works/pi/blob/v1.0.0/packages/coding-agent/src/core/tools/edit.ts)在编辑前处理行尾。这说明编辑恢复是实际客户端能力的一部分。Sprig 此次增加定位信息，没有实现自动模糊替换，也不据此宣称功能或性能全面相同。

新增测试覆盖后部文件定位、CRLF/CR/LF、重叠重复、后续片段失败、隐私、只读、输出大小和取消。实际本地 HTTP 回归用固定回复驱动“失败 → 建议页读取 → 精确编辑 → 固定验收”，四次请求完成，写入和验收分别批准。它验证恢复链路，不是模型的失败恢复成功率；原有默认读取仍只能返回该长文件的开头。

相较提交 `6a61f62`，工具描述增加 212 个 UTF-8 字节，会进入每轮工具定义；失败诊断也增加少量上下文和本地扫描工作。成功编辑的匹配和写入流程保持相同。尚未测量真实模型的净请求数、总延迟或成功率收益。本批使用 `[skip ci]`，本地 Windows/WSL 回归不代表远程 CI 或 macOS 验证。
