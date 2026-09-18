from scheduler_agent.scheduler import merge, subtract, total_hours
from tests.conftest import MON, dt


def test_merge_overlapping_and_touching():
    got = merge([(dt(MON, 9), dt(MON, 10)), (dt(MON, 9, 30), dt(MON, 11)), (dt(MON, 11), dt(MON, 12)), (dt(MON, 14), dt(MON, 15))])
    assert got == [(dt(MON, 9), dt(MON, 12)), (dt(MON, 14), dt(MON, 15))]


def test_merge_drops_empty():
    assert merge([(dt(MON, 9), dt(MON, 9))]) == []


def test_subtract_middle_and_edges():
    free = subtract((dt(MON, 10), dt(MON, 19)), [(dt(MON, 9), dt(MON, 11)), (dt(MON, 13), dt(MON, 14)), (dt(MON, 18), dt(MON, 20))])
    assert free == [(dt(MON, 11), dt(MON, 13)), (dt(MON, 14), dt(MON, 18))]
    assert total_hours(free) == 6
