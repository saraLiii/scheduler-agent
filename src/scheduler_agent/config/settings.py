"""Runtime settings: secrets from .env, work rules from YAML (Settings entity, PRD 4.4)."""
from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Weekday = Literal[0, 1, 2, 3, 4, 5, 6]


class TimeRange(BaseModel):
    start: time
    end: time

    @model_validator(mode="after")
    def _ordered(self) -> TimeRange:
        if self.end <= self.start:
            raise ValueError("time range end must be after start")
        return self


class ProtectedSlot(TimeRange):
    """A recurring slot Sara keeps for herself (personal project, etc.)."""

    weekdays: list[Weekday]
    label: str = "受保护时间"


class WorkRules(BaseModel):
    """D05 defaults are Agent guesses; Sara edits config.yaml to replace them."""

    timezone: str = "Asia/Shanghai"
    work_days: list[Weekday] = [0, 1, 2, 3, 4]
    work_hours: TimeRange = TimeRange(start=time(10, 0), end=time(19, 0))
    lunch: TimeRange | None = TimeRange(start=time(12, 30), end=time(14, 0))
    daily_task_cap_hours: float = 6.0
    daily_buffer_hours: float = 1.0
    min_block_hours: float = 1.0
    max_block_hours: float = 2.5
    meeting_buffer_minutes: int = 15
    allow_overtime_by_default: bool = False
    protected_slots: list[ProtectedSlot] = []
    planning_horizon_days: int = 14
    draft_ttl_hours: int = 24
    event_dedup_days: int = 7

    @model_validator(mode="after")
    def _sane(self) -> WorkRules:
        if self.min_block_hours > self.max_block_hours:
            raise ValueError("min_block_hours must be <= max_block_hours")
        if self.daily_task_cap_hours <= 0:
            raise ValueError("daily_task_cap_hours must be positive")
        return self


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-fable-5-1"
    feishu_bitable_app_token: str = ""
    feishu_calendar_id: str = ""
    feishu_owner_open_id: str = ""


class AppConfig(BaseModel):
    rules: WorkRules = Field(default_factory=WorkRules)
    secrets: Secrets = Field(default_factory=Secrets)


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    p = Path(path)
    rules = WorkRules()
    if p.exists():
        data = yaml.safe_load(p.read_text()) or {}
        rules = WorkRules.model_validate(data.get("rules", data))
    return AppConfig(rules=rules, secrets=Secrets())
