# 独立任务工作目录

Sprig 可以显式创建 Git worktree，让新任务从一个固定提交开始，并保留原目录的暂存修改、未暂存修改和未跟踪文件。工作目录独立，Git 对象和引用仍然共享；这是 [Git worktree](https://git-scm.com/docs/git-worktree.html) 的目录模型，不是进程、文件系统或网络沙箱。

## 创建并开始新任务

在 Sprig 开发环境中运行，`--workdir` 指向待开发仓库的根目录：

```sh
uv run --locked sprig --workdir /path/to/repo --create-worktree ../repo-task
uv run --locked sprig --workdir /path/to/repo-task --tui
```

Windows PowerShell 同样支持：

```powershell
uv run --locked sprig --workdir E:\projects\repo --create-worktree E:\projects\repo-task
uv run --locked sprig --workdir E:\projects\repo-task --tui
```

新目录的父目录必须已存在，目标必须在源工作区之外且不存在。相对路径相对于 `--workdir` 解析。默认从 `HEAD` 开始，也可以显式选择本地提交、分支或标签；解析后固定到完整提交 ID，不跟随审批期间发生的分支移动：

```sh
uv run --locked sprig --workdir /path/to/repo --create-worktree ../repo-task --worktree-ref v1.0
uv run --locked sprig --workdir /path/to/repo --list-worktrees
```

创建结果包含新路径、实际提交、`verified` 和新进程的 `start_argv`。新目录使用 detached HEAD，不创建分支，不复制未提交修改。开始任务前确认固定提交包含所需的已有工作；原目录的修改保留原样。

本地创建/列出命令不初始化模型 provider，不需要模型密钥，不能与任务、附件或交互界面参数混用。它们仍加载配置中的只读、工具禁用、超时、输出及凭据遮盖限制；`--no-config` 可用于完全离线的本地管理。非交互输入默认拒绝批准。

## TUI 与聊天

在输入框中 Enter 发送：

```text
/worktrees
/worktree ../new task directory
```

`/worktree` 后的全部剩余文本作为一个路径，内部空格和 Windows 反斜杠保留，不需要 shell 引号。TUI 和 `--chat` 都支持这两条本地命令。路径、Git 输出和创建结果不自动加入模型历史，也不向模型发请求。

当前会话、工作根、提供商、验收和历史绑定保持原样。创建成功后使用返回的命令在新目录启动一个新会话；旧会话不能直接恢复到不同的工作根。活动任务期间本地管理需等待结束或先取消；普通消息仍用于中途引导。已批准的本地 Git 命令使先前任务验收失效；下一次运行按原规则清除旧编辑读取标识并重新检查。

会话和变更备份的保存目录也不能位于其他 Git 仓库内，包括原工作区、无关检出和 bare 仓库；在创建目录前检查目标的祖先。旧保存位置如在仓库内将拒绝打开，需先明确批准迁移到仓库外。这个检查不调用 Git 或模型，也不自动迁移、删除已有对话。

## 批准与失败边界

每次 Git 进程分别显示完整 argv、工作目录、超时和输出上限，并请求新批准。创建通常包括解析根目录/提交、读取配置键名、创建和核对新 HEAD 四次批准。拒绝后不执行后续命令，不重放旧授权。源目录、目标父目录和本地 Git 配置在批准后重新核对；新目标出现、目录替换、重解析点或配置变化会停止。

Git 从进程 PATH 解析为绝对可执行路径，拒绝工作区内的 Git 可执行文件。管理进程使用最小环境，忽略用户配置的额外命令环境及继承扩展，也忽略系统/用户 Git 配置。它们禁用 hooks、fsmonitor、自动维护、递归子模块、稀疏检出、已声明的 smudge/process 内容过滤器，以及 Git 协议和缺失对象的按需拉取。相关 Git 环境行为见[官方环境变量说明](https://git-scm.com/docs/git/2.46.0)。这使 LFS 等文件保留提交中的指针内容，子模块不自动初始化，缺失对象不会自动补下载。

本地 Git `include/includeIf` 会拒绝创建，避免在批准后从未固定的附加文件加载驱动；配置文件上限 1 MiB，最多 32 个内容过滤器。Git 输出继续受 `max_output` 限制，不能因为结果太长就提高限额或继续执行。旧 Git 不支持所需命令时停止，不换用不受检查的兼容流程。

被凭据遮盖或不能精确解码的 Git 元数据也会停止流程；不能把 `[REDACTED]` 或替换字符当作真实目录、提交或过滤器名来继续执行。配置检查只请求键名，不导出 Git 配置值。

超时、输出超限或后置核对失败可能留下部分目录和 Git 管理记录。创建开始后不确定的结果会标为 `outcome=uncertain`，保留路径并要求检查；不自动重试、删除、prune 或 force。取消会停止管理进程并且不返回完成结果，路径同样需要检查。可在明确批准后查看 `git worktree list`、状态和 diff，再决定后续操作。当前没有自动合并、移除工作树、复制脏状态或切换活动会话的工作流。

Windows 的专用 Git 元数据读取能力仅用于本地配置版本检查，不能创建/写入文件，仍拒绝 `.env`、重解析点和关闭后的句柄。普通模型文件工具继续拒绝 `.git`。这些检查约束管理流程，不构成 OS 沙箱；其他获批命令仍可能访问外部文件和网络。外部进程在命令执行期间修改配置或目录也不构成原子事务，后置检查失败时必须保留不确定状态。

## 验证

离线回归使用真实一次性 Git 仓库，覆盖脏源目录保留、固定提交、从已有 linked worktree 再创建、配置/目标审批竞态、逐阶段拒绝、取消、hooks/过滤器未运行、缺失 promisor 对象不拉取、原会话不能跨目录恢复，以及实际 Textual 界面逐次批准和零模型请求。它们验证产品工作流，不证明真实模型编码成功率提高。

```sh
uv run --locked python -m unittest tests.test_worktrees -q
uv run --locked python -m unittest discover -s tests -q
```

涉及 Windows 句柄行为的验证必须等待包含当前提交的 Windows runner；POSIX 平台跳过该用例不能算 Windows 通过。

本批功能 SHA `ad2f1d59f773e0cc33f3a5083fe6688f951f65f8` 的 [Windows/Ubuntu 原生 CI](https://github.com/qinyin233/sprig-agent/actions/runs/39104363629) 已全部成功，两个作业的精确提交核对和完整 unittest 步骤均成功。macOS 和完整版本矩阵没有在本批运行；后续仅更新验证文档的提交跳过 CI。
