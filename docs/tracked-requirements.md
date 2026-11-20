# 跟踪需求

在 Sprig TUI 中输入下面的命令并按 Enter，把独立的要求记录在同一会话中。名称用小写英文字母开头，后续可包含小写英文字母、数字和连字符，最多 32 字符。正文最多 1024 个 UTF-8 字节。

```text
/requirements set cache 使用 SQLite 缓存
/requirements set logging 保留审计日志
/requirements set cache 改用内存缓存
/requirements show cache
/requirements clear cache
/requirements
```

第二次设置 `cache` 会替换它的正文并增加版本号，`logging` 仍然有效。撤回 `cache` 后，清单保留它的已撤回状态。用 `/requirements show cache` 查看当前版本、正文和原始消息位置。把返回的 `source_message` 交给 `/history` 可查看对应的会话记录。清单较长时，用返回的 `next_offset` 继续翻页。

空闲时，命令在本地生效。发送下一条任务或运行 `/continue` 时，模型会收到当前清单。保存并恢复会话后，Sprig 仍可从原会话记录还原清单。终端连续对话也支持这些命令。

任务执行中，可以在主输入框或操作确认窗的调整框发送 `set` 或 `clear`。已接收的变更在下一处安全边界生效，尚未执行的旧提案随之失效。Sprig 会保留先前已执行的修改和命令结果，重新规划的写入和命令仍逐次确认。

需求变更会让当前的任务验收过期。用 `/verify` 重新运行已配置的固定检查。检查结果反映运行当时的工作区和检查范围。需要撤销已执行的文件修改时，可单独查看并批准[会话里的文件撤销操作](file-actions.md)。

## 给需求运行固定检查

```text
/verify cache 1 3
/requirements evidence cache
```

第一条只提议运行第 1 和第 3 条已配置的检查，分别显示实际命令并要求批准。编号从 1 开始，按当前 `[agent].checks` 或 `--check` 的顺序。第二条在本地读取最近一次检查记录，显示检查对应的需求版本、已运行检查的退出码，以及未获得结果的编号。可把 `command_message` 或 `result_message` 交给 `/history` 查看完整命令和输出。

配置了 `check_files` 时，Sprig 在运行前后对比选定文件。只有需求版本和跟踪文件仍匹配，且当前进程保留运行时记录，查询才显示 `current`。修改该需求、跟踪文件变化，或新开一轮模型任务会使对应证据过期。恢复会话后可以查看保存的历史结果。未配置跟踪文件的结果标为 `unscoped`。

单项通过描述所选命令的实际结果。用无参数的 `/verify` 运行整套固定检查。这两个命令都在本地执行，每条命令都需要新批准。
