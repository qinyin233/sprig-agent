# 任务前基线检查

`[agent].check_before_task = true` 可让应用在首次模型请求前运行用户已经配置的固定 `checks`。默认关闭；开启时必须配置非空检查，不扫描或执行仓库里的未知测试脚本。

```toml
[agent]
checks = [["uv", "run", "--locked", "python", "-m", "unittest", "discover", "-s", "tests", "-q"]]
check_before_task = true
```

CLI 使用 `--check-before-task` / `--no-check-before-task` 覆盖。profile 可以继承固定检查；只读 profile 应同时设置 `checks=[]` 和 `check_before_task=false`。受限项目层不能关闭可信配置已开启的基线。

每条命令仍要求新批准，并在执行前保存意图。完整运行后正常返回的非零退出码作为基线失败进入模型上下文，供其定位；这可能是环境或依赖问题，并不自动证明业务代码错误。拒绝、取消、超时、输出超限、启动或保存异常使任务停止，不联系模型。命令以当前用户权限运行，测试可能写缓存；本设置不提供 OS 沙箱。

检查期间收到的新引导保留，等待用户显式继续。`/continue` 或恢复后重新提议基线，使用新动作 ID 和新批准，不重放保存的意图。基线通过也不能证明任务完成；模型宣布完成后，应用仍独立运行固定验收。

`/status` 显示本次基线状态：`not_run`、`running`、`passed`、`failed`、`blocked`、`incomplete`、`steered`、`cancelled` 或 `error`。启用会话保存时，普通未加密的本地历史包含检查结果；开关和固定命令绑定会话。

三个基准入口 `scripts/benchmark-tasks.py`、`scripts/benchmark-repository-task.py` 和 `benchmarks/clients/mini_task.py` 都接受 `--check-before-task`，报告保留开关和状态。mini 另保留实际送入 SDK 的 `model_task_sha256`。启用增加了模型可见信息、执行时间和审批，不能与未启用的历史运行声称输入完全相同。固定回复回归证明流程行为，不证明真实编码能力提升。
