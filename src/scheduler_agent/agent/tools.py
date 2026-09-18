"""Deterministic tools the LLM may call. Time/capacity math lives here, never in the model."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from scheduler_agent.config.settings import WorkRules
from scheduler_agent.domain.models import Confidence, Priority, Task, TaskStatus, new_id
from scheduler_agent.feishu.calendar import CalendarAdapter
from scheduler_agent.feishu.errors import FeishuPermissionError
from scheduler_agent.scheduler import capacity_report, merge
from scheduler_agent.store.repo import OpsLog, TaskRepo

from .drafts import DraftService
from .llm import LLMProvider, ToolSpec
from .prompts import DECOMPOSE_INSTRUCTIONS


@dataclass
class ToolContext:
    rules: WorkRules
    tz: ZoneInfo
    tasks: TaskRepo
    calendar: CalendarAdapter
    ops: OpsLog
    llm: LLMProvider
    drafts: DraftService | None = None
    now: datetime | None = None

    def clock(self) -> datetime:
        return self.now or datetime.now(self.tz)


def _parse_date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _task_view(t: Task) -> dict[str, Any]:
    return {
        "task_id": t.task_id, "title": t.title, "status": t.status.value, "priority": t.priority.value, "source": t.source,
        "parent_id": t.parent_id, "estimate": [t.estimate_min, t.estimate_max], "remaining_hours": t.remaining,
        "requested_deadline": t.requested_deadline.isoformat() if t.requested_deadline else None,
        "committed_deadline": t.committed_deadline.isoformat() if t.committed_deadline else None,
        "planned_end": t.planned_end.isoformat() if t.planned_end else None,
        "confidence": t.confidence.value, "risk": t.risk,
    }


SPECS: list[ToolSpec] = [
    ToolSpec("list_tasks", "列出任务。默认只列活跃任务（未完成未取消）。", {
        "type": "object", "properties": {"include_done": {"type": "boolean"}, "top_level_only": {"type": "boolean"}}}),
    ToolSpec("decompose_and_estimate", "把一段需求拆成子任务并给出区间估算（由 LLM 结构化输出，结果供 Sara 审阅，尚不入库）。", {
        "type": "object", "required": ["requirement"],
        "properties": {"requirement": {"type": "string"}, "clarifications": {"type": "string", "description": "Sara 已回答的澄清信息"}}}),
    ToolSpec("check_capacity", "计算到某日期为止的真实可用容量、已承诺负载与缺口。读取主日历忙闲，权限不足会报错而不是返回 0 会议。", {
        "type": "object", "required": ["until"],
        "properties": {"until": {"type": "string", "description": "YYYY-MM-DD，含当日"},
                       "requested_hours_min": {"type": "number"}, "requested_hours_max": {"type": "number"}}}),
    ToolSpec("create_tasks", "在 Sara 确认后把父任务与子任务写入任务表。只写 requested_deadline，不写承诺。", {
        "type": "object", "required": ["title", "subtasks"],
        "properties": {"title": {"type": "string"}, "description": {"type": "string"}, "source": {"type": "string", "enum": ["老板", "PM", "同事", "自己"]},
                       "priority": {"type": "string", "enum": ["P0", "P1", "P2"]}, "requested_deadline": {"type": "string"},
                       "assumptions": {"type": "string"}, "risk": {"type": "string"}, "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                       "subtasks": {"type": "array", "items": {"type": "object", "required": ["title", "estimate_min", "estimate_max"],
                                    "properties": {"title": {"type": "string"}, "estimate_min": {"type": "number"}, "estimate_max": {"type": "number"},
                                                   "dependency": {"type": "string"}}}}}}),
    ToolSpec("update_task_progress", "更新任务剩余工时/实际工时/状态。承诺日期只能通过 commit=true 且 Sara 明确说了承诺时写入。", {
        "type": "object", "required": ["task_id"],
        "properties": {"task_id": {"type": "string"}, "remaining_hours": {"type": "number"}, "actual_hours_delta": {"type": "number"},
                       "status": {"type": "string", "enum": ["inbox", "ready", "scheduled", "in_progress", "blocked", "done", "cancelled"]},
                       "note": {"type": "string"}, "commit_deadline": {"type": "string", "description": "YYYY-MM-DD，仅当 Sara 明确承诺"},
                       "override_estimate": {"type": "array", "items": {"type": "number"}, "description": "[min,max] Sara 覆盖估算"}}}),
]


SPECS += [
    ToolSpec("simulate_schedule", "为指定任务生成排期草案（不写日历）。返回草案 ID、逐日时间块、未排入工时。Sara 回复「确认」后才写入。", {
        "type": "object", "required": ["task_ids"],
        "properties": {"task_ids": {"type": "array", "items": {"type": "string"}, "description": "父任务或子任务 ID"},
                       "horizon_days": {"type": "integer", "description": "向后排多少天，默认配置值"},
                       "allow_overtime": {"type": "boolean", "description": "仅当 Sara 本轮明确说允许加班时为 true"}}}),
    ToolSpec("reschedule_task", "进度变化后重排某任务的未来已确认块：生成新草案并标记要替换的旧块。确认后旧块删除、新块写入。", {
        "type": "object", "required": ["task_id"],
        "properties": {"task_id": {"type": "string"}, "horizon_days": {"type": "integer"}, "allow_overtime": {"type": "boolean"}}}),
]


class Tools:
    def __init__(self, ctx: ToolContext):
        self.ctx = ctx

    def dispatch(self, name: str, args: dict[str, Any]) -> Any:
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return {"error": f"unknown tool {name}"}
        return fn(**args)

    # --- tools ---
    def t_list_tasks(self, include_done: bool = False, top_level_only: bool = False) -> dict[str, Any]:
        tasks = self.ctx.tasks.list_all() if include_done else self.ctx.tasks.list_active()
        if top_level_only:
            tasks = [t for t in tasks if not t.parent_id]
        active = [t for t in tasks if t.is_active]
        return {"count": len(tasks), "remaining_hours_total": round(sum(t.remaining for t in active), 1),
                "tasks": [_task_view(t) for t in sorted(tasks, key=lambda t: (t.parent_id or "", t.sequence))]}

    def t_decompose_and_estimate(self, requirement: str, clarifications: str = "") -> dict[str, Any]:
        prompt = f"{DECOMPOSE_INSTRUCTIONS}\n\n需求：{requirement}\n补充信息：{clarifications or '无'}"
        turn = self.ctx.llm.complete("你是资深工程负责人，只输出 JSON。", [{"role": "user", "content": prompt}], [])
        text = turn.text.strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[1] if "\n" in text else text.strip("`")
            text = text.rsplit("```", 1)[0] if "```" in text else text
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {"error": "LLM 未返回合法 JSON", "raw": text[:2000]}
        subs = data.get("subtasks", [])
        lo = round(sum(float(s.get("estimate_min", 0)) for s in subs), 1)
        hi = round(sum(float(s.get("estimate_max", 0)) for s in subs), 1)
        for s in subs:
            if float(s.get("estimate_max", 0)) < float(s.get("estimate_min", 0)):
                s["estimate_max"] = s["estimate_min"]
        data["total_estimate"] = [lo, hi]
        return data

    def t_check_capacity(self, until: str, requested_hours_min: float = 0.0, requested_hours_max: float = 0.0) -> dict[str, Any]:
        now = self.ctx.clock()
        start = now.date()
        end = _parse_date(until) or start
        if end < start:
            return {"error": f"until={until} 早于今天"}
        t0 = datetime.combine(start, datetime.min.time(), self.ctx.tz)
        t1 = datetime.combine(end + timedelta(days=1), datetime.min.time(), self.ctx.tz)
        try:
            meetings = self.ctx.calendar.primary_busy(t0, t1)
            agent_blocks = self.ctx.calendar.agent_blocks(t0, t1)
        except FeishuPermissionError as e:
            return {"error": "calendar_permission", "message": str(e), "note": "无法读取日历，不能据此判断容量。"}
        days_off = {b.start.astimezone(self.ctx.tz).date() for b in meetings if b.all_day}
        busy = merge([b.interval for b in meetings if not b.all_day] + [b.interval for b in agent_blocks])
        # today: only future part of today counts
        active = self.ctx.tasks.list_active()
        leaf = [t for t in active if not any(o.parent_id == t.task_id for o in active)]
        committed = round(sum(t.remaining for t in leaf), 1)
        rep = capacity_report(self.ctx.rules, start, end, busy, committed, (requested_hours_min, requested_hours_max), days_off)
        # subtract hours already passed today
        past_today = 0.0
        for d in rep.days:
            if d.day == start:
                for s, e in d.free_slots:
                    if e <= now:
                        past_today += (e - s).total_seconds() / 3600
                    elif s < now:
                        past_today += (now - s).total_seconds() / 3600
        available = round(max(0.0, rep.available_hours - min(past_today, rep.days[0].cap_hours if rep.days else 0)), 1)
        gap_min = round(max(0.0, committed + requested_hours_min - available), 1)
        gap_max = round(max(0.0, committed + requested_hours_max - available), 1)
        squeezed = []
        if gap_max > 0:
            acc = 0.0
            for t in sorted(leaf, key=lambda t: (t.priority.value, t.sequence), reverse=True):
                if acc >= gap_max:
                    break
                squeezed.append({"task_id": t.task_id, "title": t.title, "remaining": t.remaining})
                acc += t.remaining
        return {
            "range": [start.isoformat(), end.isoformat()], "available_hours": available, "committed_hours": committed,
            "requested_hours": [requested_hours_min, requested_hours_max], "gap_hours": [gap_min, gap_max],
            "meetings_hours": round(sum(d.busy_hours for d in rep.days), 1), "days_off": sorted(d.isoformat() for d in days_off),
            "per_day": [{"day": d.day.isoformat(), "cap": d.cap_hours, "busy": round(d.busy_hours, 1)} for d in rep.days],
            "likely_squeezed_tasks": squeezed, "overtime_allowed_by_default": self.ctx.rules.allow_overtime_by_default,
        }

    def t_create_tasks(self, title: str, subtasks: list[dict[str, Any]], description: str = "", source: str = "自己", priority: str = "P1",
                       requested_deadline: str | None = None, assumptions: str = "", risk: str = "", confidence: str = "medium") -> dict[str, Any]:
        seq = self.ctx.tasks.next_sequence()
        lo = round(sum(float(s["estimate_min"]) for s in subtasks), 1)
        hi = round(sum(float(s["estimate_max"]) for s in subtasks), 1)
        parent = Task(new_id(), title, TaskStatus.READY, Priority(priority), source, description, None, lo, hi, hi, 0.0,
                      _parse_date(requested_deadline), None, None, "", risk, assumptions, Confidence(confidence), "llm", sequence=seq)
        children = [
            Task(new_id(), s["title"], TaskStatus.READY, Priority(priority), source, "", parent.task_id, float(s["estimate_min"]), float(s["estimate_max"]),
                 float(s["estimate_max"]), 0.0, _parse_date(requested_deadline), None, None, s.get("dependency", ""), "", "", Confidence(confidence), "llm", sequence=seq + i + 1)
            for i, s in enumerate(subtasks)
        ]
        saved = self.ctx.tasks.create_many([parent] + children)
        self.ctx.ops.write("create_tasks", "success", target=parent.task_id, detail={"title": title, "children": len(children), "estimate": [lo, hi]})
        return {"parent": _task_view(saved[0]), "children": [_task_view(c) for c in saved[1:]], "note": "已写入任务表；未写入任何承诺日期。"}

    def t_update_task_progress(self, task_id: str, remaining_hours: float | None = None, actual_hours_delta: float | None = None,
                               status: str | None = None, note: str = "", commit_deadline: str | None = None,
                               override_estimate: list[float] | None = None) -> dict[str, Any]:
        t = self.ctx.tasks.get(task_id)
        if not t:
            return {"error": f"任务 {task_id} 不存在"}
        before = _task_view(t)
        if override_estimate:
            t = t.with_estimate(float(override_estimate[0]), float(override_estimate[1]), "user_override")
        if remaining_hours is not None:
            t = replace(t, remaining_hours=float(remaining_hours))
        if actual_hours_delta:
            t = replace(t, actual_hours=t.actual_hours + float(actual_hours_delta))
        if status:
            t = t.transition(TaskStatus(status))
            if t.status == TaskStatus.DONE:
                t = replace(t, remaining_hours=0.0)
        if commit_deadline:
            t = t.commit(_parse_date(commit_deadline))
        if note:
            t = replace(t, risk=(t.risk + "\n" if t.risk else "") + f"[{self.ctx.clock():%m-%d}] {note}")
        try:
            saved = self.ctx.tasks.save(t)
        except Exception as e:  # noqa: BLE001 - StaleRecordError etc.; surface to Sara
            return {"error": "stale_or_write_failed", "message": str(e), "hint": "记录可能已被 Sara 在表格中手动修改，请先复述差异再确认。"}
        # roll up parent remaining
        if saved.parent_id:
            siblings = [x for x in self.ctx.tasks.list_all() if x.parent_id == saved.parent_id]
            parent = self.ctx.tasks.get(saved.parent_id)
            if parent:
                self.ctx.tasks.save(replace(parent, remaining_hours=round(sum(x.remaining for x in siblings), 1)), check_stale=False)
        self.ctx.ops.write("update_task_progress", "success", target=task_id, detail={"before": before, "after": _task_view(saved)})
        return {"before": before, "after": _task_view(saved), "reschedule_needed": remaining_hours is not None or status in {"done", "cancelled", "blocked"}}

    # --- R2/R3: drafts ---
    def t_simulate_schedule(self, task_ids: list[str], horizon_days: int | None = None, allow_overtime: bool = False) -> dict[str, Any]:
        if not self.ctx.drafts:
            return {"error": "排期服务未初始化"}
        try:
            d = self.ctx.drafts.build(task_ids, horizon_days, allow_overtime)
        except FeishuPermissionError as e:
            return {"error": "calendar_permission", "message": str(e)}
        return {"draft_id": d.draft_id, "expires_at": d.expires_at.isoformat(), "blocks": len(d.blocks), "gap_hours": d.gap_hours,
                "unscheduled": d.unscheduled, "summary": d.summary,
                "instruction": "把 summary 原样展示给 Sara，并告诉她回复「确认」写入日历、「取消」放弃。缺口不为 0 时明确说明。"}

    def t_reschedule_task(self, task_id: str, horizon_days: int | None = None, allow_overtime: bool = False) -> dict[str, Any]:
        if not self.ctx.drafts:
            return {"error": "排期服务未初始化"}
        from scheduler_agent.domain.models import DraftKind
        now = self.ctx.clock()
        t = self.ctx.tasks.get(task_id)
        if not t:
            return {"error": f"任务 {task_id} 不存在"}
        ids = [task_id]
        children = [x for x in self.ctx.tasks.list_all() if x.parent_id == task_id]
        leaf_ids = [c.task_id for c in children] or [task_id]
        old = [b for lid in leaf_ids for b in self.ctx.drafts.blocks.future_confirmed_for(lid, now)]
        try:
            d = self.ctx.drafts.build(ids, horizon_days, allow_overtime, kind=DraftKind.RESCHEDULE, replaces=old)
        except FeishuPermissionError as e:
            return {"error": "calendar_permission", "message": str(e)}
        return {"draft_id": d.draft_id, "replaces_blocks": len(old), "blocks": len(d.blocks), "gap_hours": d.gap_hours,
                "unscheduled": d.unscheduled, "summary": d.summary,
                "instruction": "说明将取消多少旧块、新排多少块、计划完成日是否变化，并请 Sara 回复「确认」或「取消」。"}
