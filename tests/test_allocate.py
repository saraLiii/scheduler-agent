from datetime import timedelta

from scheduler_agent.domain.models import Priority, Task
from scheduler_agent.scheduler import allocate, capacity_report, prioritize
from tests.conftest import FRI, MON, TUE, dt


def mk(tid, hours, prio=Priority.P1, committed=None, requested=None, seq=0):
    return Task(tid, tid, priority=prio, estimate_min=hours, estimate_max=hours, remaining_hours=hours,
                committed_deadline=committed, requested_deadline=requested, sequence=seq)


def test_prioritize_order():
    ts = [mk("c", 1, Priority.P2, seq=3), mk("a", 1, Priority.P0, seq=2), mk("b", 1, Priority.P1, committed=FRI, seq=1)]
    assert [t.task_id for t in prioritize(ts)] == ["b", "a", "c"]


def test_blocks_respect_size_and_cap(rules):
    rep = capacity_report(rules, MON, TUE, [], 0)
    res = allocate([mk("t", 7)], list(rep.days), rules, "d1")
    assert not res.unscheduled
    assert all(1.0 <= b.hours <= 2.5 for b in res.blocks)
    per_day: dict = {}
    for b in res.blocks:
        per_day[b.start.date()] = per_day.get(b.start.date(), 0) + b.hours
    assert all(v <= 6 for v in per_day.values())
    assert res.planned_end["t"] == TUE


def test_no_overtime_reports_unscheduled(rules):
    rep = capacity_report(rules, MON, MON, [], 0)
    res = allocate([mk("t", 10)], list(rep.days), rules, "d1")
    assert res.unscheduled["t"] == 4
    assert res.overtime_hours == 0
    assert all(b.end.hour <= 19 for b in res.blocks)


def test_overtime_only_when_allowed(rules):
    rep = capacity_report(rules, MON, MON, [], 0)
    res = allocate([mk("t", 8)], list(rep.days), rules, "d1", allow_overtime=True)
    assert res.overtime_hours == 2
    assert not res.unscheduled


def test_blocks_avoid_meetings_and_buffer(rules):
    busy = [(dt(MON, 11), dt(MON, 12)), (dt(MON, 15), dt(MON, 16))]
    rep = capacity_report(rules, MON, MON, busy, 0)
    res = allocate([mk("t", 3)], list(rep.days), rules, "d1")
    pad = timedelta(minutes=15)
    for b in res.blocks:
        assert all(b.end <= bs - pad or b.start >= be + pad for bs, be in busy)


def test_not_before_skips_past(rules):
    rep = capacity_report(rules, MON, MON, [], 0)
    res = allocate([mk("t", 2)], list(rep.days), rules, "d1", not_before=dt(MON, 16))
    assert res.blocks[0].start >= dt(MON, 16)


def test_higher_priority_first_when_short(rules):
    rep = capacity_report(rules, MON, MON, [], 0)
    res = allocate([mk("low", 5, Priority.P2, seq=1), mk("high", 5, Priority.P0, seq=2)], list(rep.days), rules, "d1")
    assert "high" not in res.unscheduled
    assert res.unscheduled["low"] == 4
