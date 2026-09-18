"""Draft lifecycle: build a schedule draft, confirm it into the calendar, cancel, retry.

Confirm is idempotent at two levels: draft status (second "确认" is a no-op) and per-block
`idempotency_key=block_id` on the calendar API. Partial failure is recorded, never hidden.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from scheduler_agent.config.settings import WorkRules
from scheduler_agent.domain.models import (
    BlockStatus,
    Draft,
    DraftKind,
    DraftStatus,
    Task,
    TaskStatus,
    TimeBlock,
    new_id,
)
from scheduler_agent.feishu.calendar import CalendarAdapter
from scheduler_agent.feishu.errors import FeishuError
from scheduler_agent.scheduler import allocate, capacity_report, merge, overlaps
from scheduler_agent.store.repo import BlockRepo, DraftRepo, OpsLog, TaskRepo

log = logging.getLogger(__name__)


@dataclass
class DraftService:
    rules: WorkRules
    tz: ZoneInfo
    tasks: TaskRepo
    blocks: BlockRepo
    drafts: DraftRepo
    calendar: CalendarAdapter
    ops: OpsLog
    now: datetime | None = None

    def clock(self) -> datetime:
        return self.now or datetime.now(self.tz)

    # ---------- build ----------
    def _busy(self, t0: datetime, t1: datetime, exclude_block_ids: set[str] = frozenset()):
        meetings = self.calendar.primary_busy(t0, t1)
        days_off = {b.start.astimezone(self.tz).date() for b in meetings if b.all_day}
        agent = [b for b in self.blocks.confirmed() if b.start < t1 and b.end > t0 and b.block_id not in exclude_block_ids]
        busy = merge([m.interval for m in meetings if not m.all_day] + [(b.start, b.end) for b in agent])
        return busy, days_off

    def build(self, task_ids: list[str], until_days: int | None = None, allow_overtime: bool = False,
              kind: DraftKind = DraftKind.NEW, replaces: list[TimeBlock] | None = None) -> Draft:
        now = self.clock()
        horizon = until_days or self.rules.planning_horizon_days
        start = now.date()
        end = start + timedelta(days=horizon)
        t0 = datetime.combine(start, datetime.min.time(), self.tz)
        t1 = datetime.combine(end + timedelta(days=1), datetime.min.time(), self.tz)
        exclude = {b.block_id for b in (replaces or [])}
        busy, days_off = self._busy(t0, t1, exclude)
        all_tasks = self.tasks.list_all()
        by_id = {t.task_id: t for t in all_tasks}
        # expand parents into leaf children; leaves are what get scheduled
        leaves: list[Task] = []
        for tid in task_ids:
            kids = [t for t in all_tasks if t.parent_id == tid and t.is_active]
            leaves.extend(kids if kids else ([by_id[tid]] if tid in by_id and by_id[tid].is_active else []))
        leaves = [t for t in leaves if t.remaining > 0]
        rep = capacity_report(self.rules, start, end, busy, 0.0, days_off=days_off)
        draft_id = new_id()
        res = allocate(leaves, list(rep.days), self.rules, draft_id, not_before=now, allow_overtime=allow_overtime)
        gap = round(sum(res.unscheduled.values()), 1)
        summary = self._summary(leaves, res.blocks, res.unscheduled, res.overtime_hours, by_id)
        d = Draft(draft_id, kind, tuple(task_ids), res.blocks, now, now + timedelta(hours=self.rules.draft_ttl_hours),
                  DraftStatus.DRAFT, gap, dict(res.unscheduled), summary, tuple(exclude))
        d = self.drafts.save(d)
        self.ops.write("build_draft", "success", draft_id=d.draft_id, detail={"blocks": len(d.blocks), "gap": gap, "kind": kind.value})
        return d

    def _summary(self, leaves, blocks, unscheduled, overtime, by_id) -> str:
        lines = []
        by_day: dict = {}
        for b in blocks:
            by_day.setdefault(b.start.astimezone(self.tz).date(), []).append(b)
        for day in sorted(by_day):
            lines.append(f"{day:%m-%d %a}")
            for b in sorted(by_day[day], key=lambda x: x.start):
                t = by_id.get(b.task_id)
                name = t.title if t else b.task_id
                parent = by_id.get(t.parent_id).title if t and t.parent_id and by_id.get(t.parent_id) else None
                label = f"{parent} · {name}" if parent else name
                ot = "（加班）" if b.origin == "agent-overtime" else ""
                lines.append(f"  {b.start.astimezone(self.tz):%H:%M}–{b.end.astimezone(self.tz):%H:%M} {label}{ot}")
        if unscheduled:
            lines.append("未排入：" + "，".join(f"{by_id[t].title if t in by_id else t} {h}h" for t, h in unscheduled.items()))
        if overtime:
            lines.append(f"其中加班 {overtime}h")
        return "\n".join(lines) if lines else "（无可排时间块）"

    # ---------- confirm ----------
    def confirm(self, draft_id: str) -> tuple[Draft, str]:
        d = self.drafts.get(draft_id)
        if not d:
            return None, "找不到这个草案。"
        now = self.clock()
        if d.status in {DraftStatus.CONFIRMED}:
            return d, "该草案此前已确认，未重复写入。"
        if d.status == DraftStatus.PARTIAL:
            return self._retry_partial(d)
        if d.status != DraftStatus.DRAFT:
            return d, f"该草案状态为 {d.status.value}，无法确认。"
        if d.is_expired(now):
            d = self.drafts.save(replace(d, status=DraftStatus.EXPIRED))
            return d, "草案已过期（超过有效期），请重新生成。"
        # RL11 / R11: re-validate against the calendar right before writing
        conflicts = self._conflicts(d)
        if conflicts:
            d = self.drafts.save(replace(d, status=DraftStatus.CANCELLED))
            self.ops.write("confirm_draft", "failed", draft_id=d.draft_id, detail={"reason": "conflict", "conflicts": conflicts})
            return d, "写入前复查发现日历已有变化，以下时间块与新占用冲突，草案已作废，请让我重新排期：\n" + "\n".join(conflicts)
        return self._write(d)

    def _conflicts(self, d: Draft) -> list[str]:
        if not d.blocks:
            return []
        t0, t1 = min(b.start for b in d.blocks), max(b.end for b in d.blocks)
        busy, _ = self._busy(t0, t1, set(d.replaces_block_ids))
        out = []
        for b in d.blocks:
            for s, e in busy:
                if overlaps((b.start, b.end), (s, e)):
                    out.append(f"{b.start.astimezone(self.tz):%m-%d %H:%M}–{b.end.astimezone(self.tz):%H:%M} 与 {s.astimezone(self.tz):%H:%M}–{e.astimezone(self.tz):%H:%M} 重叠")
                    break
        return out

    def _write(self, d: Draft) -> tuple[Draft, str]:
        by_id = {t.task_id: t for t in self.tasks.list_all()}
        cal_id = self.calendar.agent_calendar_id
        # 1. cancel replaced blocks (reschedule) — delete first so freed slots can't be double-booked later
        cancelled: list[TimeBlock] = []
        if d.replaces_block_ids:
            for old in self.blocks.confirmed():
                if old.block_id in d.replaces_block_ids:
                    try:
                        if old.event_id:
                            self.calendar.delete_block_event(old.event_id)
                        cancelled.append(replace(old, status=BlockStatus.CANCELLED))
                    except FeishuError as e:
                        log.warning("delete old block %s failed: %s", old.block_id, e)
            if cancelled:
                self.blocks.update_many(cancelled)
        # 2. create new events, one by one, idempotent per block
        ok: list[TimeBlock] = []
        failed: list[tuple[TimeBlock, str]] = []
        for b in d.blocks:
            t = by_id.get(b.task_id)
            parent = by_id.get(t.parent_id) if t and t.parent_id else None
            title = f"[工作块] {parent.title + ' · ' if parent else ''}{t.title if t else b.task_id}"
            try:
                eid = self.calendar.create_block_event(replace(b, calendar_id=cal_id), title, f"task_id={b.task_id}\ndraft_id={d.draft_id}")
                ok.append(replace(b, status=BlockStatus.CONFIRMED, calendar_id=cal_id, event_id=eid))
            except FeishuError as e:
                failed.append((replace(b, status=BlockStatus.FAILED, calendar_id=cal_id), str(e)))
        # 3. persist blocks + task state
        if ok or failed:
            self.blocks.create_many(ok + [f for f, _ in failed])
        planned: dict[str, datetime] = {}
        for b in ok:
            planned[b.task_id] = max(planned.get(b.task_id, b.end), b.end)
        for tid, when in planned.items():
            t = by_id.get(tid)
            if not t:
                continue
            t2 = replace(t, planned_end=when.astimezone(self.tz).date())
            if t2.status in {TaskStatus.READY, TaskStatus.INBOX}:
                t2 = t2.transition(TaskStatus.SCHEDULED) if t2.status == TaskStatus.READY else t2
            try:
                self.tasks.save(t2, check_stale=False)
            except FeishuError as e:
                log.warning("task planned_end update failed %s: %s", tid, e)
        status = DraftStatus.CONFIRMED if not failed else (DraftStatus.PARTIAL if ok else DraftStatus.FAILED)
        d = self.drafts.save(replace(d, status=status))
        self.ops.write("confirm_draft", "success" if status == DraftStatus.CONFIRMED else status.value, draft_id=d.draft_id,
                       detail={"ok": len(ok), "failed": [(f.block_id, m) for f, m in failed], "cancelled": len(cancelled)})
        if status == DraftStatus.CONFIRMED:
            return d, f"已写入 {len(ok)} 个工作块到「AI 工作计划」日历。" + (f" 同时取消了 {len(cancelled)} 个旧块。" if cancelled else "")
        if status == DraftStatus.PARTIAL:
            lines = [f"{f.start.astimezone(self.tz):%m-%d %H:%M} {m}" for f, m in failed]
            return d, f"部分成功：{len(ok)} 个已写入，{len(failed)} 个失败。回复「重试」只补写失败的块。\n" + "\n".join(lines)
        return d, "全部写入失败：\n" + "\n".join(m for _, m in failed)

    def _retry_partial(self, d: Draft) -> tuple[Draft, str]:
        failed_blocks = [b for b in self.blocks.list_all() if b.draft_id == d.draft_id and b.status == BlockStatus.FAILED]
        if not failed_blocks:
            d = self.drafts.save(replace(d, status=DraftStatus.CONFIRMED))
            return d, "没有待补写的块，草案已标记为确认。"
        by_id = {t.task_id: t for t in self.tasks.list_all()}
        ok, still = [], []
        for b in failed_blocks:
            t = by_id.get(b.task_id)
            try:
                eid = self.calendar.create_block_event(b, f"[工作块] {t.title if t else b.task_id}", f"task_id={b.task_id}\ndraft_id={d.draft_id}")
                ok.append(replace(b, status=BlockStatus.CONFIRMED, event_id=eid))
            except FeishuError as e:
                still.append((b, str(e)))
        if ok:
            self.blocks.update_many(ok)
        status = DraftStatus.CONFIRMED if not still else DraftStatus.PARTIAL
        d = self.drafts.save(replace(d, status=status))
        self.ops.write("retry_draft", status.value, draft_id=d.draft_id, detail={"ok": len(ok), "failed": len(still)})
        return d, (f"补写成功 {len(ok)} 个块，全部完成。" if not still else f"补写 {len(ok)} 个，仍有 {len(still)} 个失败，可再回复「重试」。")

    def cancel(self, draft_id: str) -> str:
        d = self.drafts.get(draft_id)
        if not d or d.status != DraftStatus.DRAFT:
            return "没有可取消的待确认草案。"
        self.drafts.save(replace(d, status=DraftStatus.CANCELLED))
        return "草案已取消，日历未做任何改动。"
