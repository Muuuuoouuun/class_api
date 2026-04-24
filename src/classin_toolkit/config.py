from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class AcademyConfig(BaseModel):
    name: str
    timezone: str = "Asia/Seoul"


class ClassInConfig(BaseModel):
    base_url: str = "https://api.eeo.cn"
    school_id: str
    secret_key: str
    webhook_secret: str = ""
    default_password: str = "Classin!2026"


class NotionDatabases(BaseModel):
    students: str
    lessons: str
    reports: str


class NotionConfig(BaseModel):
    token: str
    databases: NotionDatabases


class AnthropicConfig(BaseModel):
    api_key: str
    model: str = "claude-sonnet-4-6"
    report_model: str = "claude-opus-4-7"


class AligoConfig(BaseModel):
    api_key: str = ""
    user_id: str = ""
    sender: str = ""


class NotifyConfig(BaseModel):
    mode: Literal["dry_run", "live"] = "dry_run"
    provider: Literal["aligo", "solapi"] = "aligo"
    aligo: AligoConfig = Field(default_factory=AligoConfig)


class WebhookConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8787
    dump_dir: str = "./samples/incoming"


class ReportsConfig(BaseModel):
    output_dir: str = "./reports_out"
    weekly_cron_day: str = "fri"
    weekly_cron_hour: int = 17


class AppConfig(BaseModel):
    academy: AcademyConfig
    classin: ClassInConfig
    notion: NotionConfig
    anthropic: AnthropicConfig
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    reports: ReportsConfig = Field(default_factory=ReportsConfig)


DEFAULT_CONFIG_PATH = Path("config.yaml")


@lru_cache(maxsize=1)
def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"{p} not found. Copy config.yaml.example to {p} and fill values."
        )
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return AppConfig.model_validate(data)
