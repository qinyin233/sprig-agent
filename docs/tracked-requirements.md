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
