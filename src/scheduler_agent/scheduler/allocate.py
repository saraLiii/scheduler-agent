"""Heuristic time-block allocation (PRD 6.4): hard constraints → priority order → fill slots.

Never invents feasibility: hours that do not fit stay in `unscheduled`. Overtime is only
considered when `allow_overtime=True` for this draft (RL07) and is reported separately.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

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


def _hours(s: datetime, e: datetime) -> float:
    return (e - s).total_seconds() / 3600


def _take_size(left: float, avail: float, rules: WorkRules) -> float:
    """How much of `left` to put in a slot with `avail` hours, respecting RL04 block bounds.

    Avoids orphaning a remainder smaller than min_block: absorb it into this block when the
    slot allows, otherwise shrink this block so the remainder is at least min_block.
    """
    take = min(left, rules.max_block_hours, avail)
    rem = left - take
    if 0 < rem < rules.min_block_hours:
        if left <= avail and left <= rules.max_block_hours + rules.min_block_hours:
            take = left
        elif take - (rules.min_block_hours - rem) >= rules.min_block_hours:
            take -= rules.min_block_hours - rem
    return round(take, 2)


def allocate(
    tasks: list[Task],
    days: list[DayCapacity],
    rules: WorkRules,
    draft_id: str,
    not_before: datetime | None = None,
    allow_overtime: bool = False,
) -> AllocationResult:
    """Place `tasks` (already filtered to ready leaves) into `days` in priority order."""
    states: list[_DayState] = []
    for d in days:
        slots = []
        for s, e in d.free_slots:
            if not_before is not None:
                s = max(s, not_before)
            if _hours(s, e) >= rules.min_block_hours:
                slots.append((s, e))
        states.append(_DayState(d, d.cap_hours, slots))

    blocks: list[TimeBlock] = []
    unscheduled: dict[str, float] = {}
    planned_end: dict[str, date] = {}
    notes: list[str] = []

    for task in prioritize(tasks):
        left = round(task.remaining, 2)
        while left > 0:
            placed = False
            for st in states:
                if st.remaining_cap < rules.min_block_hours:
                    continue
                for i, (s, e) in enumerate(st.slots):
                    avail = min(_hours(s, e), st.remaining_cap)
                    if avail < rules.min_block_hours:
                        continue
                    take = _take_size(left, avail, rules)
                    if take < rules.min_block_hours:
                        continue
                    end = s + timedelta(hours=take)
                    blocks.append(TimeBlock(new_id(), task.task_id, s, end, draft_id=draft_id))
                    st.remaining_cap = round(st.remaining_cap - take, 2)
                    st.slots[i : i + 1] = [(end, e)] if _hours(end, e) >= rules.min_block_hours else []
                    planned_end[task.task_id] = max(planned_end.get(task.task_id, date.min), st.cap.day)
                    left = round(left - take, 2)
                    placed = True
                    break
                if placed:
                    break
            if not placed:
                unscheduled[task.task_id] = left
                break

    overtime = 0.0
    if unscheduled and not allow_overtime:
        notes.append("容量不足，未排入的工时已列出；未自动使用加班时间（RL07）。")
    if unscheduled and allow_overtime:
        notes.append("已按 Sara 本次指令使用加班时间；加班块仅为方案展示，代价见 overtime_hours。")
        tz = ZoneInfo(rules.timezone)
        # Simple model: up to 2h after work_hours.end on each work day, in priority order.
        for st in states:
            if not unscheduled:
                break
            if st.cap.work_hours <= 0:
                continue
            start = datetime.combine(st.cap.day, rules.work_hours.end, tz)
            if not_before is not None and start + timedelta(hours=rules.min_block_hours) <= not_before:
                continue
            room = 2.0
            for task in prioritize([t for t in tasks if t.task_id in unscheduled]):
                if room < rules.min_block_hours:
                    break
                take = min(unscheduled[task.task_id], room)
                if take < rules.min_block_hours and take < unscheduled[task.task_id]:
                    continue
                blocks.append(TimeBlock(new_id(), task.task_id, start, start + timedelta(hours=take), draft_id=draft_id, origin="agent-overtime"))
                start += timedelta(hours=take)
                room = round(room - take, 2)
                overtime += take
                planned_end[task.task_id] = max(planned_end.get(task.task_id, date.min), st.cap.day)
                unscheduled[task.task_id] = round(unscheduled[task.task_id] - take, 2)
                if unscheduled[task.task_id] <= 0:
                    del unscheduled[task.task_id]
    blocks.sort(key=lambda b: b.start)
    return AllocationResult(tuple(blocks), unscheduled, planned_end, round(overtime, 2), tuple(notes))
