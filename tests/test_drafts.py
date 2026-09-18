"""R2/R3: draft build → confirm → partial failure/retry → reschedule, with fakes (no network)."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from scheduler_agent.agent.drafts import DraftService
from scheduler_agent.agent.orchestrator import Orchestrator
from scheduler_agent.agent.tools import ToolContext, Tools
from scheduler_agent.config.settings import WorkRules
from scheduler_agent.domain.models import BlockStatus, DraftStatus, TaskStatus
from scheduler_agent.feishu.calendar import BusyEvent
from scheduler_agent.feishu.errors import FeishuError
from tests.conftest import MON, dt
from tests.test_tools_fake import FakeCalendar, FakeOps, FakeTaskRepo, ScriptedLLM

TZ = ZoneInfo("Asia/Shanghai")


class FakeBlockRepo:
    def __init__(self):
        self.rows = []

    def list_all(self):
        return list(self.rows)

    def confirmed(self):
        return [b for b in self.rows if b.status == BlockStatus.CONFIRMED]

    def future_confirmed_for(self, task_id, after):
        return [b for b in self.confirmed() if b.task_id == task_id and b.start >= after]

    def create_many(self, blocks):
        out = [replace(b, record_id=f"blk{i}") for i, b in enumerate(blocks, len(self.rows))]
        self.rows.extend(out)
        return out

    def update_many(self, blocks):
        ids = {b.block_id: b for b in blocks}
        self.rows = [ids.get(b.block_id, b) for b in self.rows]


class FakeDraftRepo:
    def __init__(self):
        self.rows = {}
        self._cache = self.rows

    def save(self, d):
        d = d if d.record_id else replace(d, record_id=f"d{len(self.rows)}")
        self.rows[d.draft_id] = d
        return d

    def get(self, did):
        return self.rows.get(did)

    def load_open(self):
        return [d for d in self.rows.values() if d.status == DraftStatus.DRAFT]

    def latest_open(self):
        o = self.load_open()
        return max(o, key=lambda d: d.created_at) if o else None

    def latest_partial(self):
        o = [d for d in self.rows.values() if d.status == DraftStatus.PARTIAL]
        return max(o, key=lambda d: d.created_at) if o else None


class WritingCalendar(FakeCalendar):
    """Records created events; can be told to fail the Nth create."""

    def __init__(self, busy=None):
        super().__init__(busy)
        self.agent_calendar_id = "cal_agent"
        self.created, self.deleted = {}, []
        self.fail_on = set()
        self._n = 0

    def agent_blocks(self, a, b):
        return [BusyEvent(s, e, event_id=eid) for eid, (s, e) in self.created.items()]

    def create_block_event(self, block, title, description=""):
        self._n += 1
        if self._n in self.fail_on:
            raise FeishuError("simulated 500", 500)
        if block.block_id in self.created:  # idempotency_key semantics
            return f"ev_{block.block_id}"
        self.created[f"ev_{block.block_id}"] = (block.start, block.end)
        return f"ev_{block.block_id}"

    def delete_block_event(self, event_id):
        self.created.pop(event_id, None)
        self.deleted.append(event_id)


@pytest.fixture
def svc():
    tasks, blocks, drafts, cal, ops = FakeTaskRepo(), FakeBlockRepo(), FakeDraftRepo(), WritingCalendar(), FakeOps()
    s = DraftService(WorkRules(), TZ, tasks, blocks, drafts, cal, ops, now=dt(MON, 9))
    llm = ScriptedLLM([])
    tools = Tools(ToolContext(WorkRules(), TZ, tasks, cal, ops, llm, s, now=dt(MON, 9)))
    return s, tools, tasks, blocks, drafts, cal, llm


def _seed(tools, hours=(3, 4)):
    r = tools.t_create_tasks("审核改造", [{"title": f"s{i}", "estimate_min": h, "estimate_max": h} for i, h in enumerate(hours)], requested_deadline="2026-09-25")
    return r["parent"]["task_id"], [c["task_id"] for c in r["children"]]


def test_build_and_confirm_writes_calendar_and_marks_scheduled(svc):
    s, tools, tasks, blocks, drafts, cal, _ = svc
    pid, kids = _seed(tools)
    res = tools.t_simulate_schedule([pid])
    assert res["gap_hours"] == 0 and res["blocks"] >= 3
    d, msg = s.confirm(res["draft_id"])
    assert d.status == DraftStatus.CONFIRMED and "已写入" in msg
    assert len(cal.created) == res["blocks"]
    assert all(b.status == BlockStatus.CONFIRMED and b.event_id for b in blocks.rows)
    for k in kids:
        t = tasks.get(k)
        assert t.status == TaskStatus.SCHEDULED and t.planned_end is not None


def test_confirm_twice_is_noop(svc):
    s, tools, *_ , cal, _ = svc
    pid, _ = _seed(tools)
    did = tools.t_simulate_schedule([pid])["draft_id"]
    s.confirm(did)
    n = len(cal.created)
    d, msg = s.confirm(did)
    assert len(cal.created) == n and "已确认" in msg


def test_expired_draft_rejected(svc):
    s, tools, *_ = svc
    pid, _ = _seed(tools)
    did = tools.t_simulate_schedule([pid])["draft_id"]
    s.now = dt(MON, 9) + timedelta(hours=25)
    d, msg = s.confirm(did)
    assert d.status == DraftStatus.EXPIRED and "过期" in msg


def test_conflict_detected_before_write(svc):
    s, tools, tasks, blocks, drafts, cal, _ = svc
    pid, _ = _seed(tools, hours=(2,))
    res = tools.t_simulate_schedule([pid])
    first = drafts.get(res["draft_id"]).blocks[0]
    cal.busy = [BusyEvent(first.start, first.end)]  # Sara added a meeting on top
    d, msg = s.confirm(res["draft_id"])
    assert d.status == DraftStatus.CANCELLED and "冲突" in msg and not cal.created


def test_partial_failure_visible_then_retry(svc):
    s, tools, tasks, blocks, drafts, cal, _ = svc
    pid, _ = _seed(tools, hours=(5,))
    res = tools.t_simulate_schedule([pid])
    assert res["blocks"] >= 2
    cal.fail_on = {2}
    d, msg = s.confirm(res["draft_id"])
    assert d.status == DraftStatus.PARTIAL and "失败" in msg and "重试" in msg
    assert sum(1 for b in blocks.rows if b.status == BlockStatus.FAILED) == 1
    d, msg = s.confirm(res["draft_id"])  # retry path
    assert d.status == DraftStatus.CONFIRMED and all(b.status == BlockStatus.CONFIRMED for b in blocks.rows)


def test_no_overtime_and_gap_reported(svc):
    s, tools, *_ = svc
    pid, _ = _seed(tools, hours=(30,))
    res = tools.t_simulate_schedule([pid], horizon_days=1)
    assert res["gap_hours"] > 0 and "未排入" in res["summary"]
    assert "加班" not in res["summary"]
    res2 = tools.t_simulate_schedule([pid], horizon_days=1, allow_overtime=True)
    assert "加班" in res2["summary"] and res2["gap_hours"] < res["gap_hours"]


def test_reschedule_replaces_future_blocks_only(svc):
    s, tools, tasks, blocks, drafts, cal, _ = svc
    pid, kids = _seed(tools, hours=(4,))
    did = tools.t_simulate_schedule([pid])["draft_id"]
    s.confirm(did)
    old = sorted(blocks.confirmed(), key=lambda b: b.start)
    assert len(old) == 2
    # time passes: first block is done, second is in the future
    s.now = old[0].end + timedelta(minutes=5)
    tools.ctx.now = s.now
    tools.t_update_task_progress(kids[0], remaining_hours=6, note="DB 复杂")
    res = tools.t_reschedule_task(pid)
    assert res["replaces_blocks"] == 1
    d, msg = s.confirm(res["draft_id"])
    assert d.status == DraftStatus.CONFIRMED and "取消了 1 个旧块" in msg
    assert old[1].event_id in cal.deleted and old[0].event_id not in cal.deleted
    total_future = sum(b.hours for b in blocks.confirmed() if b.start >= s.now)
    assert total_future == 6


def test_done_task_reschedule_cancels_future_blocks(svc):
    s, tools, tasks, blocks, drafts, cal, _ = svc
    pid, kids = _seed(tools, hours=(4,))
    s.confirm(tools.t_simulate_schedule([pid])["draft_id"])
    tools.t_update_task_progress(kids[0], status="done")
    res = tools.t_reschedule_task(pid)
    assert res["replaces_blocks"] == 2 and res["blocks"] == 0
    d, _ = s.confirm(res["draft_id"])
    assert not blocks.confirmed() and len(cal.deleted) == 2


def test_orchestrator_confirm_word_routes_to_service(svc):
    s, tools, tasks, blocks, drafts, cal, llm = svc
    orch = Orchestrator(llm, tools)
    assert "没有待确认" in orch.handle("u", "确认")
    pid, _ = _seed(tools)
    tools.t_simulate_schedule([pid])
    out = orch.handle("u", "确认")
    assert "已写入" in out and cal.created and llm.calls == []
    assert "没有待确认" in orch.handle("u", "取消")
