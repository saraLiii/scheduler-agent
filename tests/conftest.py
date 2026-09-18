from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from scheduler_agent.config.settings import ProtectedSlot, WorkRules

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def rules() -> WorkRules:
    return WorkRules()


@pytest.fixture
def rules_protected() -> WorkRules:
    return WorkRules(protected_slots=[ProtectedSlot(start=time(17, 0), end=time(19, 0), weekdays=[4], label="个人项目")])


def dt(day: date, h: int, m: int = 0) -> datetime:
    return datetime.combine(day, time(h, m), TZ)


# 2026-09-21 is a Monday
MON = date(2026, 9, 21)
TUE = date(2026, 9, 22)
FRI = date(2026, 9, 25)
SAT = date(2026, 9, 26)
