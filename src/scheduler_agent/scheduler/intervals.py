"""Interval arithmetic on tz-aware datetimes. Pure, deterministic."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

Interval = tuple[datetime, datetime]


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    """Union of intervals, sorted, overlapping/touching ones merged (PRD 6.2: no double deduction)."""
    items = sorted((s, e) for s, e in intervals if e > s)
    out: list[Interval] = []
    for s, e in items:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def subtract(base: Interval, busy: Iterable[Interval]) -> list[Interval]:
    """base minus union(busy)."""
    s, e = base
    free: list[Interval] = []
    cur = s
    for bs, be in merge(busy):
        if be <= cur:
            continue
        if bs >= e:
            break
        if bs > cur:
            free.append((cur, bs))
        cur = max(cur, be)
    if cur < e:
        free.append((cur, e))
    return free


def total_hours(intervals: Iterable[Interval]) -> float:
    return sum((e - s).total_seconds() for s, e in intervals) / 3600


def overlaps(a: Interval, b: Interval) -> bool:
    return a[0] < b[1] and b[0] < a[1]
