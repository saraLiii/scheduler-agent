"""Single-agent tool-calling loop with per-user conversation memory (in-process)."""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .llm import LLMProvider, LLMUnavailable, tool_result_message
from .prompts import SYSTEM
from .tools import SPECS, Tools

log = logging.getLogger(__name__)

RESERVED = {"列任务", "任务列表", "我的任务"}
CONFIRM = {"确认", "确定", "ok", "OK", "好", "可以"}
CANCEL = {"取消", "不要", "算了"}
RETRY = {"重试"}


@dataclass
class Session:
    history: deque = field(default_factory=lambda: deque(maxlen=40))

    def messages(self) -> list[dict[str, Any]]:
        return list(self.history)


class Orchestrator:
    def __init__(self, llm: LLMProvider, tools: Tools, max_steps: int = 8):
        self._llm, self._tools, self._max = llm, tools, max_steps
        self._sessions: dict[str, Session] = {}

    def session(self, user: str) -> Session:
        return self._sessions.setdefault(user, Session())

    def handle(self, user: str, text: str) -> str:
        text = text.strip()
        if text in RESERVED:  # deterministic path works even when LLM is down
            return self._render_tasks(self._tools.t_list_tasks())
        if text in CONFIRM | CANCEL | RETRY:
            return self._handle_draft_word(user, text)
        s = self.session(user)
        s.history.append({"role": "user", "content": text})
        try:
            for _ in range(self._max):
                turn = self._llm.complete(SYSTEM, s.messages(), SPECS)
                s.history.append({"role": "assistant", "content": turn.raw_content})
                if not turn.tool_calls:
                    return turn.text or "（无内容）"
                for call in turn.tool_calls:
                    try:
                        result = self._tools.dispatch(call.name, call.input)
                        is_err = isinstance(result, dict) and "error" in result
                    except Exception as e:  # tool bug must not kill the conversation
                        log.exception("tool %s failed", call.name)
                        result, is_err = {"error": type(e).__name__, "message": str(e)}, True
                    s.history.append(tool_result_message(call, result, is_err))
            return "步骤过多，我先停在这里。请把需求或问题再说具体一点。"
        except Exception as e:
            log.exception("llm failed")
            s.history.pop()  # drop the dangling user turn
            raise LLMUnavailable(str(e)) from e

    def _handle_draft_word(self, user: str, text: str) -> str:
        svc = self._tools.ctx.drafts
        if svc is None:
            return "排期服务未初始化。"
        if text in RETRY:
            p = svc.drafts.latest_partial()
            if not p:
                return "没有需要重试的部分失败草案。"
            _, msg = svc.confirm(p.draft_id)
        else:
            d = svc.drafts.latest_open()
            if not d:
                return "当前没有待确认的草案。" + ("" if text in CANCEL else "先让我为某个任务排期。")
            if text in CANCEL:
                msg = svc.cancel(d.draft_id)
            else:
                _, msg = svc.confirm(d.draft_id)
        s = self.session(user)  # keep the LLM aware of what just happened
        s.history.append({"role": "user", "content": f"[系统] Sara 回复「{text}」，处理结果：{msg}"})
        s.history.append({"role": "assistant", "content": [{"type": "text", "text": msg}]})
        return msg

    @staticmethod
    def _render_tasks(res: dict[str, Any]) -> str:
        if not res["tasks"]:
            return "当前没有活跃任务。"
        lines = [f"活跃任务 {res['count']} 个，剩余工时合计约 {res['remaining_hours_total']}h："]
        for t in res["tasks"]:
            ind = "  └ " if t["parent_id"] else "• "
            dl = f"，期望 {t['requested_deadline']}" if t["requested_deadline"] else ""
            cm = f"，已承诺 {t['committed_deadline']}" if t["committed_deadline"] else ""
            lines.append(f"{ind}[{t['priority']}][{t['status']}] {t['title']} 剩余 {t['remaining_hours']}h{dl}{cm}")
        return "\n".join(lines)
