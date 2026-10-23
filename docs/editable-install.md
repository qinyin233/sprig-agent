# 中文目录与 editable 安装

Sprig 保留 uv 的常规 editable 开发安装。Windows Python 3.11 在中文目录中也可使用 `uv sync --locked --python 3.11` 和 `uv run --locked --python 3.11 sprig --help`，不必改为非 editable 安装。项目默认开发版本仍为 3.13。

## 问题与修复

原先 setuptools 为简单 src 布局生成含绝对源码路径的静态 `.pth`。在本机，Python 3.13 生成的 UTF-8 路径文件被 Windows Python 3.11 按 GBK 读取时，解释器可能在导入应用前就失败；另一些字符组合可能解码成错误路径。这与模型供应商、密钥或 TUI 初始化无关。

现在显式声明 `sprig_agent` 的包目录，保留 src 自动发现，使实测 setuptools 使用自己的 editable 导入钩子。生成的 `.pth` 只包含 ASCII 导入语句，中文路径在 Python 源文件中表达，不再依赖 `.pth` 的本地解码。安装后新增和修改包内模块仍立即生效；也不再把整个 src 目录的无关顶层模块暴露给导入搜索。

构建最低版本从 setuptools 61 对齐到 64，因为 PEP 660 editable hooks 从 64 引入。[官方说明](https://setuptools.pypa.io/en/latest/userguide/development_mode.html)同时强调：具体 editable 实现策略可能随布局和版本改变。因此这里用生成物与真实启动验证约束行为，不把包目录配置视为所有未来版本的保证。当前完整包验证使用 setuptools 84.2.1。

## 已有损坏环境的迁移

新的包目录声明不能让已经损坏的解释器绕过启动时的旧 `.pth`。若 uv 在查询旧 Python 3.11 环境时就报 GBK 解码错误，请保留旧目录，打开一个新的 PowerShell 窗口，选择尚不存在的新环境目录：

```powershell
$env:UV_PROJECT_ENVIRONMENT = ".venv-311-recovered"
uv sync --locked --python 3.11
uv run --locked --python 3.11 sprig --help
```

这不会删除原 `.venv`、修改用户级编码设置或设置全局 UTF-8 模式。环境选择只影响当前窗口；关闭窗口后可回到项目默认的环境选择。若要检查旧环境或保留其中额外安装的依赖，请自行核对，不能把解释器启动失败当作授权删除它的理由。

Windows 与 WSL 仍应分别使用虚拟环境，见 [README](../README.md)。本修复验证同一平台上的 Python 版本复用，不承诺可把带绝对路径的 editable 安装直接搬到另一台机器或另一操作系统。

## 可重复的安装检查

```sh
uv run --locked python scripts/check-editable-install.py --offline
```

检查在临时中文目录复制全部 65 个应用源码文件及实际项目元数据，用 uv 锁定安装运行依赖，再构建完整 PEP 660 wheel、分别安装进两个临时环境。验证 ASCII `.pth`、八个主要入口/模块的真实导入来源、`sprig --help`、安装后新增模块与修改即时生效，以及无关顶层模块不可见。禁用用户 site 和 UTF-8 模式；子进程不继承模型密钥，报告不输出原始环境、子命令错误正文或私有路径。

本机已缓存对应 Python 和依赖时，还可指定跨版本消费：

```sh
uv run --locked python scripts/check-editable-install.py --producer-python 3.13 --consumer-python 3.11 --offline
```

`--offline` 禁止下载，要求运行依赖和构建依赖已在 uv 缓存中；缓存不足时检查失败，不能算通过。默认用当前 Python 创建两个环境，适合后续 CI 各版本作业；指定另一版本时，uv 也需要能找到对应解释器。检查仅更改临时副本，不改用户虚拟环境，不调用模型。它不是操作系统沙箱或真实编码性能基准。

Windows 完整包负对照复现了旧 `.pth` 的 GBK 启动失败；修复后 3.13→3.11、各自同版本和本地 WSL 验证通过。具体范围与本批回归见[验证记录](benchmarks/editable-install-20261023.json)。GitHub Actions 依用户要求仍暂停；未来矩阵已加入该检查，但本批不能声称远端 CI 或 macOS 通过。
