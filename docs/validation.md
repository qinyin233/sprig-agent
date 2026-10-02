# 本轮开发与验证记录

日期：2026-10-02。开发基线为仓库 `main` 的 `3ef37f8`，功能改动已分批提交并推送到 `origin/main`，未发布 Release。下列结果来自实际运行，不是预期的 CI 结果。

## 分批提交与验证

| 批次 | 提交 | 范围 | Windows 提交快照验证 |
| --- | --- | --- | --- |
| 1 | [`df30b21`](https://github.com/qinyin233/sprig-agent/commit/df30b21) | 原生 Windows 文件与命令执行、私有会话、项目地图 | 102 项：89 通过、13 平台跳过；CLI 帮助正常 |
| 2 | [`3eb3fc6`](https://github.com/qinyin233/sprig-agent/commit/3eb3fc6) | SSE 流式响应、完整工具调用校验、CLI 增量显示 | 121 项：108 通过、13 平台跳过；CLI 帮助正常 |
| 3 | [`8bce0f1`](https://github.com/qinyin233/sprig-agent/commit/8bce0f1) | TUI、Enter 发送、中途引导、固定验收与修复、uv 和 CI 配置 | 独立 uv 环境安装锁文件；151 项：138 通过、13 平台跳过；CLI 帮助正常 |

以上测试使用暂存区导出的独立快照，避免后续未提交的功能影响前一批的验证。启动脚本、构建清单、用户文档和调研报告单独整理为最后一批。

## uv 环境与离线测试

Windows 开发环境使用 uv 0.9.14 管理的 CPython 3.13.0，项目环境为 `.venv`。`.python-version` 选择 3.13，`uv.lock` 锁定当前项目、Textual 8.2.8 及其依赖。模型与工具核心使用标准库，TUI 使用 Textual。WSL 使用独立的 `.venv-wsl`，不共用 Windows 虚拟环境。

| 实际环境 | 测试数 | 通过 | 跳过 | 结果 |
| --- | ---: | ---: | ---: | --- |
| 原生 Windows，uv `.venv`，CPython 3.13.0 | 151 | 138 | 13 | 通过 |
| 原生 Windows，uv `.venv-py311`，CPython 3.11.14 | 151 | 138 | 13 | 通过 |
| 原生 Windows，uv `.venv-py314`，CPython 3.14.0 | 151 | 138 | 13 | 通过 |
| WSL Linux，uv `.venv-wsl`，CPython 3.12.13 | 151 | 136 | 15 | 通过 |

Windows 跳过 POSIX 专用权限、FIFO、符号链接和目录 FD 测试；Linux 跳过 15 项原生 Windows API 测试。Windows 的 ACL、junction、目录锁定、文件更新竞态、相对可执行路径及 Job 清理有专门的实际系统测试，不能把跳过数当作通过数。

Windows Python 3.11 在中文路径的 editable `.pth` 解码上存在旧版 `site` 模块限制；这组验证使用 uv 的 `--no-editable` 安装和运行，避免生成这类路径引用。默认 3.13 环境、PowerShell 启动器和 Windows 3.14 环境无需此选项。

本地复现：

```sh
uv sync --locked --managed-python
uv run --locked python -m unittest discover -s tests -q
uv run --locked sprig --help
```

同一检出中的 WSL 环境：

```sh
export UV_PROJECT_ENVIRONMENT=.venv-wsl
uv sync --locked --managed-python
uv run --locked python -m unittest discover -s tests -q
```

GitHub Actions 已配置 Ubuntu、Windows、macOS 与 Python 3.11–3.14 的矩阵，使用 uv 安装环境、运行测试。推送后查询 Actions API 返回 HTTP 403，因此远程 CI 结果尚未核实；实际运行状态见 [GitHub Actions](https://github.com/qinyin233/sprig-agent/actions/workflows/tests.yml)。macOS 尚未本地实测，不能把 CI 配置视为测试通过。

## 重点行为

- SSE 测试覆盖 CR/LF、UTF-8 分段、多行事件、注释、交错工具参数、Agnes 结束后用量事件、空字段、截断、超限和跨片段密钥遮盖。实际本地 HTTP 服务器会等待客户端文本回调后再发送结束事件，验证文本在响应结束之前显示。
- 不完整或非法流不会执行响应中的工具，也不会保存半份助手消息。调用意图先保存、工具结果后保存；恢复不会重放待定调用。
- 固定验收命令真实失败后，测试代理修复实际文件，再由应用执行相同命令。模型宣称成功不能覆盖失败、超时或输出超限；拒绝验收会停止任务。
- Windows 实际 junction 不能用于文件读取、写入、搜索或命令工作目录；批准等待期间目录不能重命名；原文件 DACL 和 CRLF 得以保留。
- Windows 实际后代进程超时终止后没有延迟写入，Ctrl-C 路径也会清理。命令等待作业内进程退出，避免异步终止留下占用的工作目录。
- 用户在确认期间仅更新 Windows 访问时间，不会被误判为文件修改；仍检查文件身份、相关元数据和实际内容变化。

## TUI 交互验证

通过 Textual Pilot 操作实际 Textual 界面和真实临时文件，验证了 Enter 提交、Shift+Enter / Ctrl+J 换行、空消息不请求、多行输入、响应结束前显示文本、多轮历史、完整 diff 弹窗、默认 Enter 拒绝、显式按钮批准、命令验收、拒绝验收、运行命令取消、待确认操作取消、迟到模型工具不执行，以及界面内会话保存恢复。

执行中补充要求的测试覆盖流式响应等待、真实命令运行、待确认修改和固定验收四种时机：连续消息按顺序加入当前会话；旧模型响应的工具不执行；正在运行的命令保留真实结果，同批剩余调用标记为未执行；确认窗可用 Ctrl+L 聚焦调整输入框，发送后需重新批准新 diff。最后一刻到达的新消息仍由同一工作线程处理，取消后补充要求可保存并在明确继续后采用。每组历史均满足工具调用/结果顺序，保存和加载往返一致。

取消后的进程检查不依赖 UI 响应速度：测试先取消并等待清理，再释放后代进程的写入条件，确认没有残留进程产生文件。恢复和本地项目地图查看不会调用模型。TUI 文本遮盖已知密钥并显示终端控制字符的转义形式。

另在原生 Windows ConPTY 终端实际启动 `uv run sprig --tui`，确认全屏布局显示，并使用 Ctrl+Q 正常退出，返回码为 0。macOS 未实测。

Microsoft Store 应用别名所建立的 Python 环境在测试中曾出现后代进程逃离清理范围的问题。当前实现会在启动前拒绝 `APPEXECLINK` 以及 `pyvenv.cfg` 指向这类别名的环境；PowerShell 启动器强制选择 uv 管理的 CPython。这里不将 Store 别名虚拟环境报告为受支持或通过验证。任意获批程序或 shell 仍可能间接使用外部代理，命令执行没有沙箱。

## Agnes 真实 API 测试

使用用户提供的凭证，仅在测试进程环境中配置：

- 模型：`agnes-3.0-flash`
- API 根地址：`https://apihub.agnes-ai.com/v1`
- 协议：`POST /chat/completions`，普通和 SSE 流式 function tools

在 Windows uv `.venv` 中运行 `scripts/live-smoke.py`，临时项目的 `calc.py` 起初错误地返回 `a - b`。两种模式都实际调用 `read_file`、`edit_file`，修复为 `a + b`，然后通过应用固定的验收命令，返回码为 0。最终会话保存后加载的消息与原消息完全相同。

| 模式 | 实际修复 | 固定验收 | 会话往返一致 | 文本回调次数 |
| --- | --- | --- | --- | ---: |
| 普通 | 通过 | 通过，返回码 0 | 是 | 0 |
| 流式 | 通过 | 通过，返回码 0 | 是 | 43 |

回调次数随模型回复变化，不是速度或性能指标。测试夹具只批准这一项精确修复与固定验收 argv；其他写入和命令被拒绝。产品仍要求逐次人工确认，没有自动批准选项。API 测试会消耗账户额度，普通离线测试不调用真实服务。测试密钥未写入项目或会话文件。

随后运行 `scripts/live-tui-smoke.py`，通过 Textual Pilot 在真实 TUI 中完成同一 Agnes 流式任务。界面两次明确批准分别对应修改与固定验收命令；实际文件修复、验收返回码及会话往返检查均通过。SVG 快照记录了该次 headless 界面的实际渲染，不能当作 Windows 桌面截图或 macOS 测试证明。

更新交互后运行 `scripts/live-tui-smoke.py --steer`：任务由 Enter 提交，在首次确认期间通过调整输入框 Enter 发送补充要求。旧操作作废后，模型重新提出修复，经两次新批准完成文件修改和固定验收；最终总结包含要求的 `STEERING_OK`，补充消息进入保存历史，加载往返一致。输出 `tui_success=true`、`enter_submission=true`、`steering_sent=true`。该测试使用真实 Agnes API 与 Textual Pilot；流式等待和运行中命令的竞态由可控制的离线测试单独验证。

## 启动与构建

`uv run --locked sprig --version` 和 `scripts/run-agnes.ps1 --help` 成功，后者无需密钥即可查看帮助。`uv build --sdist --wheel` 成功构建源码包和通用 Python wheel；新增流式、TUI 与 Windows 模块包含在 wheel 中，源码包包含 uv 配置、文档和启动/验证脚本。构建工具对原有许可证元数据格式有弃用提示，不影响本次构建。

`git diff --check` 无空白错误。源码、测试、脚本、文档和锁文件中没有检测到本次提供的 API key 格式原文。此检查只覆盖已知凭证，不构成任意秘密检测保证。

当前交付的功能和后续差距见[同类项目对照](agent-comparison.md)。没有进行与其他代理的性能、成功率或安全性对比，也没有验证所有兼容服务。
