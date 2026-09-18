"""Uniform error types so the agent can answer honestly (PRD §9: permission != empty)."""


class FeishuError(Exception):
    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code


class FeishuPermissionError(FeishuError):
    """Missing scope / token invalid. Never treat as 'no data'."""


class FeishuRateLimited(FeishuError):
    pass


class FeishuNotFound(FeishuError):
    pass


# Feishu codes: 99991663/99991664/99991668 token & scope problems; 99991400 rate limit
_PERMISSION_CODES = {99991661, 99991663, 99991664, 99991665, 99991668, 99991672, 99991679, 190004, 1254302}
_RATE_CODES = {99991400, 1254291, 1254290}


def raise_for(code: int, msg: str) -> None:
    if code == 0:
        return
    if code in _PERMISSION_CODES:
        raise FeishuPermissionError(f"飞书权限不足或凭证无效 (code={code}): {msg}", code)
    if code in _RATE_CODES:
        raise FeishuRateLimited(f"飞书接口限流 (code={code}): {msg}", code)
    if code in {1254040, 1254043, 191000}:
        raise FeishuNotFound(f"飞书资源不存在 (code={code}): {msg}", code)
    raise FeishuError(f"飞书调用失败 (code={code}): {msg}", code)
