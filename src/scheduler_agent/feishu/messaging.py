"""Send/reply text messages to Sara. Only ever targets the owner's open_id (R20)."""
from __future__ import annotations

import json

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from .errors import raise_for


class Messenger:
    def __init__(self, client: lark.Client, owner_open_id: str):
        self._c = client
        self._owner = owner_open_id

    def send_text(self, text: str, receive_id: str | None = None) -> str:
        rid = receive_id or self._owner
        if not rid:
            raise ValueError("FEISHU_OWNER_OPEN_ID 未配置")
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(CreateMessageRequestBody.builder().receive_id(rid).msg_type("text").content(json.dumps({"text": text})).build())
            .build()
        )
        resp = self._c.im.v1.message.create(req)
        raise_for(resp.code, resp.msg)
        return resp.data.message_id

    def reply_text(self, message_id: str, text: str) -> str:
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(ReplyMessageRequestBody.builder().msg_type("text").content(json.dumps({"text": text})).build())
            .build()
        )
        resp = self._c.im.v1.message.reply(req)
        raise_for(resp.code, resp.msg)
        return resp.data.message_id
