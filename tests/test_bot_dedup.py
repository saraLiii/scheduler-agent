"""Event dedup + inbound extraction (PRD §9 idempotency)."""
from types import SimpleNamespace

from scheduler_agent.bot.ws_bot import extract
from scheduler_agent.store.repo import EventDedup


class FakeGW:
    def __init__(self):
        self.created = []

    def search(self, table, filter_=None, page_size=500):
        return []

    def create(self, table, fields):
        self.created.append((table, fields))


def test_dedup_marks_once():
    d = EventDedup(FakeGW(), None)
    assert not d.seen("m1")
    d.mark("m1")
    assert d.seen("m1")


def _event(chat_type="p2p", mtype="text", content='{"text":" 你好 "}'):
    msg = SimpleNamespace(chat_type=chat_type, message_type=mtype, content=content, message_id="om_1", chat_id="oc_1")
    sender = SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_s"))
    return SimpleNamespace(event=SimpleNamespace(message=msg, sender=sender))


def test_extract_p2p_text():
    inb = extract(_event())
    assert inb.text == "你好" and inb.sender_open_id == "ou_s"


def test_extract_ignores_group_and_non_text():
    assert extract(_event(chat_type="group")) is None
    assert extract(_event(mtype="image", content="{}")) is None
