"""Calendar adapter.

Read: primary-calendar free/busy (meetings) + agent-calendar instance view (our blocks).
Write: only into the agent-owned "AI 工作计划" calendar. `idempotency_key=block_id` makes
confirm retries safe (tech plan §4).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import lark_oapi as lark
from lark_oapi.api.calendar.v4 import (
    Calendar,
    CalendarEvent,
    CreateCalendarEventRequest,
    CreateCalendarRequest,
    DeleteCalendarEventRequest,
    InstanceViewCalendarEventRequest,
    ListFreebusyRequest,
    ListFreebusyRequestBody,
    PatchCalendarEventRequest,
    TimeInfo,
)

from scheduler_agent.domain.models import TimeBlock
from scheduler_agent.scheduler.intervals import Interval

from .errors import FeishuError, raise_for

AGENT_CALENDAR_SUMMARY = "AI 工作计划"


@dataclass(frozen=True)
class BusyEvent:
    start: datetime
    end: datetime
    all_day: bool = False
    event_id: str | None = None

    @property
    def interval(self) -> Interval:
        return (self.start, self.end)


def _rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("datetime must be tz-aware")
    return dt.isoformat(timespec="seconds")


def _parse_time(ti) -> tuple[datetime, bool]:
    """TimeInfo -> (aware datetime UTC, all_day)."""
    if getattr(ti, "date", None):
        d = datetime.fromisoformat(ti.date).replace(tzinfo=UTC)
        return d, True
    ts = int(ti.timestamp)
    return datetime.fromtimestamp(ts, tz=UTC), False


class CalendarAdapter:
    def __init__(self, client: lark.Client, agent_calendar_id: str | None, owner_open_id: str):
        self._c = client
        self.agent_calendar_id = agent_calendar_id
        self._owner = owner_open_id

    # ---- read ----
    def primary_busy(self, time_min: datetime, time_max: datetime) -> list[BusyEvent]:
        """Sara's primary-calendar busy intervals. Raises FeishuPermissionError instead of returning []."""
        body = ListFreebusyRequestBody.builder().time_min(_rfc3339(time_min)).time_max(_rfc3339(time_max)).user_id(self._owner).only_busy(True).build()
        req = ListFreebusyRequest.builder().user_id_type("open_id").request_body(body).build()
        resp = self._c.calendar.v4.freebusy.list(req)
        raise_for(resp.code, resp.msg)
        out: list[BusyEvent] = []
        for fb in resp.data.freebusy_list or []:
            s = datetime.fromisoformat(fb.start_time)
            e = datetime.fromisoformat(fb.end_time)
            out.append(BusyEvent(s, e))
        return out

    def agent_blocks(self, time_min: datetime, time_max: datetime) -> list[BusyEvent]:
        """Existing agent events (confirmed blocks) — freebusy does not cover our own calendar."""
        if not self.agent_calendar_id:
            return []
        req = (
            InstanceViewCalendarEventRequest.builder()
            .calendar_id(self.agent_calendar_id)
            .start_time(str(int(time_min.timestamp())))
            .end_time(str(int(time_max.timestamp())))
            .build()
        )
        resp = self._c.calendar.v4.calendar_event.instance_view(req)
        raise_for(resp.code, resp.msg)
        out: list[BusyEvent] = []
        for ev in resp.data.items or []:
            if getattr(ev, "status", None) == "cancelled":
                continue
            s, ad = _parse_time(ev.start_time)
            e, _ = _parse_time(ev.end_time)
            out.append(BusyEvent(s, e, ad, ev.event_id))
        return out

    # ---- write (agent calendar only) ----
    def ensure_agent_calendar(self) -> str:
        if self.agent_calendar_id:
            return self.agent_calendar_id
        cal = Calendar.builder().summary(AGENT_CALENDAR_SUMMARY).description("Personal Work Scheduler 自动创建的工作块日历").permissions("private").build()
        resp = self._c.calendar.v4.calendar.create(CreateCalendarRequest.builder().request_body(cal).build())
        raise_for(resp.code, resp.msg)
        self.agent_calendar_id = resp.data.calendar.calendar_id
        return self.agent_calendar_id

    def create_block_event(self, block: TimeBlock, title: str, description: str = "") -> str:
        if not self.agent_calendar_id:
            raise FeishuError("agent calendar id 未配置，请先运行 bootstrap")
        ev = (
            CalendarEvent.builder()
            .summary(title)
            .description(description)
            .start_time(TimeInfo.builder().timestamp(str(int(block.start.timestamp()))).timezone(str(block.start.tzinfo)).build())
            .end_time(TimeInfo.builder().timestamp(str(int(block.end.timestamp()))).timezone(str(block.end.tzinfo)).build())
            .free_busy_status("busy")
            .build()
        )
        req = CreateCalendarEventRequest.builder().calendar_id(self.agent_calendar_id).idempotency_key(block.block_id).request_body(ev).build()
        resp = self._c.calendar.v4.calendar_event.create(req)
        raise_for(resp.code, resp.msg)
        return resp.data.event.event_id

    def move_block_event(self, event_id: str, block: TimeBlock) -> None:
        ev = (
            CalendarEvent.builder()
            .start_time(TimeInfo.builder().timestamp(str(int(block.start.timestamp()))).timezone(str(block.start.tzinfo)).build())
            .end_time(TimeInfo.builder().timestamp(str(int(block.end.timestamp()))).timezone(str(block.end.tzinfo)).build())
            .build()
        )
        req = PatchCalendarEventRequest.builder().calendar_id(self.agent_calendar_id).event_id(event_id).request_body(ev).build()
        resp = self._c.calendar.v4.calendar_event.patch(req)
        raise_for(resp.code, resp.msg)

    def delete_block_event(self, event_id: str) -> None:
        req = DeleteCalendarEventRequest.builder().calendar_id(self.agent_calendar_id).event_id(event_id).need_notification(False).build()
        resp = self._c.calendar.v4.calendar_event.delete(req)
        if resp.code in {1254040, 1254043}:  # already gone
            return
        raise_for(resp.code, resp.msg)
