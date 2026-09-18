from datetime import date

import pytest

from scheduler_agent.domain.models import EstimateError, InvalidTransition, Task, TaskStatus


def test_estimate_pair_required():
    with pytest.raises(EstimateError):
        Task("a", "a", estimate_min=1)
    with pytest.raises(EstimateError):
        Task("a", "a", estimate_min=5, estimate_max=3)


def test_transitions():
    t = Task("a", "a").transition(TaskStatus.READY).transition(TaskStatus.SCHEDULED).transition(TaskStatus.IN_PROGRESS)
    with pytest.raises(InvalidTransition):
        t.transition(TaskStatus.INBOX)
    assert t.transition(TaskStatus.DONE).status == TaskStatus.DONE


def test_commit_only_explicit():
    t = Task("a", "a", requested_deadline=date(2026, 9, 25))
    assert t.committed_deadline is None
    assert t.commit(date(2026, 9, 26)).committed_deadline == date(2026, 9, 26)
