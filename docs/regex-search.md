# 正则代码检索与资源边界

日期：2026-10-05，接口更新于 2026-10-22。模型现在使用默认正则的 [grep](grep.md)，显式 `literal=true` 搜索原文；旧 `search` 仍默认字面字符串、`regex=true` 才解释表达式。以下旧入口示例和历史测量保持其原始条件；后端和资源边界共用。

参考 [Pi grep 的实际实现](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/tools/grep.ts)：它默认使用 ripgrep 正则，并提供 literal、glob、ignoreCase、context 和 limit。Sprig 使用锁定的本地 `google-re2==1.1.20251105`，保留文件工具自己的目录句柄、隐私检查和遍历预算。工具执行时不下载或启动外部搜索程序；这不意味着两者的全部正则语法相同。

## 使用与语义

工具参数示例：

```json
{
  "query": "^(?:async )?def (?:load|save)_item\\(",
  "path": "src",
  "glob": "*.py",
  "regex": true,
  "context": 1,
  "limit": 100
}
```

| 场景 | 表达式 | 行为 |
| --- | --- | --- |
| 找两类定义 | `^(?:async )?def (?:load|save)_item\(` | 命中同步或异步定义；返回实际完整行 |
| 常见测试方法 | `^\s*def test_.*(?:cancel|privacy)` | 组合名称条件；可配合 `glob="test_*.py"` |
| 中文文本 | `\p{Han}+` | 按 Unicode 属性匹配 |
| 空行 | `^$` | 每个实际空行一次；文件末尾不额外生成空行 |

最多 1,000 个字符；支持 RE2 的分支、分组、字符类、量词、锚点和 Unicode 属性，不支持 lookaround、回溯引用或跨物理 CR/LF 行匹配。U+2028 等 Unicode 分隔字符留在原物理行。同一行有多处匹配仍只计一次，零宽匹配也只计一次。`context` 的前后数组只包含真实存在的物理行，EOF 附近可少于请求数量。

大小写忽略在正则模式采用简单 Unicode 折叠，例如 `k` 可以匹配 Kelvin 符号；它不会把 `Straße` 展开为 `STRASSE`。字面模式继续使用 Python 完整 `casefold()`。RE2 的 `\w`、`\d`、`\b` 使用 ASCII 语义，需要 Unicode 字符时使用属性类。具体语法见 [RE2 官方语法](https://github.com/google/re2/wiki/Syntax)。

## 安全与性能限制

[RE2 的设计](https://github.com/google/re2)以处理不可信表达式为目标：避免指数回溯，原生编译和匹配使用可配置资源预算。Sprig 设置 1 MiB 引擎程序/缓存预算并关闭原生错误日志；无效、不支持或过于复杂的表达式返回固定错误，不回显查询。绑定版本的原生接口只测试行是否匹配，不构建捕获组与 Unicode 位置对象，也不使用 Python 包默认的全局 128 项表达式缓存。表达式与原生缓存随本次调用释放。[锁定版本的发行文件](https://pypi.org/project/google-re2/1.1.20251105/#files)提供 Windows、Linux 和 macOS 构建；是否实际运行仍以本批平台测试为准。

编译与逐行匹配的累计工作时间达到 1 秒时，在原生调用返回后的检查点停止整个搜索；这不是硬性墙钟超时。磁盘读取、遍历及序列化不计入该工作时间，仍受原有 512 KiB 单文件、8 MiB 总读取、5,000 个条目、32 层目录和输出预算限制。引擎预算不等于 Python 进程全部内存限制。

正则结果增加 `timed_out` 字段。工作预算耗尽时，同时标记 `timed_out=true` 和 `truncated=true`，保留已经完整的命中记录，不返回尚未完成的上下文。显式历史压缩保留超时证据。取消在编译、读取与匹配边界检查；编译或匹配结束时已经取消，会丢弃本次结果。完整的受限文件仍先经过凭证、UTF-8、二进制和隐私检查，才交给正则；新的忽略规则在下一次调用生效。单条结果超出预算时整体省略，已知密钥遮盖后的 JSON 大小也计入上限。

## 离线定义窗口对照

```powershell
uv run --locked python scripts/benchmark-tools.py --regex-search --compare-ref 2570de2acc1a5a73f75f60d519a1d8e825b42118 --repeat 5
```

旧版使用三条已知的字面查询，新版用一条分支正则，在相同的合成 Python 文件中返回相同定义窗口。脚本独立加载两版源代码，核对每个路径、行号、正文和前后行，以及实际文件读取数和字节数；保存代码、驱动、锁文件与输入摘要。每个样本创建新的 Tools 实例；操作系统缓存没有清空，结果不包含模型和用户等待时间。

原始结果：[Windows Python 3.13](benchmarks/regex-search-windows.json)、[WSL Linux Python 3.12](benchmarks/regex-search-linux.json)。两平台、两版本的输入和已知窗口一致。

| 场景 | 旧版 → 新版调用/文件读取 | Windows 中位数旧 → 新 | Linux 中位数旧 → 新 |
| --- | --- | --- | --- |
| 32 文件，96 个定义窗口 | 3 → 1 次调用；96 → 32 次文件读取 | 24.093 → 9.954 ms | 6.346 → 5.893 ms |
| 1 文件，3 个后部定义窗口 | 3 → 1 次调用；3 → 1 次文件读取 | 4.124 → 2.674 ms | 1.893 → 1.501 ms |
| 32 文件，原有单条字面查询 | 两版各 1 次调用、32 次文件读取 | 7.010 → 8.553 ms | 2.075 → 1.780 ms |

字面控制组的 Windows 波动说明不能宣称所有检索更快。固定多条件查询减少重复读取，不等于真实模型一定会采用同样查询，也不能换算为模型请求、token、费用、任务总延迟或编码成功率。自然复杂仓库、同模型竞品任务与大更新的远程矩阵仍待验证。

## 本批验证

最终代码的全量测试各运行 408 项：Windows Python 3.11 / 3.13 各 394 通过、14 项 POSIX 专属检查跳过；WSL Linux Python 3.12 为 392 通过、16 项平台检查跳过。Windows Python 3.14 另跑 35 项搜索检查，全部通过；这不代表该版本的产品全量测试已经通过。新增检查覆盖实际原生引擎、编译资源超限、回溯陷阱、ASCII/Unicode 语义、跨文件累计预算、取消、完整文件隐私、严格 JSON 输出上限、历史压缩与真实回环 HTTP 调用链。

锁定依赖安装、CLI/测量工具帮助、包构建与内容检查通过。本批为 `[skip ci]` 提交，尚无该提交的 GitHub CI 或 macOS 运行结果；本地结果不能代替远程矩阵。
