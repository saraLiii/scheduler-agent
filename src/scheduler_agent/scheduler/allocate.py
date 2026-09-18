"""Heuristic time-block allocation (PRD 6.4): hard constraints → priority order → fill slots.

Never invents feasibility: hours that do not fit stay in `unscheduled`. Overtime is only
considered when `allow_overtime=True` for this draft (RL07) and is reported separately.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from scheduler_agent.config.settings import WorkRules
from scheduler_agent.domain.models import Priority, Task, TimeBlock, new_id

from .capacity import DayCapacity
from .intervals import Interval

_PRIO_RANK = {Priority.P0: 0, Priority.P1: 1, Priority.P2: 2}
_FAR = date(9999, 1, 1)


def prioritize(tasks: list[Task]) -> list[Task]:
    """RL08: committed deadline, then priority, then requested deadline, then creation order."""
    return sorted(
        tasks,
        key=lambda t: (
            t.committed_deadline or _FAR,
            _PRIO_RANK[t.priority],
            t.requested_deadline or _FAR,
            t.sequence,
        ),
    )


@dataclass
class _DayState:
    cap: DayCapacity
    remaining_cap: float
    slots: list[Interval]


@dataclass(frozen=True)
class AllocationResult:
    blocks: tuple[TimeBlock, ...]
    unscheduled: dict[str, float]  # task_id -> hours not placed
    planned_end: dict[str, date]  # task_id -> last block date
    overtime_hours: float = 0.0
    notes: tuple[str, ...] = ()


def _split_hours(total: float, rules: WorkRules) -> list[float]:
    """Cut a task's hours into chunks within [min_block, max_block]; tail < min merges into previous (RL04)."""
    chunks: list[float] = []
    left = total
    while left > 0:
        c = min(rules.max_block_hours, left)
        left -= c
        if 0 < left < rules.min_block_hours and c + left <= rules.max_block_hours + rules.min_block_hours:
            c += left
            left = 0
        chunks.append(round(c, 2))
    return chunks


def allocate(
    tasks: list[Task],
    days: list[DayCapacity],
    rules: WorkRules,
    draft_id: str,
    not_before: datetime | None = None,
    allow_overtime: bool = False,
    deadline_hint: dict[str, date] | None = None,
) -> AllocationResult:
    """Place `tasks` (already filtered to ready ones) into `days` in priority order."""
    states = [_DayState(d, d.cap_hours, [s for s in d.free_slots if not_before is None or s[1] > not_before]) for d in days]
    if not_before is not None:
        for st in states:
            st.slots = [(max(s, not_before), e) for s, e in st.slots if e > max(s, not_before)]
    blocks: list[TimeBlock] = []
    unscheduled: dict[str, float] = {}
    planned_end: dict[str, date] = {}
    notes: list[str] = []

    for task in prioritize(tasks):
        need = task.remaining
        if need <= 0:
            continue
        for chunk in _split_hours(need, rules):
            placed = False
            for st in states:
                if st.remaining_cap < min(chunk, rules.min_block_hours):
                    continue
                for i, (s, e) in enumerate(st.slots):
                    slot_h = (e - s).total_seconds() / 3600
                    take = min(chunk, slot_h, st.remaining_cap)
                    if take < rules.min_block_hours and take < chunk:
                        continue
                    if take <= 0:
                        continue
                    end = s + timedelta(hours=take)
                    blocks.append(TimeBlock(new_id(), task.task_id, s, end, draft_id=draft_id))
                    st.remaining_cap = round(st.remaining_cap - take, 2)
                    rest = (end, e)
                    st.slots[i : i + 1] = [rest] if (e - end).total_seconds() / 3600 >= rules.min_block_hours else []
                    planned_end[task.task_id] = max(planned_end.get(task.task_id, date.min), st.cap.day)
                    leftover = round(chunk - take, 2)
                    if leftover > 0:
                        unscheduled[task.task_id] = unscheduled.get(task.task_id, 0.0) + leftover
                    placed = True
                    break
                if placed:
                    break
            if not placed:
                unscheduled[task.task_id] = round(unscheduled.get(task.task_id, 0.0) + chunk, 2)
    # Leftover chunks are retried once so a partially-placed chunk's remainder can find another slot.
    if unscheduled:
        retry = {tid: h for tid, h in unscheduled.items()}
        unscheduled = {}
        for task in prioritize([t for t in tasks if t.task_id in retry]):
            left = retry[task.task_id]
            for st in states:
                if left <= 0:
                    break
                for i, (s, e) in enumerate(st.slots):
                    if left <= 0 or st.remaining_cap <= 0:
                        break
                    slot_h = (e - s).total_seconds() / 3600
                    take = min(left, slot_h, st.remaining_cap, rules.max_block_hours)
                    if take < rules.min_block_hours:
                        continue
                    end = s + timedelta(hours=take)
                    blocks.append(TimeBlock(new_id(), task.task_id, s, end, draft_id=draft_id))
                    st.remaining_cap = round(st.remaining_cap - take, 2)
                    st.slots[i : i + 1] = [(end, e)] if (e - end).total_seconds() / 3600 >= rules.min_block_hours else []
                    planned_end[task.task_id] = max(planned_end.get(task.task_id, date.min), st.cap.day)
                    left = round(left - take, 2)
            if left > 0:
                unscheduled[task.task_id] = left
    if unscheduled and not allow_overtime:
        notes.append("容量不足，未排入的工时已列出；未自动使用加班时间（RL07）。")
    overtime = 0.0
    if unscheduled and allow_overtime:
        notes.append("已按 Sara 本次指令使用加班时间；加班块仅为方案展示，代价见 overtime_hours。")
        # Simple model: evening 19:00-21:00 on work days after the last used day, cap 2h/day.
        tzdays = [st.cap for st in states if st.cap.work_hours > 0]
        for cap in tzdays:
            if not unscheduled:
                break
            for tid in list(unscheduled):
                take = min(2.0, unscheduled[tid])
                if take < rules.min_block_hours:
                    take = unscheduled[tid]
                start = datetime.combine(cap.day, rules.work_hours.end, tzinfo=days[0].free_slots[0][0].tzinfo if days and days[0].free_slots else None)
                blocks.append(TimeBlock(new_id(), tid, start, start + timedelta(hours=take), draft_id=draft_id, origin="agent-overtime"))
                overtime += take
                planned_end[tid] = max(planned_end.get(tid, date.min), cap.day)
                unscheduled[tid] = round(unscheduled[tid] - take, 2)
                if unscheduled[tid] <= 0:
                    del unscheduled[tid]
                break
    blocks.sort(key=lambda b: b.start)
    return AllocationResult(tuple(blocks), unscheduled, planned_end, round(overtime, 2), tuple(notes))
