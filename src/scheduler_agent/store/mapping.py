"""Domain <-> Bitable field mapping."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from scheduler_agent.domain.models import (
    BlockStatus,
    Confidence,
    Draft,
    DraftKind,
    DraftStatus,
    Priority,
    Task,
    TaskStatus,
    TimeBlock,
)


def _ms(dt: datetime | None) -> int | None:
    return None if dt is None else int(dt.timestamp() * 1000)


def _date_ms(d: date | None, tz: ZoneInfo) -> int | None:
    return None if d is None else int(datetime.combine(d, datetime.min.time(), tz).timestamp() * 1000)


def _dt(ms: Any, tz: ZoneInfo) -> datetime | None:
    if ms in (None, "", 0):
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).astimezone(tz)


def _d(ms: Any, tz: ZoneInfo) -> date | None:
    dt = _dt(ms, tz)
    return dt.date() if dt else None


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):  # rich text segments
        return "".join(seg.get("text", "") if isinstance(seg, dict) else str(seg) for seg in v)
    return str(v)


def _num(v: Any) -> float | None:
    if v in (None, ""):
        return None
    return float(v)


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


# ---- Task ----
def task_to_fields(t: Task, tz: ZoneInfo, now: datetime) -> dict[str, Any]:
    return _clean({
        "task_id": t.task_id, "title": t.title, "status": t.status.value, "priority": t.priority.value,
        "source": t.source, "parent_id": t.parent_id or "", "description": t.description,
        "estimate_min": t.estimate_min, "estimate_max": t.estimate_max, "remaining_hours": t.remaining_hours,
        "actual_hours": t.actual_hours, "requested_deadline": _date_ms(t.requested_deadline, tz),
        "committed_deadline": _date_ms(t.committed_deadline, tz), "planned_end": _date_ms(t.planned_end, tz),
        "dependency": t.dependency, "risk": t.risk, "assumptions": t.assumptions,
        "confidence": t.confidence.value, "estimate_source": t.estimate_source, "sequence": t.sequence,
        "updated_by_agent_at": _ms(now),
    })


def fields_to_task(record_id: str, f: dict[str, Any], last_modified: int | None, tz: ZoneInfo) -> Task:
    return Task(
        task_id=_text(f.get("task_id")), title=_text(f.get("title")),
        status=TaskStatus(_text(f.get("status")) or "inbox"), priority=Priority(_text(f.get("priority")) or "P1"),
        source=_text(f.get("source")) or "自己", description=_text(f.get("description")),
        parent_id=_text(f.get("parent_id")) or None,
        estimate_min=_num(f.get("estimate_min")), estimate_max=_num(f.get("estimate_max")),
        remaining_hours=_num(f.get("remaining_hours")), actual_hours=_num(f.get("actual_hours")) or 0.0,
        requested_deadline=_d(f.get("requested_deadline"), tz), committed_deadline=_d(f.get("committed_deadline"), tz),
        planned_end=_d(f.get("planned_end"), tz),
        dependency=_text(f.get("dependency")), risk=_text(f.get("risk")), assumptions=_text(f.get("assumptions")),
        confidence=Confidence(_text(f.get("confidence")) or "medium"), estimate_source=_text(f.get("estimate_source")) or "llm",
        record_id=record_id, last_modified_ms=last_modified, sequence=int(_num(f.get("sequence")) or 0),
    )


# ---- TimeBlock ----
def block_to_fields(b: TimeBlock) -> dict[str, Any]:
    return _clean({
        "block_id": b.block_id, "task_id": b.task_id, "start": _ms(b.start), "end": _ms(b.end),
        "duration_hours": round(b.hours, 2), "status": b.status.value, "draft_id": b.draft_id or "",
        "calendar_id": b.calendar_id or "", "event_id": b.event_id or "", "origin": b.origin, "actual_hours": b.actual_hours,
    })


def fields_to_block(record_id: str, f: dict[str, Any], tz: ZoneInfo) -> TimeBlock:
    return TimeBlock(
        block_id=_text(f.get("block_id")), task_id=_text(f.get("task_id")),
        start=_dt(f.get("start"), tz), end=_dt(f.get("end"), tz), status=BlockStatus(_text(f.get("status")) or "draft"),
        draft_id=_text(f.get("draft_id")) or None, calendar_id=_text(f.get("calendar_id")) or None,
        event_id=_text(f.get("event_id")) or None, origin=_text(f.get("origin")) or "agent",
        actual_hours=_num(f.get("actual_hours")), record_id=record_id,
    )


# ---- Draft ----
def draft_to_fields(d: Draft) -> dict[str, Any]:
    payload = {
        "blocks": [{"block_id": b.block_id, "task_id": b.task_id, "start": _ms(b.start), "end": _ms(b.end), "origin": b.origin} for b in d.blocks],
        "replaces_block_ids": list(d.replaces_block_ids), "unscheduled": d.unscheduled,
    }
    return _clean({
        "draft_id": d.draft_id, "kind": d.kind.value, "status": d.status.value, "task_ids": ",".join(d.task_ids),
        "payload": json.dumps(payload, ensure_ascii=False), "gap_hours": d.gap_hours, "summary": d.summary,
        "created_at": _ms(d.created_at), "expires_at": _ms(d.expires_at),
    })


def fields_to_draft(record_id: str, f: dict[str, Any], tz: ZoneInfo) -> Draft:
    payload = json.loads(_text(f.get("payload")) or "{}")
    did = _text(f.get("draft_id"))
    blocks = tuple(
        TimeBlock(b["block_id"], b["task_id"], _dt(b["start"], tz), _dt(b["end"], tz), draft_id=did, origin=b.get("origin", "agent"))
        for b in payload.get("blocks", [])
    )
    return Draft(
        draft_id=did, kind=DraftKind(_text(f.get("kind")) or "new"),
        task_ids=tuple(x for x in _text(f.get("task_ids")).split(",") if x), blocks=blocks,
        created_at=_dt(f.get("created_at"), tz), expires_at=_dt(f.get("expires_at"), tz),
        status=DraftStatus(_text(f.get("status")) or "draft"), gap_hours=_num(f.get("gap_hours")) or 0.0,
        unscheduled=payload.get("unscheduled", {}), summary=_text(f.get("summary")),
        replaces_block_ids=tuple(payload.get("replaces_block_ids", [])), record_id=record_id,
    )
