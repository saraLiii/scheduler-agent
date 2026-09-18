"""Feishu long-connection bot: ack fast, dedup, hand off to a worker thread (PRD §9)."""
from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import dataclass

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from scheduler_agent.agent.llm import LLMUnavailable
from scheduler_agent.agent.orchestrator import Orchestrator
from scheduler_agent.feishu.errors import FeishuPermissionError
from scheduler_agent.feishu.messaging import Messenger
from scheduler_agent.store.repo import EventDedup

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Inbound:
    message_id: str
    sender_open_id: str
    chat_id: str
    text: str


def extract(event: P2ImMessageReceiveV1) -> Inbound | None:
    msg = event.event.message
    if msg.chat_type != "p2p" or msg.message_type != "text":
        return None
    try:
        text = json.loads(msg.content).get("text", "")
    except (ValueError, AttributeError):
        text = ""
    return Inbound(msg.message_id, event.event.sender.sender_id.open_id, msg.chat_id, text.strip())


class Bot:
    def __init__(self, app_id: str, app_secret: str, owner_open_id: str, messenger: Messenger, dedup: EventDedup, orchestrator: Orchestrator):
        self._owner = owner_open_id
        self._m = messenger
        self._dedup = dedup
        self._orch = orchestrator
        self._q: queue.Queue[Inbound] = queue.Queue()
        handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(self._on_message).build()
        self._ws = lark.ws.Client(app_id, app_secret, event_handler=handler, log_level=lark.LogLevel.INFO)
        self._worker = threading.Thread(target=self._loop, name="agent-worker", daemon=True)

    # runs inside the ws callback: must return fast
    def _on_message(self, event: P2ImMessageReceiveV1) -> None:
        inb = extract(event)
        if inb is None:
            return
        if self._owner and inb.sender_open_id != self._owner:
            log.warning("ignoring message from non-owner %s", inb.sender_open_id)
            return
        if self._dedup.seen(inb.message_id):
            log.info("duplicate event %s ignored", inb.message_id)
            return
        self._dedup.mark(inb.message_id)
        self._q.put(inb)

    def _loop(self) -> None:
        while True:
            inb = self._q.get()
            try:
                reply = self._orch.handle(inb.sender_open_id, inb.text)
            except LLMUnavailable:
                reply = "模型服务暂时不可用。你仍可以发「列任务」查看任务；稍后再试自然语言请求。"
            except FeishuPermissionError as e:
                reply = f"飞书权限不足，无法完成：{e}"
            except Exception as e:
                log.exception("handle failed")
                reply = f"处理失败：{type(e).__name__}: {e}"
            try:
                self._m.reply_text(inb.message_id, reply)
            except Exception:
                log.exception("reply failed")

    def run(self) -> None:
        self._worker.start()
        log.info("ws client starting")
        self._ws.start()  # blocking
