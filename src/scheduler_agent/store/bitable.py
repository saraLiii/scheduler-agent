"""Thin Bitable record gateway with re-read-before-write (RL13) helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import lark_oapi as lark
from lark_oapi.api.bitable.v1 import (
    AppTableRecord,
    BatchCreateAppTableRecordRequest,
    BatchCreateAppTableRecordRequestBody,
    BatchUpdateAppTableRecordRequest,
    BatchUpdateAppTableRecordRequestBody,
    CreateAppTableRecordRequest,
    GetAppTableRecordRequest,
    SearchAppTableRecordRequest,
    SearchAppTableRecordRequestBody,
    UpdateAppTableRecordRequest,
)

from scheduler_agent.feishu.errors import FeishuError, raise_for


class StaleRecordError(FeishuError):
    """The record changed in Bitable since we read it (Sara edited it by hand)."""


@dataclass(frozen=True)
class Record:
    record_id: str
    fields: dict[str, Any]
    last_modified_ms: int | None


def _lm(fields: dict[str, Any]) -> int | None:
    v = fields.get("last_modified")
    return int(v) if isinstance(v, (int, float)) else None


class BitableGateway:
    def __init__(self, client: lark.Client, app_token: str):
        self._c = client
        self.app_token = app_token
        self._table_ids: dict[str, str] = {}

    def set_table_ids(self, mapping: dict[str, str]) -> None:
        self._table_ids = dict(mapping)

    def table_id(self, name: str) -> str:
        try:
            return self._table_ids[name]
        except KeyError as e:
            raise FeishuError(f"表 {name} 未初始化，请先运行 bootstrap") from e

    def search(self, table: str, filter_: dict | None = None, page_size: int = 500) -> list[Record]:
        out: list[Record] = []
        token: str | None = None
        while True:
            body = SearchAppTableRecordRequestBody.builder()
            if filter_:
                body = body.filter(filter_)
            req = SearchAppTableRecordRequest.builder().app_token(self.app_token).table_id(self.table_id(table)).page_size(page_size)
            if token:
                req = req.page_token(token)
            resp = self._c.bitable.v1.app_table_record.search(req.request_body(body.build()).build())
            raise_for(resp.code, resp.msg)
            for it in resp.data.items or []:
                out.append(Record(it.record_id, it.fields or {}, _lm(it.fields or {})))
            if not resp.data.has_more:
                return out
            token = resp.data.page_token

    def get(self, table: str, record_id: str) -> Record:
        req = GetAppTableRecordRequest.builder().app_token(self.app_token).table_id(self.table_id(table)).record_id(record_id).build()
        resp = self._c.bitable.v1.app_table_record.get(req)
        raise_for(resp.code, resp.msg)
        r = resp.data.record
        return Record(r.record_id, r.fields or {}, _lm(r.fields or {}))

    def create(self, table: str, fields: dict[str, Any]) -> Record:
        req = CreateAppTableRecordRequest.builder().app_token(self.app_token).table_id(self.table_id(table)).request_body(AppTableRecord.builder().fields(fields).build()).build()
        resp = self._c.bitable.v1.app_table_record.create(req)
        raise_for(resp.code, resp.msg)
        r = resp.data.record
        return Record(r.record_id, r.fields or fields, _lm(r.fields or {}))

    def batch_create(self, table: str, rows: list[dict[str, Any]]) -> list[Record]:
        out: list[Record] = []
        for i in range(0, len(rows), 500):
            chunk = rows[i : i + 500]
            body = BatchCreateAppTableRecordRequestBody.builder().records([AppTableRecord.builder().fields(f).build() for f in chunk]).build()
            req = BatchCreateAppTableRecordRequest.builder().app_token(self.app_token).table_id(self.table_id(table)).request_body(body).build()
            resp = self._c.bitable.v1.app_table_record.batch_create(req)
            raise_for(resp.code, resp.msg)
            out.extend(Record(r.record_id, r.fields or {}, _lm(r.fields or {})) for r in resp.data.records or [])
        return out

    def update(self, table: str, record_id: str, fields: dict[str, Any], expect_last_modified: int | None = None) -> Record:
        """RL13: re-read and compare last_modified before writing when caller passes what it last saw."""
        if expect_last_modified is not None:
            cur = self.get(table, record_id)
            if cur.last_modified_ms is not None and cur.last_modified_ms != expect_last_modified:
                raise StaleRecordError(f"记录 {record_id} 在表 {table} 中已被手动修改（{cur.last_modified_ms} != {expect_last_modified}）")
        req = UpdateAppTableRecordRequest.builder().app_token(self.app_token).table_id(self.table_id(table)).record_id(record_id).request_body(AppTableRecord.builder().fields(fields).build()).build()
        resp = self._c.bitable.v1.app_table_record.update(req)
        raise_for(resp.code, resp.msg)
        r = resp.data.record
        return Record(r.record_id, r.fields or fields, _lm(r.fields or {}))

    def batch_update(self, table: str, updates: list[tuple[str, dict[str, Any]]]) -> None:
        for i in range(0, len(updates), 500):
            chunk = updates[i : i + 500]
            body = BatchUpdateAppTableRecordRequestBody.builder().records([AppTableRecord.builder().record_id(rid).fields(f).build() for rid, f in chunk]).build()
            req = BatchUpdateAppTableRecordRequest.builder().app_token(self.app_token).table_id(self.table_id(table)).request_body(body).build()
            resp = self._c.bitable.v1.app_table_record.batch_update(req)
            raise_for(resp.code, resp.msg)
