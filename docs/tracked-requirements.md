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

## 按需求查找文件修改

```text
/requirements work cache 把缓存从 SQLite 改为内存
/requirements changes cache
```

第一条把本轮任务关联到 `cache` 的当前版本。审批窗会显示需求名和版本。Sprig 记录这轮经批准并进入编辑备份的文件改动，包括命令通过 `backup` 显式留存的路径。中途接收新引导会结束旧归属。已经记录的改动仍可按原版本查回。

TUI 中可以先输入 `/requirements work cache `，再粘贴长任务正文。Enter 会发送完整正文。整段长粘贴仍按普通文本处理。也可以在任务正文的 `@路径` 上按 Tab，明确选择随本轮发送的完整文件快照。

第二条显示该会话关联的保留备份和原始消息位置。 `current` 显示检查时文件是否与最后的已记录状态一致，`later_recorded_change` 显示同一文件是否有更新的已应用备份。拿返回的 ID 运行 `/changes show ID` 可以查看完整历史 diff。预览在本地读取，命令在备份范围外的副作用和已淘汰的备份不会列出。

要跨进程查看，需同时保存会话和[修改备份](change-review.md)。

## 撤回某个版本的文件改动

```text
/requirements revert cache 1
/requirements revert cache 1 CHANGE_ID
```

第一条为版本 1 的保留备份生成逆向修改。第二条用 `/requirements changes cache` 返回的完整变更 ID，只选择该文件。撤回采用整文件的连续备份。文件有较新的未选中改动，或与最后记录的内容和身份不符时，Sprig 会报告冲突。尚有可重做的条目时，Sprig 会先保留这些条目并停止撤回。

Sprig 在第一次批准前核对所有待恢复文件。每个文件分别显示完整逆向 diff 并要求新批准，批准后会再次核对。取消、拒绝或后续冲突会停止剩余文件，已完成的文件会留在本地记录。明确重试会检查尚未撤回的保留备份。文件写入结果无法确认时，会保留待结算记录，供用户检查。

每次逆向编辑也进入原有备份历史，一组操作可以通过 `/undo-batch` 和 `/redo-batch` 再次审阅和恢复。新记录使用相同的容量上限。恢复缺失文件时，Windows 会使用当前用户私有 ACL。已确定被删除的路径会移出实时引用，当前验收随文件变更过期。命令在保留备份范围以外的副作用由用户单独处理。

### 保留同一文件中的独立修改

```text
/requirements revert cache 1 --merge
/requirements revert cache 1 CHANGE_ID --merge
```

显式加上 `--merge` 后，Sprig 会按备份顺序逆向比较编辑后的快照、编辑前的快照和当前文件。以三份文本中唯一且顺序一致的未变行划分片段，保留能分开的后续修改。每个合并后的文件仍展示完整逆向 diff，并在批准后重新核对当前文件。

双方修改同一片段、重复行导致定位不清或共同的行顺序变化时，Sprig 会停下来报告冲突。已记录的文件创建和删除仍沿用整文件校验。合并完成后，先前验收会过期。
