# scheduler-agent

Personal Work Scheduler：飞书机器人形态的个人工作调度 Agent。接收需求 → 拆解与区间估算 → 对照真实日历容量算缺口 → 排期草案 → 文字确认后写入独立「AI 工作计划」日历 → 进度变化时重排。它不替你承诺任何交付日期。

文档：需求澄清 / 总体技术方案 / 实时开发控制台见飞书 Wiki「Personal Work Scheduler｜飞书日程 Agent」。

## 运行

```bash
uv sync
cp .env.example .env          # 填 FEISHU_APP_ID / FEISHU_APP_SECRET / ANTHROPIC_API_KEY / FEISHU_OWNER_OPEN_ID
uv run scheduler-agent bootstrap   # 建 Base + 5 表 + 「AI 工作计划」日历，输出两行写回 .env
uv run scheduler-agent check       # 连通性检查：表可读、日历忙闲可读
uv run scheduler-agent run         # 长连接机器人常驻
```

飞书自建应用需要：机器人能力、事件订阅 `im.message.receive_v1`（长连接模式）、权限
`im:message`、`im:message:send_as_bot`、`calendar:calendar`、`calendar:calendar:readonly`、`bitable:app`、`drive:drive`（共享 Base/日历给你）。

工作规则在 `config.yaml`，默认值是推测值，请按真实习惯修改。

## 结构

- `bot/` 长连接接收、去重、立即回执、工作线程
- `agent/` Anthropic 工具调用循环、提示词、确定性工具
- `scheduler/` 容量、空闲区间、时间块分配（纯函数）
- `feishu/` 消息、日历、错误映射
- `store/` 多维表格 schema、网关（写前重读）、仓储、bootstrap
- `domain/` 任务、时间块、草案、状态机

```bash
uv run pytest -q && uv run ruff check src tests
```
