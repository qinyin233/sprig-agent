"""Ephemeral application observations, never model context or authorization."""

from dataclasses import asdict, dataclass
from typing import Literal


Phase = Literal["starting", "preparing", "model", "steering", "tool", "approval", "question", "check", "summary", "finished"]
Outcome = Literal["running", "waiting", "approved", "rejected", "delivered", "completed", "passed",
                  "failed", "incomplete", "cancelled", "skipped", "uncertain", "unverified"]


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    run_id: str
    sequence: int
    phase: Phase
    outcome: Outcome
    step: int
    max_steps: int
    action: int = 0
    tool: str | None = None
    check: Literal["baseline", "edit-check", "acceptance", "requirement", "reproduction"] | None = None
    check_index: int = 0
    check_total: int = 0
    reason: str | None = None

    def snapshot(self):
        return asdict(self)


def tool_outcome(result):
    if result.get("answered") is False:
        return "incomplete"
    if result.get("skipped"):
        return "skipped"
    if result.get("cancelled") or result.get("interrupted"):
        return "cancelled"
    if result.get("outcome") or result.get("partial"):
        return "uncertain"
    if result.get("approved") is False:
        return "rejected"
    if ("error" in result or result.get("timed_out") or result.get("output_limited")
            or ("returncode" in result and (type(result["returncode"]) is not int or result["returncode"] != 0))):
        return "failed"
    return "completed"


def runtime_label(event, *, chinese=False):
    """Render only application-owned enums/numbers and allowlisted tool names."""
    if chinese:
        phases = {"starting": "准备任务", "preparing": "准备模型上下文", "model": "等待模型响应",
                  "steering": "新要求已加入对话", "tool": "工具", "approval": "操作批准",
                  "question": "澄清问题", "check": "检查", "summary": "会话摘要", "finished": "本轮结束"}
        outcomes = {"running": "进行中", "waiting": "等待确认", "approved": "已批准，仍需执行",
                    "rejected": "已拒绝", "delivered": "已递送", "completed": "已结束",
                    "passed": "检查通过", "failed": "失败", "incomplete": "未完成 / 未验收",
                    "cancelled": "已取消", "skipped": "未执行", "uncertain": "结果不确定",
                    "unverified": "模型本轮结束，未配置验收命令"}
        checks = {"baseline": "任务前基线", "edit-check": "编辑后检查", "acceptance": "最终验收",
                  "requirement": "单项需求检查", "reproduction": "失败复跑"}
        reason = "；会话保存失败，内存状态不代表已保存" if event.reason == "checkpoint" else ""
        budget = f"模型步骤 {event.step}/{event.max_steps}，剩余 {event.max_steps - event.step}"
    else:
        phases, outcomes, checks = {}, {}, {}
        reason = "; conversation checkpoint failed" if event.reason == "checkpoint" else ""
        budget = f"model steps {event.step}/{event.max_steps}; remaining {event.max_steps - event.step}"
    label = phases.get(event.phase, event.phase) + ": " + outcomes.get(event.outcome, event.outcome)
    if chinese and event.phase == "question" and event.outcome == "waiting":
        label = "澄清问题：等待回答"
    if event.tool:
        label += f" | #{event.action} {event.tool}"
    if event.check:
        label += f" | {checks.get(event.check, event.check)} {event.check_index}/{event.check_total}"
    return label + " | " + budget + reason
