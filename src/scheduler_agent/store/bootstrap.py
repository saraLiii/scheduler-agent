"""Idempotent bootstrap: create Base + 5 tables + fields, create agent calendar, share to Sara."""
from __future__ import annotations

from dataclasses import dataclass

import lark_oapi as lark
from lark_oapi.api.bitable.v1 import (
    AppTableCreateHeader,
    AppTableField,
    CreateAppRequest,
    CreateAppTableFieldRequest,
    CreateAppTableRequest,
    CreateAppTableRequestBody,
    ListAppTableFieldRequest,
    ListAppTableRequest,
    ReqApp,
    ReqTable,
)
from lark_oapi.api.drive.v1 import BaseMember, CreatePermissionMemberRequest

from scheduler_agent.feishu.errors import raise_for

from .schema import ALL_TABLES, FieldDef, TableDef

BASE_NAME = "Personal Work Scheduler"


@dataclass
class BootstrapResult:
    app_token: str
    table_ids: dict[str, str]
    created_tables: list[str]
    added_fields: dict[str, list[str]]


def _field_builder(f: FieldDef) -> AppTableField:
    b = AppTableField.builder().field_name(f.name).type(f.type)
    prop = f.to_property()
    if prop is not None:
        b = b.property(prop)
    return b.build()


def _header_builder(f: FieldDef) -> AppTableCreateHeader:
    b = AppTableCreateHeader.builder().field_name(f.name).type(f.type)
    prop = f.to_property()
    if prop is not None:
        b = b.property(prop)
    return b.build()


def ensure_base(client: lark.Client, app_token: str | None) -> str:
    if app_token:
        return app_token
    req = CreateAppRequest.builder().request_body(ReqApp.builder().name(BASE_NAME).build()).build()
    resp = client.bitable.v1.app.create(req)
    raise_for(resp.code, resp.msg)
    return resp.data.app.app_token


def list_tables(client: lark.Client, app_token: str) -> dict[str, str]:
    out: dict[str, str] = {}
    token = None
    while True:
        req = ListAppTableRequest.builder().app_token(app_token).page_size(100)
        if token:
            req = req.page_token(token)
        resp = client.bitable.v1.app_table.list(req.build())
        raise_for(resp.code, resp.msg)
        for t in resp.data.items or []:
            out[t.name] = t.table_id
        if not resp.data.has_more:
            return out
        token = resp.data.page_token


def _existing_fields(client: lark.Client, app_token: str, table_id: str) -> set[str]:
    names: set[str] = set()
    token = None
    while True:
        req = ListAppTableFieldRequest.builder().app_token(app_token).table_id(table_id).page_size(100)
        if token:
            req = req.page_token(token)
        resp = client.bitable.v1.app_table_field.list(req.build())
        raise_for(resp.code, resp.msg)
        names.update(f.field_name for f in resp.data.items or [])
        if not resp.data.has_more:
            return names
        token = resp.data.page_token


def ensure_table(client: lark.Client, app_token: str, tdef: TableDef, existing: dict[str, str]) -> tuple[str, bool, list[str]]:
    if tdef.name in existing:
        table_id, created = existing[tdef.name], False
    else:
        # create with primary field only, then add the rest (keeps creation payload simple and predictable)
        first = tdef.fields[0]
        table = ReqTable.builder().name(tdef.name).default_view_name("全部").fields([_header_builder(first)]).build()
        req = CreateAppTableRequest.builder().app_token(app_token).request_body(CreateAppTableRequestBody.builder().table(table).build()).build()
        resp = client.bitable.v1.app_table.create(req)
        raise_for(resp.code, resp.msg)
        table_id, created = resp.data.table_id, True
    have = _existing_fields(client, app_token, table_id)
    added: list[str] = []
    for f in tdef.fields:
        if f.name in have:
            continue
        req = CreateAppTableFieldRequest.builder().app_token(app_token).table_id(table_id).request_body(_field_builder(f)).build()
        resp = client.bitable.v1.app_table_field.create(req)
        raise_for(resp.code, resp.msg)
        added.append(f.name)
    return table_id, created, added


def share_to_owner(client: lark.Client, token: str, token_type: str, owner_open_id: str, perm: str = "full_access") -> None:
    if not owner_open_id:
        return
    member = BaseMember.builder().member_type("openid").member_id(owner_open_id).perm(perm).build()
    req = CreatePermissionMemberRequest.builder().token(token).type(token_type).need_notification(False).request_body(member).build()
    resp = client.drive.v1.permission_member.create(req)
    if resp.code in {1063004, 1069502}:  # already a member
        return
    raise_for(resp.code, resp.msg)


def bootstrap_bitable(client: lark.Client, app_token: str | None, owner_open_id: str) -> BootstrapResult:
    app_token = ensure_base(client, app_token)
    existing = list_tables(client, app_token)
    ids: dict[str, str] = {}
    created: list[str] = []
    added: dict[str, list[str]] = {}
    for tdef in ALL_TABLES:
        tid, was_created, fields_added = ensure_table(client, app_token, tdef, existing)
        ids[tdef.name] = tid
        if was_created:
            created.append(tdef.name)
        if fields_added:
            added[tdef.name] = fields_added
    share_to_owner(client, app_token, "bitable", owner_open_id)
    return BootstrapResult(app_token, ids, created, added)
