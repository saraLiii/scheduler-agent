"""Entry point: `scheduler-agent bootstrap` | `scheduler-agent run` | `scheduler-agent check`."""
from __future__ import annotations

import argparse
import logging
import sys
from zoneinfo import ZoneInfo

from scheduler_agent.config.settings import load_config


def _wire(cfg):
    from scheduler_agent.agent.drafts import DraftService
    from scheduler_agent.agent.llm import AnthropicProvider
    from scheduler_agent.agent.orchestrator import Orchestrator
    from scheduler_agent.agent.tools import ToolContext, Tools
    from scheduler_agent.feishu.calendar import CalendarAdapter
    from scheduler_agent.feishu.client import build_client
    from scheduler_agent.feishu.messaging import Messenger
    from scheduler_agent.store.bitable import BitableGateway
    from scheduler_agent.store.bootstrap import list_tables
    from scheduler_agent.store.repo import BlockRepo, DraftRepo, EventDedup, OpsLog, TaskRepo

    s = cfg.secrets
    tz = ZoneInfo(cfg.rules.timezone)
    client = build_client(s.feishu_app_id, s.feishu_app_secret)
    gw = BitableGateway(client, s.feishu_bitable_app_token)
    gw.set_table_ids(list_tables(client, s.feishu_bitable_app_token))
    tasks, ops = TaskRepo(gw, tz), OpsLog(gw, tz)
    cal = CalendarAdapter(client, s.feishu_calendar_id or None, s.feishu_owner_open_id)
    llm = AnthropicProvider(s.anthropic_api_key, s.anthropic_model)
    drafts = DraftService(cfg.rules, tz, tasks, BlockRepo(gw, tz), DraftRepo(gw, tz), cal, ops)
    drafts.drafts.load_open()  # startup recovery: unexpired drafts can still be confirmed
    tools = Tools(ToolContext(cfg.rules, tz, tasks, cal, ops, llm, drafts))
    orch = Orchestrator(llm, tools)
    dedup = EventDedup(gw, tz, cfg.rules.event_dedup_days)
    return client, gw, tasks, cal, Messenger(client, s.feishu_owner_open_id), orch, dedup


def cmd_bootstrap(cfg) -> int:
    from scheduler_agent.feishu.calendar import CalendarAdapter
    from scheduler_agent.feishu.client import build_client
    from scheduler_agent.store.bootstrap import bootstrap_bitable

    s = cfg.secrets
    client = build_client(s.feishu_app_id, s.feishu_app_secret)
    res = bootstrap_bitable(client, s.feishu_bitable_app_token or None, s.feishu_owner_open_id)
    cal = CalendarAdapter(client, s.feishu_calendar_id or None, s.feishu_owner_open_id)
    cal_id = cal.ensure_agent_calendar()
    print("Bootstrap 完成。把下面两行写入 .env：")
    print(f"FEISHU_BITABLE_APP_TOKEN={res.app_token}")
    print(f"FEISHU_CALENDAR_ID={cal_id}")
    print(f"tables={res.table_ids}")
    if res.created_tables:
        print(f"新建表: {res.created_tables}")
    if res.added_fields:
        print(f"补充字段: {res.added_fields}")
    return 0


def cmd_check(cfg) -> int:
    _client, _gw, tasks, cal, _m, _o, dedup = _wire(cfg)
    n = dedup.load()
    print(f"tasks table reachable: {len(tasks.list_all())} rows; dedup loaded {n} recent message ids")
    from datetime import datetime, timedelta
    now = datetime.now(ZoneInfo(cfg.rules.timezone))
    busy = cal.primary_busy(now, now + timedelta(days=7))
    print(f"primary calendar busy events next 7d: {len(busy)}")
    return 0


def cmd_run(cfg) -> int:
    from scheduler_agent.bot.ws_bot import Bot

    _client, _gw, _tasks, _cal, messenger, orch, dedup = _wire(cfg)
    n = dedup.load()
    logging.getLogger(__name__).info("loaded %d recent message ids", n)
    Bot(cfg.secrets.feishu_app_id, cfg.secrets.feishu_app_secret, cfg.secrets.feishu_owner_open_id, messenger, dedup, orch).run()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scheduler-agent")
    p.add_argument("command", choices=["bootstrap", "run", "check"])
    p.add_argument("--config", default="config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(a.config)
    return {"bootstrap": cmd_bootstrap, "run": cmd_run, "check": cmd_check}[a.command](cfg)


if __name__ == "__main__":
    sys.exit(main())
