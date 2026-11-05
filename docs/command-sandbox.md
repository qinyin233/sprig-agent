# Linux 命令隔离

把下面的设置放入可信用户的 `config.toml` 或 profile。

```toml
[execution]
command_sandbox = "bubblewrap"
background = true # 如需受管后台命令、管道或 PTY
```

启用后，前台 `run`、固定验收、后台命令、管道和 PTY 都在同一类隔离环境中启动。每条命令依然逐次批准，提示会列出原始 argv、工作目录和只读挂载。会话绑定包含所选模式和额外目录。项目配置不能设置或关闭此模式。配置检查会报告所选模式，实际能否启动取决于宿主。

## 宿主和本地依赖

需要 64 位 x86 或 ARM Linux、支持用户命名空间和其中的 binfmt_misc 挂载，以及系统提供的 `/usr/bin/bwrap` 和 `/usr/bin/unshare`。bubblewrap 需要 `--bind-fd`，util-linux unshare 需要 `--mount-binfmt`。两个启动程序须由 root 所有，且没有 setuid/setgid 或组、其他用户写入权限。WSL 使用新建的二进制格式表，宿主的 Windows 可执行文件处理器保留在宿主。初始化失败时本次命令终止。Windows 和 macOS 在默认 `off` 模式使用各自现有执行路径，显式选 `bubblewrap` 会拒绝命令。

请从工作区外安装和启动 Sprig，例如从独立的 uv 工具环境调用 `sprig --workdir /path/to/project`。隔离模式会检查宿主解释器、Sprig 源码和 Python 导入路径，避免已批准的命令改写下一次启动使用的宿主程序。

隔离内保留工作区的绝对路径，可用 `.venv/bin/python` 运行已经安装的依赖。在隔离外用 uv 的复制模式准备依赖，避免文件与宿主缓存共享硬链接。

```sh
uv sync --locked --link-mode copy
```

如果虚拟环境的解释器链接到用户目录里的 uv Python，用 `readlink .venv/bin/python` 查看链接目标，在同一可信配置层把目标的解释器目录加为只读。目录别名与解析后的路径都可用。相对路径以配置文件所在目录为基准。解释器目录的所有可读内容都会暴露给程序，最多允许八个目录。

```toml
[execution]
command_sandbox = "bubblewrap"
sandbox_read_dirs = ["~/.local/share/uv/python/cpython-3.13-linux-x86_64-gnu"]
```

## 文件和进程边界

- 程序可读写工作区中可见的普通文件。现有的文件排除项、`.git`、`.env`、配置和已知私有存储路径用空的只读文件或目录覆盖。路径保护按大小写不敏感方式匹配。工作区内遇到可见的多链接文件、特殊文件、子挂载，或无法安全覆盖的受保护链接时，执行会被拒绝。工作区扫描上限为 200,000 项、64 层目录和 96 个覆盖路径。
- 只读挂载 Linux 系统运行时及显式选择的额外运行时目录。系统子挂载和 WSL 互操作目录被遮蔽。宿主 HOME、环境变量和凭据不继承，隔离进程使用独立的临时目录。程序不能创建网络 socket 或连接工作区中的宿主 Unix socket。允许私有 Unix 流 socketpair，以支持本地进程内部通信。
- 这层保护针对命令及其子进程。应避免在启动阶段从宿主并发替换工作区、挂载或受保护路径。工作区里未命中排除规则的文件仍然可以读写，允许的运行时目录也可被读取。隔离后的文件写入直接落到工作区。超时、输出上限和作业所有权继续由 Sprig 管理。
- MCP、LSP、外部编辑器、worktree 管理和模型网络请求走各自的入口与审批。Git 元数据被覆盖，因此需要它的命令应由用户在可信环境中单独执行。

与 Codex Linux 实现的设计对照基于固定源码。Sprig 使用固定的挂载和网络策略，并与原有命令审批、输出及 PTY 所有权共用执行入口。 [Codex 挂载实现](https://github.com/openai/codex/blob/695961ee88a2c84c0919814a4345b94a2d10f3ca/codex-rs/linux-sandbox/src/bwrap.rs) · [bubblewrap 目录绑定](https://github.com/containers/bubblewrap/blob/v0.11.1/bubblewrap.c) · [Linux binfmt_misc](https://docs.kernel.org/admin-guide/binfmt-misc.html)
