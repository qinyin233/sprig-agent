# 按声明名读取源码

已知文件和声明名时，可以直接取得对应的源码范围，减少先查询行号再读取的步骤：

```json
{"path":"src/service.py","symbol":"Service.run"}
```

这是 `read_file` 的可选模式，名称精确匹配现有声明索引；Python 类和成员使用 `Class.method` 形状，其他语言使用 `project_map` 返回的名字。已有 Python、JavaScript、TypeScript、TSX、Rust、Go、C、C++、Java 解析器都支持范围元数据，没有新增模型后端或运行源码。

默认返回真实行号、声明起止行、`symbol_complete`，并沿用完整文件 SHA 和短 `read_id`。Python 的声明范围包含装饰器。返回内容按整行展开；一个物理行上可能还有其他代码。解析器记录之外的注释、宏、导出包装、导入和继承关系可能还需单独读取；它不是 LSP、跨文件引用解析或语义分析。

```json
{
  "path":"src/service.py",
  "symbol":"Service.run",
  "symbol_start_line":12,
  "symbol_end_line":26,
  "symbol_complete":true,
  "offset":12,
  "end_line":26
}
```

上例只展示位置字段，实际响应还包括源码和版本信息。`symbol_complete=true` 仅表示已返回解析器识别的整个声明范围，不证明读取了所有相关实现。文件后方还有其他内容时，原有 `truncated` 仍可能为 true；两个标志分别描述声明范围和文件。

## 边界与继续读取

- 单个待解析文件最多 512 KiB，仍先检查整个文件。更大的文件使用普通行范围或 UTF-8 字节分页，原有 8 MiB 扫描上限保持不变。
- `symbol` 是精确索引名，最多 1024 UTF-8 字节。找不到精确名称时，`symbol_suggestions` 可返回同文件中末段名称相同的声明及其起止行，例如请求 `MultiCommand.resolve_command` 时给出 `Group.resolve_command`。候选最多八项，并受完整 JSON 输出上限及隐私规则约束。返回的列表因上限缩短时会标记 `symbol_suggestions_truncated`。根据返回的精确名称或行范围再次读取，取得实际源码和版本标识。
- 语法错误、索引不可用或达到资源上限时，可改用搜索和普通行读取。
- 同名声明/重载不会自动挑选。错误返回总匹配数及有界的行位置列表；可用普通 `offset/limit` 选择范围。位置列表受完整 JSON 输出预算约束，裁剪会标记。
- 命名模式的 `offset` 保持默认 1，不设置 `limit/cursor`，可设置 `line_numbers=false` 取得原文。后续使用普通 `offset/limit` 继续读取；无需再次指定 `symbol`。
- 单次返回仍受 `max_output` 内容字节上限及 10,000 行窗口约束。`symbol_complete=false` 时检查 `end_line/next_offset`；若首行本身被截断，会有 `line_truncated=true`，不能发放编辑读取标识，可改用字节分页。

名称、位置等元数据和 JSON 转义仍计入完整请求体预算，不自动增大限额或补发模型请求。禁用 `read_file` 同时禁用此模式；只读模式允许读取。`project_map` 的开关控制地图工具本身，命名读取由 `read_file` 管理。

## 版本与隐私

声明位置和返回文本来自同一次已校验的文件快照。解析缓存只保存有界元数据，不保存源码、AST 或编辑授权；每次读取仍检查当前文件、路径排除、二进制、UTF-8、凭据和取消。源文件在解析之后被外部修改时，返回的文本和摘要仍属于同一份旧快照；后续编辑会用原有版本核对拒绝过期修改。

内容完整且允许暴露摘要时发放 `read_id`；配置的模型凭据出现在文件中时，不发放摘要或标识。编辑仍先提出完整 diff 并取得新批准。声明读取不执行源码，不产生文件写入，不代表任何测试已通过。普通编号行和字节分页协议见[文件与大文本](text-transfer.md)。

## 验证范围

离线回归覆盖九种语言的明确起止行、Python 装饰器/BOM/CRLF/CR/Unicode、重载歧义、长声明分页、部分首行、缓存后隐私变化、解析期间文件变化、过期编辑拒绝、取消和原地图输出格式。实际本地 HTTP 流程验证命名读取进入下一次请求、读取标识用于新批准的编辑，并分别执行编辑后与最终固定检查。

```sh
uv run --locked python -m unittest tests.test_named_reads -q
uv run --locked python -m unittest discover -s tests -q
```

这些检查证明导航与编辑协议有效，尚未通过真实模型证明编码任务成功率或请求次数提高。

另在旧失败 Click 候选的私有副本上做了[只读复核](benchmarks/named-read-click-20261018.json)。首次五个显式名称中四个完整返回；错误限定名 `MultiCommand.resolve_command` 被拒绝，没有返回正文或编辑标识。随后[通过声明地图恢复定位](benchmarks/named-read-click-resolved-20261018.json)，实际名字为 `Group.resolve_command`，返回第 1936–1973 行且声明完整。首次失败报告保留原样，两份报告以摘要关联。

最终取得的五个声明正文共 4762 字节，单个均不超过本次设置的 6000 字节内容上限。它们只说明工具可以读取该候选的相关声明，不能把错误名称自动当作另一种类型。原候选和副本正文均未修改；未执行候选代码或固定验收，也未请求真实模型。报告中的命令/编辑计数指代理工具层，不包含一次性副本准备和操作者的 Git 提交元信息查询。原任务的 640/642 失败记录不变。
