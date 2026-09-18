from datetime import date
from zoneinfo import ZoneInfo

from scheduler_agent.domain.models import Draft, DraftKind, Task, TaskStatus, TimeBlock
from scheduler_agent.store.mapping import (
    draft_to_fields,
    fields_to_draft,
    fields_to_task,
    task_to_fields,
)
from tests.conftest import MON, dt

TZ = ZoneInfo("Asia/Shanghai")


def test_task_roundtrip():
    t = Task("id1", "T", TaskStatus.READY, estimate_min=2, estimate_max=4, remaining_hours=3,
             requested_deadline=date(2026, 9, 25), sequence=7)
    f = task_to_fields(t, TZ, dt(MON, 9))
    assert "committed_deadline" not in f  # None dropped
    back = fields_to_task("rec", f, 123, TZ)
    assert back.title == "T" and back.requested_deadline == date(2026, 9, 25) and back.remaining == 3
    assert back.sequence == 7 and back.last_modified_ms == 123 and back.committed_deadline is None


def test_draft_roundtrip():
    b = TimeBlock("b1", "t1", dt(MON, 10), dt(MON, 12), draft_id="d1")
    d = Draft("d1", DraftKind.NEW, ("t1",), (b,), dt(MON, 9), dt(MON, 9).replace(day=22),
              gap_hours=2.5, unscheduled={"t1": 2.5}, replaces_block_ids=("old",))
    back = fields_to_draft("rec", draft_to_fields(d), TZ)
    assert back.blocks[0].start == dt(MON, 10) and back.blocks[0].hours == 2
    assert back.replaces_block_ids == ("old",) and back.unscheduled == {"t1": 2.5} and back.task_ids == ("t1",)
