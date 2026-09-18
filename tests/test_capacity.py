from datetime import date, time

from scheduler_agent.config.settings import TimeRange, WorkRules
from scheduler_agent.scheduler import capacity_report, day_capacity, work_window
from tests.conftest import FRI, MON, SAT, dt


def test_work_window_excludes_lunch(rules):
    assert work_window(rules, MON) == [(dt(MON, 10), dt(MON, 12, 30)), (dt(MON, 14), dt(MON, 19))]


def test_weekend_zero_capacity(rules):
    assert work_window(rules, SAT) == []
    assert day_capacity(rules, SAT, []).cap_hours == 0


def test_protected_slot_removed(rules_protected):
    assert work_window(rules_protected, FRI)[-1] == (dt(FRI, 14), dt(FRI, 17))


def test_prd_example_8h_2h_meeting_1h_fixed_1h_buffer():
    """PRD 6.2: 8h work, 2h meetings, 1h fixed, 1h buffer -> 4h."""
    r = WorkRules(work_hours=TimeRange(start=time(9), end=time(17)), lunch=None, daily_task_cap_hours=8,
                  daily_buffer_hours=1, meeting_buffer_minutes=0)
    busy = [(dt(MON, 10), dt(MON, 12)), (dt(MON, 15), dt(MON, 16))]
    c = day_capacity(r, MON, busy)
    assert (c.work_hours, c.busy_hours, c.raw_free_hours, c.cap_hours) == (8, 3, 4, 4)


def test_overlapping_meetings_not_double_counted(rules):
    c = day_capacity(rules, MON, [(dt(MON, 10), dt(MON, 12)), (dt(MON, 11), dt(MON, 12, 30))])
    assert c.busy_hours == 2.5


def test_daily_cap_limits(rules):
    # 7.5h window - 1h buffer = 6.5, capped to 6
    assert day_capacity(rules, MON, []).cap_hours == 6


def test_meeting_buffer_shrinks_slots(rules):
    c = day_capacity(rules, MON, [(dt(MON, 15), dt(MON, 16))])
    assert (dt(MON, 14), dt(MON, 14, 45)) not in c.free_slots  # < 1h dropped
    assert (dt(MON, 16, 15), dt(MON, 19)) in c.free_slots


def test_all_day_off_zero(rules):
    r = capacity_report(rules, MON, date(2026, 9, 22), [], committed_hours=0, days_off={MON})
    assert r.days[0].cap_hours == 0 and r.days[1].cap_hours == 6


def test_gap_computation(rules):
    r = capacity_report(rules, MON, FRI, [], committed_hours=13, requested=(14, 22))
    assert r.available_hours == 30
    assert (r.gap_min, r.gap_max) == (0, 5)
