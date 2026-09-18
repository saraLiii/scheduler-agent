"""Single lark-oapi client using the app (tenant) identity — D09."""
from __future__ import annotations

import lark_oapi as lark


def build_client(app_id: str, app_secret: str, log_level: lark.LogLevel = lark.LogLevel.WARNING) -> lark.Client:
    if not app_id or not app_secret:
        raise ValueError("FEISHU_APP_ID / FEISHU_APP_SECRET 未配置")
    return lark.Client.builder().app_id(app_id).app_secret(app_secret).log_level(log_level).build()
