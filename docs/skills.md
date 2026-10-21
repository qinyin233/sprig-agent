# 按需加载 Agent Skills

Sprig 从明确配置的技能集合目录读取 `技能名/SKILL.md`。模型请求只附带名称、描述、来源目录和内容摘要；匹配任务时通过 `skill` 工具加载完整说明，再按需读取相对路径的资料或脚本文本。加载和预览都不会执行脚本，不增加额外模型请求。默认没有技能目录或新增工具 schema。

## 配置与使用

用户 `config.toml` 或 profile 可配置：

```toml
[skills]
dirs = ["~/.agents/skills"]

[tools]
skill = true
```

最多八个技能集合目录。相对路径以声明它的配置文件目录为基准，`~` 展开到用户主目录；Windows 可使用 `C:/Users/name/.agents/skills`。不同配置层的数组整体替换，CLI 重复 `--skill-dir PATH` 覆盖配置并以当前工作区为基准。显式项目配置不能添加或改变这些根目录，没有自动扫描父目录、主目录或网上安装。

先离线检查仓库自带示例，无须模型名或凭据：

```sh
uv run --locked sprig --no-config --list-skills --skill-dir examples/skills
```

实际任务可沿用自己的模型配置，附加 `--skill-dir examples/skills`，要求“使用 change-review 技能审查这次修改”。模型根据摘要决定加载，完整说明进入正常工具结果。用户也可用 TUI/chat `/skills` 列摘要，`/skill change-review` 本地预览；这些本地查看不会发给模型或改变主输入草稿。`/skill change-review references/report-template.md` 可查看资料，带空格的相对路径用引号包住。`--doctor` 检查配置目录和 YAML 依赖。

`[tools].skill=false` 或 `--disable-tool skill` 同时移除模型 schema 和阻止正文执行入口；本地目录摘要仍可检查。只读模式允许加载说明和资料，不允许运行命令或写文件。禁用单个文件工具不代表禁止所有其他读取接口；跨接口的路径限制仍使用 `[files].exclude` 和 `.sprigignore`。

## 格式与分页

依据 [Agent Skills 格式规范](https://agentskills.io/specification)，每个子目录至少包含带 YAML frontmatter 的 `SKILL.md`。必填 `name` 与目录名完全相同，最长 64 个小写字母/数字/连字符，不接受首尾或连续连字符；`description` 最长 1024 字符。支持 `license`、`compatibility`、`metadata`、`allowed-tools`；附加字段应放进字符串键值的 `metadata`。

YAML 使用锁定的 PyYAML SafeLoader，拒绝重复键、别名/锚点、自定义对象标签、过深结构和超量节点。frontmatter 最多 16 KiB，整个 `SKILL.md` 最多 64 KiB。相同名称出现在多个目录时两者都不暴露，不静默覆盖；无效技能列出固定诊断，不回显其正文。

模型工具 `skill(name, path="SKILL.md", offset=0, page_sha256=None)` 只接受当前目录中可发现的名称及技能内相对 POSIX 路径。UTF-8 资料最多 256 KiB，拒绝二进制。长正文按实际 JSON 编码分页，结果受 `max_output` 与 16 KiB 较小值限制；使用 `next_offset` 和同页 `page_sha256` 续读。偏移是完整脱敏后的 UTF-8 字节位置，不能落在字符中间；内容改变必须从头读，不能拼接不同版本。完整读完各页后再执行工作流。

本地分页语法为 `/skill NAME PATH OFFSET PAGE_SHA256`。结果里的源文件 SHA 仅说明读取版本，不是文件编辑 `read_id` 或验收证明。内联路径以 `base_directory` 为基准；脚本需要另外通过 `run` 提出完整 argv 并取得新批准，执行目录仍受原有规则约束。

## 运行边界

技能扫描不递归整个集合：只检查直接子目录的 `SKILL.md`，不把任意 Markdown 当成技能。最多 64 个有效技能、4096 个集合条目，目录摘要最多 32 KiB，扫描另有协作式两秒限额；遇到总量截断时整份目录不暴露部分注册表，应缩小配置范围。同步文件 I/O 不保证能被限时强制中断。

根路径、技能目录和资源均沿现有原生目录句柄访问，不跟随 symlink/junction，不读硬链接文件、配置文件、内置敏感组件或排除路径。工作区内的技能叠加工作区 `.sprigignore`，集合目录也可配置自己的 `.sprigignore`。已知凭据在整份文本上脱敏后再分页，疑似密钥或私钥文件直接拒绝；未知隐私无法自动识别。目录和文件变化会在新的读取中复查，模型请求后改变的技能清单不能用于旧调用。

技能只是工作流指引。`allowed-tools` 是描述信息，不产生预批准、权限提升、关闭只读、自动安装或自动运行。现有取消、工作区路径限制、环境凭据过滤、逐次操作批准和独立验收由应用代码执行。获批命令仍不是 OS 沙箱，可访问其他路径和网络。

正文和资源结果会进入下一次供应商请求，以及启用保存的私有明文会话。目录配置绑定会话和摘要，换目录不能套用旧绑定；恢复不会重新加载或执行历史工具。目录内容本身仍可更新，下次请求和加载使用当前版本；旧结果只是带摘要的历史观察。

## 实现依据与验证

重读固定 OpenCode `dev@9d90d89` 的 [`skill.ts`](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/skill.ts)、[`tool/plugin/skill.ts`](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/tool/plugin/skill.ts) 和 [`skill-scan.ts`](https://github.com/anomalyco/opencode/blob/9d90d892dcbfc52b47e95b2ea31b8aba877128fd/packages/core/src/config/plugin/skill-scan.ts)。采用摘要发现与按需加载思路，保持 Sprig 的明确目录和审批边界；没有复制上游的链接跟随、权限持久授予或插件安装。

测试包含三协议实际 HTTP/SSE、元数据与正文分离、资料分页、Unicode/换行保真、秘密跨页、无效 YAML、变化后的续读拒绝、目录隐私和原生链接拒绝、只读/取消/关闭资源、CLI/TUI 本地预览及新操作批准。固定回复验证程序和协议流程，不证明模型编码成功率；具体平台与精确 CI 结果见[机器记录](benchmarks/skills-20261021.json)。当前没有技能市场、安装器、hooks、自动脚本或 MCP。
