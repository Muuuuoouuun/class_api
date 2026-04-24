"""스케줄 CSV/자유 텍스트 → 구조화된 수업·숙제 목록."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from ..config import AppConfig
from .claude_client import load_prompt, run_json


class ParsedHomework(BaseModel):
    title: str
    due_at: datetime | None = None


class ParsedLesson(BaseModel):
    title: str
    start_at: datetime
    end_at: datetime
    homework: ParsedHomework | None = None


class ParsedCourse(BaseModel):
    course_name: str
    teacher_name: str | None = None
    lessons: list[ParsedLesson]


def parse_schedule(cfg: AppConfig, raw_text: str) -> list[ParsedCourse]:
    system = load_prompt("schedule_parse")
    user = f"## 입력 스케줄\n\n{raw_text.strip()}"
    data: Any = run_json(cfg, system=system, user=user)
    if not isinstance(data, list):
        raise ValueError(f"expected list, got {type(data).__name__}")
    return [ParsedCourse.model_validate(c) for c in data]
