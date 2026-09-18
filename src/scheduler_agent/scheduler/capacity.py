"""Capacity = work time − meetings − fixed occupancy − buffer, per day (PRD 6.2).

Inputs are explicit so the LLM never computes time. `busy` should already include
meetings from the primary calendar AND confirmed agent blocks (from the blocks table
and the agent calendar), merged once so nothing is deducted twice.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from scheduler_agent.config.settings import WorkRules

from .intervals import Interval, merge, subtract, total_hours


@dataclass(frozen=True)
class DayCapacity:
    day: date
    work_hours: float
    busy_hours: float  # meetings + fixed occupancy inside work hours (merged)
    buffer_hours: float
    cap_hours: float  # min(daily cap, remaining) — hours actually assignable to tasks
    free_slots: tuple[Interval, ...]  # slots >= min block, after meeting buffers

    @property
    def raw_free_hours(self) -> float:
        return max(0.0, self.work_hours - self.busy_hours - self.buffer_hours)


def _tz(rules: WorkRules) -> ZoneInfo:
    return ZoneInfo(rules.timezone)


def work_window(rules: WorkRules, day: date) -> list[Interval]:
    """Work intervals for a day: work_hours minus lunch minus protected slots. Empty on non-work days."""
    if day.weekday() not in rules.work_days:
        return []
    tz = _tz(rules)
    base: Interval = (
        datetime.combine(day, rules.work_hours.start, tz),
        datetime.combine(day, rules.work_hours.end, tz),
    )
    fixed: list[Interval] = []
    if rules.lunch:
        fixed.append((datetime.combine(day, rules.lunch.start, tz), datetime.combine(day, rules.lunch.end, tz)))
    for p in rules.protected_slots:
        if day.weekday() in p.weekdays:
            fixed.append((datetime.combine(day, p.start, tz), datetime.combine(day, p.end, tz)))
    return subtract(base, fixed)


def _pad(intervals: list[Interval], minutes: int) -> list[Interval]:
    d = timedelta(minutes=minutes)
    return [(s - d, e + d) for s, e in intervals]


def day_capacity(rules: WorkRules, day: date, busy: list[Interval], all_day_off: bool = False) -> DayCapacity:
    windows = work_window(rules, day)
    if not windows or all_day_off:
        return DayCapacity(day, 0.0, 0.0, 0.0, 0.0, ())
    work_h = total_hours(windows)
    busy_m = merge(busy)
    # busy hours counted only where they intersect work windows
    busy_in_work = 0.0
    for ws, we in windows:
        for bs, be in busy_m:
            lo, hi = max(ws, bs), min(we, be)
            if hi > lo:
                busy_in_work += (hi - lo).total_seconds() / 3600
    # free slots after meeting buffers (RL05)
    padded = _pad(busy_m, rules.meeting_buffer_minutes)
    slots: list[Interval] = []
    for w in windows:
        for s, e in subtract(w, padded):
            if (e - s).total_seconds() / 3600 >= rules.min_block_hours:
                slots.append((s, e))
    raw_free = max(0.0, work_h - busy_in_work - rules.daily_buffer_hours)
    cap = min(rules.daily_task_cap_hours, raw_free, total_hours(slots))
    return DayCapacity(day, work_h, busy_in_work, rules.daily_buffer_hours, round(cap, 2), tuple(slots))


@dataclass(frozen=True)
class CapacityReport:
    start: date
    end: date  # inclusive
    days: tuple[DayCapacity, ...]
    committed_hours: float  # remaining hours of tasks already committed/scheduled
    available_hours: float  # sum of day caps
    requested_hours_min: float = 0.0
    requested_hours_max: float = 0.0

    @property
    def gap_min(self) -> float:
        return round(max(0.0, self.committed_hours + self.requested_hours_min - self.available_hours), 2)

    @property
    def gap_max(self) -> float:
        return round(max(0.0, self.committed_hours + self.requested_hours_max - self.available_hours), 2)


def capacity_report(
    rules: WorkRules,
    start: date,
    end: date,
    busy: list[Interval],
    committed_hours: float,
    requested: tuple[float, float] = (0.0, 0.0),
    days_off: set[date] | None = None,
) -> CapacityReport:
    days: list[DayCapacity] = []
    d = start
    while d <= end:
        days.append(day_capacity(rules, d, busy, all_day_off=bool(days_off and d in days_off)))
        d += timedelta(days=1)
    avail = round(sum(x.cap_hours for x in days), 2)
    return CapacityReport(start, end, tuple(days), committed_hours, avail, requested[0], requested[1])
