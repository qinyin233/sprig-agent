# OpenCode 外部编辑器与 Textual 暂停语义

2026-10-28，只读核对 OpenCode 固定提交 `fa1260d6ca484fe0fd868aa992ad81ab85343cf3` 的 TUI 外部编辑器。Sprig 对照基线为 `45e1915023e656cfe99ec61797af62a69b120f65`；该基线的[能力盘点](https://github.com/qinyin233/sprig-agent/blob/45e1915023e656cfe99ec61797af62a69b120f65/docs/development-readiness.md)仍列外部编辑器为缺口。本次没有运行上游、调用模型、读取凭据或触发 CI，不把静态阅读当作功能交付或跨平台实测。

## OpenCode 实际调用链

TUI 的 `/editor` 对应 `prompt.editor`。它先将草稿里被跟踪的长粘贴占位展开为正文，再调用编辑器；正常返回后只更新输入框、光标和相关元数据，没有调用提交任务的函数。异常会显示错误并保持原草稿；空返回也直接保持原草稿。因而“保存并退出编辑器”和“向 agent 发送任务”是两个动作。[TUI 入口](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/tui/src/component/prompt/index.tsx#L550-L580)

实际启动函数从进程环境中按 `VISUAL`、`EDITOR` 顺序选择字符串，两者都没有则报错。它用空格切分字符串，把临时文件路径追加到参数尾部；stdio 默认继承，cwd 选择传入且存在的目录，否则使用当前进程目录。Windows 分支设置 `shell:true`，其他平台不设置 shell。所读路径没有 Windows 专属默认编辑器、显式 argv 配置或引号感知的参数解析。[选择、参数与 Windows 分支](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/tui/src/editor.ts#L14-L27)

临时文件使用系统临时目录下的毫秒时间戳 `.md` 名称；先写入草稿，再暂停 renderer。编辑器发生启动错误，或退出码非零/被信号结束时拒绝结果；只有零退出才读取 UTF-8 文件。`finally` 尝试删除临时文件并恢复 renderer。[写入与暂停](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/tui/src/editor.ts#L17-L20)、[退出与清理](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/tui/src/editor.ts#L21-L38)

需要区分已经写出的代码和未被这条路径证明的保证：临时文件写入、renderer 暂停位于 `try/finally` 之前；没有看到随机独占创建、显式私有权限、读取大小限制或替换链接检查，也没有向 spawn 传入 AbortSignal/期限。这不等于已经复现了安全事故，或证明项目所有其他层都没有保护；它说明这些保证不能从这个函数推导，Sprig 不应无条件照搬。[完整函数](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/tui/src/editor.ts#L14-L39)

## 长粘贴、换行和选择元数据

`expandPromptInputPastedText` 根据已跟踪的粘贴范围展开文本，同时调整其后文件、agent 和 skill mention 的位置。编辑回来后 `realignPromptInputMentions` 在新正文中重新匹配旧 mention 文本，更新能匹配到的元数据；未找到的有文本 mention 会被移除。这是上游自己的选择重定位语义，不能作为 Sprig 从任意同形标记文字恢复附件授权的依据。[展开与位移](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/tui/src/prompt/mention.ts#L85-L104)、[返回后的重定位](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/tui/src/prompt/mention.ts#L20-L82)

返回后的 `normalizePromptContent` 只在正文原本为单行、末尾仅多一个 LF 或 CRLF 时去掉该末尾换行；多行文本不在此函数统一换行或 `trim()`。调用者随后清空粘贴折叠元数据，所以成功编辑后的正文直接留在输入框，不再保留旧粘贴占位。[内容规范化](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/tui/src/prompt/content.ts#L1-L12)、[草稿替换](https://github.com/anomalyco/opencode/blob/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/tui/src/component/prompt/index.tsx#L570-L580)

## Textual 8.2.8 的官方证据

Sprig 本机安装版本为 Textual `8.2.8`。官方标签对应提交 [`1d99508b928a771b51e1a527319c6b87dcff9e05`](https://github.com/Textualize/textual/commit/1d99508b928a771b51e1a527319c6b87dcff9e05)；本机 `textual/app.py` 归一 CRLF 为 LF 后计算的 Git blob 为 `a716ba35522b963caec43ed3b8661ca885c9b401`，与本次读取的官方该版本文件相同。这项是源码身份核对，没有实际打开交互式编辑器。

`App.suspend()` 的官方说明明确支持 Unix 类系统和 Microsoft Windows，不支持 Textual Web。它先停止应用终端模式，在上下文块中让出输入输出，退出后恢复 driver 并刷新布局；driver 不支持时抛 `SuspendNotSupported`。应用必须依据运行 driver 的能力处理失败，不能仅根据操作系统名称判断支持。[官方说明与实现](https://github.com/Textualize/textual/blob/1d99508b928a771b51e1a527319c6b87dcff9e05/src/textual/app.py#L4717-L4768)

该版本恢复 driver 的语句位于 `yield` 之后，未放进包围该 `yield` 的 `finally`。因此按 Python 生成器上下文管理语义推断：如果编辑器异常直接穿过 `with app.suspend()`，这些恢复语句可能不执行。这是源码推论，本次没有运行故障复现。Sprig 应在 suspend 块内部捕获编辑器异常，正常退出上下文完成恢复后，再向用户报告错误；启动 suspend 本身失败、恢复失败仍需独立处理。[异常边界](https://github.com/Textualize/textual/blob/1d99508b928a771b51e1a527319c6b87dcff9e05/src/textual/app.py#L4743-L4768)

## 对 Sprig 实现的约束

以下是依据源码和 Sprig 既有权限语义提出的设计要求，不是已实现声明：

- 保留用户明确打开编辑器、保存后仍需 Enter 发送的流程；只改草稿，不启动模型、排队任务或执行编辑后的 slash 文本。
- 用可信、明确的 argv 配置和具体启动批准，避免直接复制空格切分与 Windows 隐式 shell。外部编辑器仍是有用户系统权限的程序，文本编辑入口不能变成模型可无批准执行的命令入口。
- 建立私有、随机且独占的临时目录/文件，限制导出和读回 UTF-8 字节、终端控制字符、文件类型与链接；启动或读取失败保留原草稿，不展示潜在敏感正文。清理与恢复要覆盖写入、暂停、启动、等待、读回及关闭的各阶段。
- 导出真实长粘贴正文，不把显示占位交给编辑器；回来后不从伪造标记复活旧正文或附件选择。保持 Sprig 原有首尾空白/换行约定，明确处理附件元数据和空文件，不能把上游“空文件保持旧草稿”当作必然产品语义。
- 等待期间若出现新草稿、会话切换、取消或关闭，旧编辑结果不能覆盖新状态；读回成功与实际提交的完整协议预算预检仍是不同阶段。
- 除无显示的 TUI 回归外，还需要真实终端中暂停/恢复、失败/中断、Windows 与 POSIX、带空格/中文路径及等待式 GUI 编辑器的证据。GUI 编辑器若提前退出而实际编辑窗口尚未结束，不能把父进程退出当作文件完成；所需 wait 参数由具体命令确定。

本次只新增研究记录，没有修改应用、测试或配置。远程 CI 在 2026-11-01 前继续冻结，到期后也须先核实账户限制与此前门禁；本篇不声明原生 CI 或模型性能改善。
