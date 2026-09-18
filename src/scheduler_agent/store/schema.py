"""Bitable schema = the field contract Sara edits against (tech plan §5). Idempotent bootstrap uses it."""
from __future__ import annotations

from dataclasses import dataclass, field

# Bitable field types: 1 text, 2 number, 3 single select, 5 date(time ms), 7 checkbox, 1001 created time, 1002 modified time


@dataclass(frozen=True)
class FieldDef:
    name: str
    type: int
    options: tuple[str, ...] = ()
    formatter: str | None = None  # number formatter like "0.0"
    date_formatter: str | None = None

    def to_property(self) -> dict | None:
        if self.type == 3:
            return {"options": [{"name": o} for o in self.options]}
        if self.type == 2 and self.formatter:
            return {"formatter": self.formatter}
        if self.type == 5:
            return {"date_formatter": self.date_formatter or "yyyy/MM/dd HH:mm", "auto_fill": False}
        return None


@dataclass(frozen=True)
class TableDef:
    name: str
    fields: tuple[FieldDef, ...]
    primary: str = field(default="")  # first field is primary by construction


T = 1
NUM = 2
SEL = 3
DATE = 5
CHK = 7
MODIFIED = 1002

TASKS = TableDef("tasks", (
    FieldDef("task_id", T),
    FieldDef("title", T),
    FieldDef("status", SEL, ("inbox", "ready", "scheduled", "in_progress", "blocked", "done", "cancelled")),
    FieldDef("priority", SEL, ("P0", "P1", "P2")),
    FieldDef("source", SEL, ("老板", "PM", "同事", "自己")),
    FieldDef("parent_id", T),
    FieldDef("description", T),
    FieldDef("estimate_min", NUM, formatter="0.0"),
    FieldDef("estimate_max", NUM, formatter="0.0"),
    FieldDef("remaining_hours", NUM, formatter="0.0"),
    FieldDef("actual_hours", NUM, formatter="0.0"),
    FieldDef("requested_deadline", DATE, date_formatter="yyyy/MM/dd"),
    FieldDef("committed_deadline", DATE, date_formatter="yyyy/MM/dd"),
    FieldDef("planned_end", DATE, date_formatter="yyyy/MM/dd"),
    FieldDef("dependency", T),
    FieldDef("risk", T),
    FieldDef("assumptions", T),
    FieldDef("confidence", SEL, ("high", "medium", "low")),
    FieldDef("estimate_source", SEL, ("llm", "user_override")),
    FieldDef("sequence", NUM, formatter="0"),
    FieldDef("updated_by_agent_at", DATE),
    FieldDef("last_modified", MODIFIED),
))

TIME_BLOCKS = TableDef("time_blocks", (
    FieldDef("block_id", T),
    FieldDef("task_id", T),
    FieldDef("start", DATE),
    FieldDef("end", DATE),
    FieldDef("duration_hours", NUM, formatter="0.00"),
    FieldDef("status", SEL, ("draft", "confirmed", "done", "cancelled", "failed")),
    FieldDef("draft_id", T),
    FieldDef("calendar_id", T),
    FieldDef("event_id", T),
    FieldDef("origin", SEL, ("agent", "agent-overtime", "user")),
    FieldDef("actual_hours", NUM, formatter="0.00"),
    FieldDef("last_modified", MODIFIED),
))

DRAFTS = TableDef("drafts", (
    FieldDef("draft_id", T),
    FieldDef("kind", SEL, ("new", "reschedule")),
    FieldDef("status", SEL, ("draft", "confirmed", "partial", "expired", "failed", "cancelled")),
    FieldDef("task_ids", T),
    FieldDef("payload", T),  # JSON of blocks + replaces_block_ids
    FieldDef("gap_hours", NUM, formatter="0.0"),
    FieldDef("summary", T),
    FieldDef("created_at", DATE),
    FieldDef("expires_at", DATE),
    FieldDef("confirmed_at", DATE),
    FieldDef("last_modified", MODIFIED),
))

OPS_LOG = TableDef("ops_log", (
    FieldDef("log_id", T),
    FieldDef("time", DATE),
    FieldDef("action", T),
    FieldDef("draft_id", T),
    FieldDef("target", T),
    FieldDef("result", SEL, ("success", "partial", "failed")),
    FieldDef("detail", T),
))

EVENTS = TableDef("events", (
    FieldDef("message_id", T),
    FieldDef("received_at", DATE),
    FieldDef("handled", CHK),
))

ALL_TABLES: tuple[TableDef, ...] = (TASKS, TIME_BLOCKS, DRAFTS, OPS_LOG, EVENTS)
