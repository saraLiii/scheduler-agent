"""R1 integration-style tests with fakes: tools + orchestrator without Feishu/Anthropic network."""
from __future__ import annotations

import json
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from scheduler_agent.agent.llm import LLMTurn, LLMUnavailable, ToolCall
from scheduler_agent.agent.orchestrator import Orchestrator
from scheduler_agent.agent.tools import ToolContext, Tools
from scheduler_agent.config.settings import WorkRules
from scheduler_agent.feishu.calendar import BusyEvent
from scheduler_agent.feishu.errors import FeishuPermissionError
from scheduler_agent.store.repo import StaleRecordError
from tests.conftest import MON, TUE, dt

TZ = ZoneInfo("Asia/Shanghai")


class FakeTaskRepo:
    def __init__(self):
        self.rows = []
        self.stale_on_save = False

    def list_all(self):
        return list(self.rows)

    def list_active(self):
        return [t for t in self.rows if t.is_active]

    def get(self, tid):
        return next((t for t in self.rows if t.task_id == tid), None)

    def next_sequence(self):
        return max((t.sequence for t in self.rows), default=0) + 1

    def create_many(self, tasks):
        out = [replace(t, record_id=f"rec{i}", last_modified_ms=1) for i, t in enumerate(tasks, len(self.rows))]
        self.rows.extend(out)
        return out

    def save(self, task, check_stale=True):
        if check_stale and self.stale_on_save:
            raise StaleRecordError("edited by hand")
        self.rows = [task if t.task_id == task.task_id else t for t in self.rows]
        return task


class FakeCalendar:
    def __init__(self, busy=None, deny=False):
        self.busy, self.deny = busy or [], deny

    def primary_busy(self, a, b):
        if self.deny:
            raise FeishuPermissionError("missing scope calendar:calendar:readonly", 99991672)
        return self.busy

    def agent_blocks(self, a, b):
        return []


class FakeOps:
    def __init__(self):
        self.entries = []

    def write(self, action, result, target="", draft_id="", detail=None):
        self.entries.append((action, result, target))


class ScriptedLLM:
    """Returns pre-scripted turns; records what it was asked."""

    def __init__(self, turns):
        self.turns, self.calls = list(turns), []

    def complete(self, system, messages, tools):
        self.calls.append((system, messages, tools))
        if not self.turns:
            raise RuntimeError("no more scripted turns")
        return self.turns.pop(0)


def text_turn(t):
    return LLMTurn(t, (), "end_turn", [{"type": "text", "text": t}])


def tool_turn(name, args, cid="c1"):
    return LLMTurn("", (ToolCall(cid, name, args),), "tool_use", [{"type": "tool_use", "id": cid, "name": name, "input": args}])


@pytest.fixture
def ctx():
    repo, cal, ops = FakeTaskRepo(), FakeCalendar(), FakeOps()
    llm = ScriptedLLM([])
    c = ToolContext(WorkRules(), TZ, repo, cal, ops, llm, now=dt(MON, 9))
    return c, repo, cal, ops, llm


def test_create_tasks_writes_parent_children_no_commit(ctx):
    c, repo, *_ = ctx
    tools = Tools(c)
    res = tools.t_create_tasks(
        "审核改造",
        [{"title": "方案", "estimate_min": 2, "estimate_max": 3}, {"title": "开发", "estimate_min": 6, "estimate_max": 9}],
        source="老板", requested_deadline="2026-09-25",
    )
    assert res["parent"]["estimate"] == [8, 12]
    assert res["parent"]["requested_deadline"] == "2026-09-25"
    assert res["parent"]["committed_deadline"] is None  # RL10
    assert len(repo.rows) == 3 and all(ch["parent_id"] == res["parent"]["task_id"] for ch in res["children"])


def test_check_capacity_counts_committed_and_gap(ctx):
    c, repo, cal, *_ = ctx
    tools = Tools(c)
    tools.t_create_tasks("已有任务", [{"title": "a", "estimate_min": 10, "estimate_max": 10}])
    cal.busy = [BusyEvent(dt(MON, 15), dt(MON, 16))]
    res = tools.t_check_capacity("2026-09-22", 8, 8)
    assert res["committed_hours"] == 10  # leaf only, parent not double counted
    assert res["meetings_hours"] == 1
    assert res["available_hours"] > 0
    assert res["gap_hours"][0] == round(10 + 8 - res["available_hours"], 1)
    assert res["likely_squeezed_tasks"][0]["title"] == "a"


def test_check_capacity_permission_error_is_explicit(ctx):
    c, repo, cal, *_ = ctx
    cal.deny = True
    res = Tools(c).t_check_capacity("2026-09-22")
    assert res["error"] == "calendar_permission" and "available_hours" not in res


def test_all_day_event_zeroes_day(ctx):
    c, repo, cal, *_ = ctx
    cal.busy = [BusyEvent(dt(MON, 0), dt(TUE, 0), all_day=True)]
    res = Tools(c).t_check_capacity("2026-09-22")
    assert res["days_off"] == ["2026-09-21"] and res["per_day"][0]["cap"] == 0 and res["available_hours"] == 6


def test_update_progress_rollup_and_stale(ctx):
    c, repo, *_ = ctx
    tools = Tools(c)
    r = tools.t_create_tasks("父", [{"title": "s1", "estimate_min": 2, "estimate_max": 4}, {"title": "s2", "estimate_min": 2, "estimate_max": 4}])
    s1 = r["children"][0]["task_id"]
    out = tools.t_update_task_progress(s1, remaining_hours=10, note="DB 比想象复杂")
    assert out["after"]["remaining_hours"] == 10 and out["reschedule_needed"]
    assert repo.get(r["parent"]["task_id"]).remaining == 14
    repo.stale_on_save = True
    out2 = tools.t_update_task_progress(s1, remaining_hours=3)
    assert out2["error"] == "stale_or_write_failed"


def test_commit_only_via_explicit_flag(ctx):
    c, repo, *_ = ctx
    tools = Tools(c)
    r = tools.t_create_tasks("t", [{"title": "s", "estimate_min": 1, "estimate_max": 1}], requested_deadline="2026-09-25")
    tid = r["parent"]["task_id"]
    assert tools.t_update_task_progress(tid, note="老板希望周五")["after"]["committed_deadline"] is None
    assert tools.t_update_task_progress(tid, commit_deadline="2026-09-26")["after"]["committed_deadline"] == "2026-09-26"


def test_decompose_parses_json_and_sums(ctx):
    c, *_, llm = ctx
    llm.turns = [text_turn('```json\n{"title":"x","subtasks":[{"title":"a","estimate_min":2,"estimate_max":3},{"title":"b","estimate_min":4,"estimate_max":3}],"confidence":"low"}\n```')]
    res = Tools(c).t_decompose_and_estimate("做审核改造")
    assert res["total_estimate"] == [6, 6]  # b's max clamped to min
    assert res["subtasks"][1]["estimate_max"] == 4


def test_orchestrator_loop_runs_tool_then_answers(ctx):
    c, repo, *_, llm = ctx
    tools = Tools(c)
    llm.turns = [tool_turn("list_tasks", {}), text_turn("你当前没有任务。")]
    orch = Orchestrator(llm, tools)
    assert orch.handle("u", "我手上还有什么事？") == "你当前没有任务。"
    msgs = llm.calls[1][1]
    assert msgs[-1]["content"][0]["type"] == "tool_result"
    assert json.loads(msgs[-1]["content"][0]["content"])["count"] == 0


def test_reserved_word_bypasses_llm(ctx):
    c, repo, *_, llm = ctx
    tools = Tools(c)
    tools.t_create_tasks("审核改造", [{"title": "开发", "estimate_min": 5, "estimate_max": 5}])
    out = Orchestrator(llm, tools).handle("u", "列任务")
    assert "审核改造" in out and llm.calls == []


def test_llm_failure_surfaces_and_history_clean(ctx):
    c, repo, *_, llm = ctx
    orch = Orchestrator(llm, Tools(c))
    with pytest.raises(LLMUnavailable):
        orch.handle("u", "hi")
    assert len(orch.session("u").history) == 0
