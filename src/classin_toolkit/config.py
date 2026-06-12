from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

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
    schedule_api: Literal["lms", "legacy"] = "lms"
    default_teacher_uid: str | None = None
    teacher_uids: dict[str, str] = Field(default_factory=dict)
    lms_unit_prefix: str = "API schedule"


class NotionDatabases(BaseModel):
    students: str = ""
    lessons: str = ""
    reports: str = ""
    memos: str | None = None
    exams: str | None = None
    career: str | None = None
    corpus: str | None = None


class NotionConfig(BaseModel):
    token: str = ""
    databases: NotionDatabases = Field(default_factory=NotionDatabases)


class StorageConfig(BaseModel):
    backend: Literal["local", "notion"] = "local"
    path: str = "./local_data/store.json"


class DailyOutputConfig(BaseModel):
    mode: Literal["html", "notion", "both"] = "html"
    path: str = "./reports_out/daily"
    public_url_base: str = ""


class WeeklyOutputConfig(BaseModel):
    mode: Literal["html", "notion", "html+notion"] = "html"
    require_approval: bool = True
    path: str = "./reports_out/weekly"


class MemoOutputConfig(BaseModel):
    mode: Literal["local", "notion", "off"] = "local"


class OutputConfig(BaseModel):
    daily: DailyOutputConfig = Field(default_factory=DailyOutputConfig)
    weekly: WeeklyOutputConfig = Field(default_factory=WeeklyOutputConfig)
    memo: MemoOutputConfig = Field(default_factory=MemoOutputConfig)


class AnthropicConfig(BaseModel):
    api_key: str
    provider: Literal["auto", "anthropic", "gemini"] = "auto"
    model: str = "claude-sonnet-4-6"
    report_model: str = "claude-opus-4-7"


class AligoConfig(BaseModel):
    api_key: str = ""
    user_id: str = ""
    sender: str = ""


class NeisSchoolConfig(BaseModel):
    name: str = ""
    office_code: str = ""
    school_code: str = ""


class NeisConfig(BaseModel):
    api_key: str = ""
    base_url: str = "https://open.neis.go.kr/hub"
    schools: list[NeisSchoolConfig] = Field(default_factory=list)
    event_keywords: list[str] = Field(default_factory=list)


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


class CourseLinksConfig(BaseModel):
    aliases: dict[str, list[str]] = Field(default_factory=dict)


class AppConfig(BaseModel):
    academy: AcademyConfig
    classin: ClassInConfig
    storage: StorageConfig = Field(default_factory=StorageConfig)
    notion: NotionConfig = Field(default_factory=NotionConfig)
    anthropic: AnthropicConfig
    neis: NeisConfig = Field(default_factory=NeisConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    reports: ReportsConfig = Field(default_factory=ReportsConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    course_links: CourseLinksConfig = Field(default_factory=CourseLinksConfig)


DEFAULT_CONFIG_PATH = Path("config.yaml")


@lru_cache(maxsize=1)
def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"{p} not found. Copy config.yaml.example to {p} and fill values."
        )
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    data = _apply_profile_overrides(data, p.parent / "profile.local.yaml")
    return AppConfig.model_validate(data)


def _apply_profile_overrides(data: dict[str, Any], profile_path: Path) -> dict[str, Any]:
    if not profile_path.exists():
        return data
    try:
        profile = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return data
    if not isinstance(profile, dict):
        return data

    merged = dict(data)
    for profile_key, path in _PROFILE_OVERRIDES.items():
        value = profile.get(profile_key)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        _set_nested(merged, path, text)
    return merged


def _set_nested(data: dict[str, Any], path: tuple[str, ...], value: str) -> None:
    cursor = data
    for key in path[:-1]:
        next_value = cursor.get(key)
        if not isinstance(next_value, dict):
            next_value = {}
            cursor[key] = next_value
        cursor = next_value
    cursor[path[-1]] = value


_PROFILE_OVERRIDES: dict[str, tuple[str, ...]] = {
    "academy_name": ("academy", "name"),
    "classin_school_id": ("classin", "school_id"),
    "classin_secret_key": ("classin", "secret_key"),
    "classin_webhook_secret": ("classin", "webhook_secret"),
    "anthropic_api_key": ("anthropic", "api_key"),
    "ai_provider": ("anthropic", "provider"),
    "ai_model": ("anthropic", "model"),
    "ai_report_model": ("anthropic", "report_model"),
    "neis_api_key": ("neis", "api_key"),
    "aligo_api_key": ("notify", "aligo", "api_key"),
    "aligo_user_id": ("notify", "aligo", "user_id"),
    "aligo_sender": ("notify", "aligo", "sender"),
    "notify_mode": ("notify", "mode"),
}
