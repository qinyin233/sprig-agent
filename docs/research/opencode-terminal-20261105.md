# OpenCode 终端进程与 Sprig 清理边界

2026-11-05 读取 OpenCode dev `32e70a84edca690612d3b1760b0c4782537b4b93`。普通 PTY 会话提供启动、输出、退出、输入、调整尺寸和终止。Node 适配调用 `@lydell/node-pty`，Windows 单文件分发传入 ConPTY 选项。服务保存有界输出尾部和绝对游标，交付重放后再启用实时订阅。游标采用 JavaScript 字符串长度，Sprig 的日志分页采用 UTF-8 字节。[进程接口](https://github.com/anomalyco/opencode/blob/32e70a84edca690612d3b1760b0c4782537b4b93/packages/core/src/pty/pty.ts) · [Node 适配](https://github.com/anomalyco/opencode/blob/32e70a84edca690612d3b1760b0c4782537b4b93/packages/core/src/pty/pty.node.ts) · [会话服务](https://github.com/anomalyco/opencode/blob/32e70a84edca690612d3b1760b0c4782537b4b93/packages/core/src/pty.ts)

独立的持久 PTY 服务通过 daemon 处理创建、附加、输入、尺寸、交接和屏幕快照。WebSocket 处理器通过同一写队列维持重放、实时输出和关闭的顺序。连接到当前对话的终端、跨进程续接需要分别实现和验证。[持久会话](https://github.com/anomalyco/opencode/blob/32e70a84edca690612d3b1760b0c4782537b4b93/packages/core/src/persistent-pty/index.ts) · [输出队列](https://github.com/anomalyco/opencode/blob/32e70a84edca690612d3b1760b0c4782537b4b93/packages/server/src/handlers/pty-socket.ts)

Sprig 已有逐次批准的后台管道输入、有限输出和完成日志。新增控制终端要处理前后台进程组、窗口尺寸、键输入和退出，还要在显示和记录之前处理跨块的终端控制序列。Windows 可以复用挂起启动并纳入 Job 的归属策略，ConPTY 的同步输入、输出和关闭需要防止互相阻塞。POSIX 可以通过独立进程建立控制终端，避免在多线程 TUI 进程内直接 fork。[Microsoft ConPTY 文档](https://learn.microsoft.com/en-us/windows/console/creating-a-pseudoconsole-session) · [Python PTY 文档](https://docs.python.org/3/library/pty.html)

在一次性 WSL 夹具中，父进程退出或后台任务获批取消后，同会话的另一个进程组仍继续运行。两条测试都通过实际工具入口复现。Linux 清理现在在保留会话组长期间回收这些后代，使用 pidfd 约束信号目标，实际测试同时验证另一个独立会话仍可运行。独立 `setsid`、系统隔离、macOS 会话清理和终端显示属于后续边界。[Linux pidfd 信号接口](https://man7.org/linux/man-pages/man2/pidfd_send_signal.2.html) · [Sprig 后台任务](../background-jobs.md)

两端分别运行了一次性原型。Windows 以 `STARTF_USESTDHANDLES` 和空标准句柄建立 ConPTY，子进程使用控制台句柄。省略这个标志时，当前重定向宿主中的 Python 夹具收到无效的控制台尺寸句柄。正确设置后，uv 环境里的 Python 能识别终端、读取单个按键并观察尺寸从 80×24 变为 92×27。父进程自然退出后，归属 Job 回收仍在运行的子进程。WSL 原型由独立进程建立控制终端，验证 `/dev/tty`、同样的按键和尺寸变化，并用本批清理后端回收另一个进程组。这些原型为 TUI 接入和有界终端渲染提供接口依据。[node-pty 的 Windows 启动参数](https://github.com/microsoft/node-pty/blob/35c28c7d68a192699e70b1e6a142908bdccd9212/src/win/conpty.cc#L399-L449)

产品接入后用真实终端验证 CLI 中文输入、TUI Enter、单键、尺寸变化、拒绝批准及重置后的后代回收。另一组一次性测试分别在 Windows 和 WSL 输出 4096 条记录和独立末尾标记。按每页 4 KiB 的序列化预算读取，各得到 63 页，能找回全部记录和末尾标记。PTY 流含终端控制及物理换行，显示经过转义，整屏重绘仍需屏幕渲染器。[终端操作](../background-jobs.md#受管终端会话)

OpenCode 自身的 TUI 使用 `EmbeddedTerminalRenderable`，与 GUI 的 `ghostty-web` 界面分开。终端面板接收快照、按顺序应用输出和尺寸事件，尺寸切换可带画面检查点。键盘处理仅在终端取得焦点时接管，并保留应用前导键。Sprig 后续画面视图需要保留这三种边界，即有界画面状态、尺寸与输出的顺序、明确的键盘焦点。模型输入仍经现有批准路径。[OpenCode TUI 面板](https://github.com/anomalyco/opencode/blob/32e70a84edca690612d3b1760b0c4782537b4b93/packages/tui/src/component/terminal-pane.tsx)

Sprig 使用锁定的 pyte 0.8.2 维护文本画面，加入有限控制序列、备用屏和全帧凭据复查。画面尺寸只跟随获批的 PTY 调整，读取不执行程序输入。实测 Windows `more.com` 和 WSL `less` 的真实翻页与退出，两端模型可读快照均精确限制到 1024 序列化字节。原生 TUI 回归另验证备用屏重绘、Enter 焦点和任务重置后的清空。宽字符局部覆盖可在 pyte 留下空续格，Sprig 的有界显示遍历会处理孤立续格。[pyte 发布源码](https://pypi.org/project/pyte/0.8.2/)
