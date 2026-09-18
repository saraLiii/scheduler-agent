"""Domain entities. Field names double as the Bitable column contract (tech plan §5)."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import StrEnum

from ulid import ULID


def new_id() -> str:
    return str(ULID())


class TaskStatus(StrEnum):
    INBOX = "inbox"
    READY = "ready"
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


# PRD 4.1 lifecycle; blocked/cancelled reachable from any active state.
_ACTIVE = {TaskStatus.INBOX, TaskStatus.READY, TaskStatus.SCHEDULED, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED}
ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.INBOX: {TaskStatus.READY, TaskStatus.CANCELLED, TaskStatus.BLOCKED},
    TaskStatus.READY: {TaskStatus.SCHEDULED, TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED, TaskStatus.BLOCKED},
    TaskStatus.SCHEDULED: {TaskStatus.IN_PROGRESS, TaskStatus.READY, TaskStatus.DONE, TaskStatus.CANCELLED, TaskStatus.BLOCKED},
    TaskStatus.IN_PROGRESS: {TaskStatus.DONE, TaskStatus.SCHEDULED, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.BLOCKED: {TaskStatus.READY, TaskStatus.SCHEDULED, TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED},
    TaskStatus.DONE: set(),
    TaskStatus.CANCELLED: set(),
}


class Priority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class InvalidTransition(ValueError):
    pass


class EstimateError(ValueError):
    pass


@dataclass(frozen=True)
class Task:
    task_id: str
    title: str
    status: TaskStatus = TaskStatus.INBOX
    priority: Priority = Priority.P1
    source: str = "自己"
    description: str = ""
    parent_id: str | None = None
    estimate_min: float | None = None
    estimate_max: float | None = None
    remaining_hours: float | None = None
    actual_hours: float = 0.0
    requested_deadline: date | None = None
    committed_deadline: date | None = None
    planned_end: date | None = None
    dependency: str = ""
    risk: str = ""
    assumptions: str = ""
    confidence: Confidence = Confidence.MEDIUM
    estimate_source: str = "llm"
    record_id: str | None = None
    last_modified_ms: int | None = None  # Bitable 最后更新时间, for RL13 re-read check
    sequence: int = 0  # creation order tiebreak

    def __post_init__(self) -> None:
        if (self.estimate_min is None) != (self.estimate_max is None):
            raise EstimateError("estimate_min and estimate_max must be given together (RL09)")
        if self.estimate_min is not None and self.estimate_max < self.estimate_min:
            raise EstimateError("estimate_max must be >= estimate_min (RL09)")

    @property
    def is_active(self) -> bool:
        return self.status in _ACTIVE

    @property
    def remaining(self) -> float:
        if self.remaining_hours is not None:
            return self.remaining_hours
        return self.estimate_max or 0.0

    def transition(self, to: TaskStatus) -> Task:
        if to == self.status:
            return self
        if to not in ALLOWED_TRANSITIONS[self.status]:
            raise InvalidTransition(f"{self.status} -> {to} not allowed")
        return replace(self, status=to)

    def with_estimate(self, lo: float, hi: float, source: str = "llm") -> Task:
        if hi < lo:
            raise EstimateError("estimate_max must be >= estimate_min (RL09)")
        return replace(self, estimate_min=lo, estimate_max=hi, remaining_hours=hi, estimate_source=source)

    def commit(self, d: date) -> Task:
        """Only the explicit commit action writes committed_deadline (RL10)."""
        return replace(self, committed_deadline=d)


class BlockStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class TimeBlock:
    block_id: str
    task_id: str
    start: datetime
    end: datetime
    status: BlockStatus = BlockStatus.DRAFT
    draft_id: str | None = None
    calendar_id: str | None = None
    event_id: str | None = None
    origin: str = "agent"
    actual_hours: float | None = None
    record_id: str | None = None

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


class DraftKind(StrEnum):
    NEW = "new"
    RESCHEDULE = "reschedule"


class DraftStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    PARTIAL = "partial"
    EXPIRED = "expired"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Draft:
    draft_id: str
    kind: DraftKind
    task_ids: tuple[str, ...]
    blocks: tuple[TimeBlock, ...]
    created_at: datetime
    expires_at: datetime
    status: DraftStatus = DraftStatus.DRAFT
    gap_hours: float = 0.0
    unscheduled: dict[str, float] = field(default_factory=dict)  # task_id -> hours not placed
    summary: str = ""
    replaces_block_ids: tuple[str, ...] = ()  # reschedule: confirmed future blocks to cancel
    record_id: str | None = None

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at
