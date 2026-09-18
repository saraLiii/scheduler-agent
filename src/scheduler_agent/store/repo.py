"""Repositories over Bitable — the only source of truth (D01). In-memory caches are accelerators."""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from scheduler_agent.domain.models import (
    BlockStatus,
    Draft,
    DraftStatus,
    Task,
    TimeBlock,
    new_id,
)

from .bitable import BitableGateway, StaleRecordError
from .mapping import (
    block_to_fields,
    draft_to_fields,
    fields_to_block,
    fields_to_draft,
    fields_to_task,
    task_to_fields,
)

log = logging.getLogger(__name__)


def _now(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


class TaskRepo:
    def __init__(self, gw: BitableGateway, tz: ZoneInfo):
        self._gw, self._tz = gw, tz

    def list_all(self) -> list[Task]:
        return [fields_to_task(r.record_id, r.fields, r.last_modified_ms, self._tz) for r in self._gw.search("tasks")]

    def list_active(self) -> list[Task]:
        return [t for t in self.list_all() if t.is_active]

    def get(self, task_id: str) -> Task | None:
        for t in self.list_all():
            if t.task_id == task_id:
                return t
        return None

    def next_sequence(self) -> int:
        seqs = [t.sequence for t in self.list_all()]
        return (max(seqs) + 1) if seqs else 1

    def create_many(self, tasks: list[Task]) -> list[Task]:
        now = _now(self._tz)
        recs = self._gw.batch_create("tasks", [task_to_fields(t, self._tz, now) for t in tasks])
        return [replace(t, record_id=r.record_id, last_modified_ms=r.last_modified_ms) for t, r in zip(tasks, recs)]

    def save(self, task: Task, check_stale: bool = True) -> Task:
        """RL13: re-read before write. Raises StaleRecordError when Sara edited the row meanwhile."""
        if not task.record_id:
            return self.create_many([task])[0]
        expect = task.last_modified_ms if check_stale else None
        r = self._gw.update("tasks", task.record_id, task_to_fields(task, self._tz, _now(self._tz)), expect_last_modified=expect)
        return replace(task, last_modified_ms=r.last_modified_ms)


class BlockRepo:
    def __init__(self, gw: BitableGateway, tz: ZoneInfo):
        self._gw, self._tz = gw, tz

    def list_all(self) -> list[TimeBlock]:
        return [fields_to_block(r.record_id, r.fields, self._tz) for r in self._gw.search("time_blocks")]

    def confirmed(self) -> list[TimeBlock]:
        return [b for b in self.list_all() if b.status == BlockStatus.CONFIRMED]

    def future_confirmed_for(self, task_id: str, after: datetime) -> list[TimeBlock]:
        return [b for b in self.confirmed() if b.task_id == task_id and b.start >= after]

    def create_many(self, blocks: list[TimeBlock]) -> list[TimeBlock]:
        recs = self._gw.batch_create("time_blocks", [block_to_fields(b) for b in blocks])
        return [replace(b, record_id=r.record_id) for b, r in zip(blocks, recs)]

    def update_many(self, blocks: list[TimeBlock]) -> None:
        self._gw.batch_update("time_blocks", [(b.record_id, block_to_fields(b)) for b in blocks if b.record_id])


class DraftRepo:
    def __init__(self, gw: BitableGateway, tz: ZoneInfo):
        self._gw, self._tz = gw, tz
        self._cache: dict[str, Draft] = {}

    def save(self, d: Draft) -> Draft:
        if d.record_id:
            self._gw.update("drafts", d.record_id, draft_to_fields(d))
            out = d
        else:
            r = self._gw.create("drafts", draft_to_fields(d))
            out = replace(d, record_id=r.record_id)
        self._cache[out.draft_id] = out
        return out

    def get(self, draft_id: str) -> Draft | None:
        if draft_id in self._cache:
            return self._cache[draft_id]
        for r in self._gw.search("drafts", {"conjunction": "and", "conditions": [{"field_name": "draft_id", "operator": "is", "value": [draft_id]}]}):
            d = fields_to_draft(r.record_id, r.fields, self._tz)
            self._cache[d.draft_id] = d
            return d
        return None

    def latest_open(self) -> Draft | None:
        opens = [d for d in self.load_open() if d.status == DraftStatus.DRAFT]
        return max(opens, key=lambda d: d.created_at) if opens else None

    def latest_partial(self) -> Draft | None:
        rows = self._gw.search("drafts", {"conjunction": "and", "conditions": [{"field_name": "status", "operator": "is", "value": ["partial"]}]})
        ds = [fields_to_draft(r.record_id, r.fields, self._tz) for r in rows]
        for d in ds:
            self._cache[d.draft_id] = d
        return max(ds, key=lambda d: d.created_at) if ds else None

    def load_open(self) -> list[Draft]:
        """Startup recovery: all drafts still in `draft` state."""
        out = []
        for r in self._gw.search("drafts", {"conjunction": "and", "conditions": [{"field_name": "status", "operator": "is", "value": ["draft"]}]}):
            d = fields_to_draft(r.record_id, r.fields, self._tz)
            self._cache[d.draft_id] = d
            out.append(d)
        return out

    def expire_stale(self, now: datetime) -> list[Draft]:
        expired = []
        for d in self.load_open():
            if d.is_expired(now):
                expired.append(self.save(replace(d, status=DraftStatus.EXPIRED)))
        return expired


class OpsLog:
    def __init__(self, gw: BitableGateway, tz: ZoneInfo):
        self._gw, self._tz = gw, tz

    def write(self, action: str, result: str, target: str = "", draft_id: str = "", detail: dict[str, Any] | None = None) -> None:
        try:
            self._gw.create("ops_log", {
                "log_id": new_id(), "time": int(_now(self._tz).timestamp() * 1000), "action": action, "draft_id": draft_id,
                "target": target, "result": result, "detail": json.dumps(detail or {}, ensure_ascii=False)[:20000],
            })
        except Exception:
            log.warning("ops_log write failed", exc_info=True)


class EventDedup:
    """message_id idempotency: cache first, Bitable as durable store (RL12)."""

    def __init__(self, gw: BitableGateway, tz: ZoneInfo, keep_days: int = 7):
        self._gw, self._tz, self._keep = gw, tz, keep_days
        self._seen: set[str] = set()

    def load(self) -> int:
        cutoff = _now(self._tz) - timedelta(days=self._keep)
        n = 0
        for r in self._gw.search("events"):
            mid = str(r.fields.get("message_id", ""))
            ts = r.fields.get("received_at")
            if mid and ts and int(ts) / 1000 >= cutoff.timestamp():
                self._seen.add(mid)
                n += 1
        return n

    def seen(self, message_id: str) -> bool:
        return message_id in self._seen

    def mark(self, message_id: str) -> None:
        self._seen.add(message_id)
        try:
            self._gw.create("events", {"message_id": message_id, "received_at": int(_now(self._tz).timestamp() * 1000), "handled": True})
        except Exception:
            log.warning("events write failed", exc_info=True)


__all__ = ["BlockRepo", "DraftRepo", "EventDedup", "OpsLog", "StaleRecordError", "TaskRepo"]
