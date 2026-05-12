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


_KOR_WEEKDAY = ("월", "화", "수", "목", "금", "토", "일")


def parse_schedule_text(cfg: AppConfig, raw_text: str) -> list[dict[str, Any]]:
    """드롭존 UI용 평탄화된 행 포맷으로 스케줄을 반환한다."""
    rows: list[dict[str, Any]] = []
    for course in parse_schedule(cfg, raw_text):
        for lesson in course.lessons:
            start, end = lesson.start_at, lesson.end_at
            rows.append(
                {
                    "day": _KOR_WEEKDAY[start.weekday()],
                    "time": f"{start:%H:%M}-{end:%H:%M}",
                    "class_name": course.course_name,
                    "teacher": course.teacher_name or "",
                    "room": "",
                    "confidence": 0.95,
                }
            )
    return rows
