"""주간 학생 리포트 생성 — 학생별 개인화 Claude 호출."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from ..config import AppConfig
from ..storage.notion_repo import StudentRecord
from .claude_client import load_prompt, run_structured


@dataclass
class WeeklyReport:
    summary_markdown: str
    parent_message: str

    @classmethod
    def parse(cls, text: str) -> "WeeklyReport":
        parts = text.split("## 학부모 카톡 문구", 1)
        summary = parts[0].strip()
        parent = parts[1].strip() if len(parts) == 2 else ""
        return cls(summary_markdown=summary, parent_message=parent)


def build_weekly_report(
    *,
    cfg: AppConfig,
    student: StudentRecord,
    period_start: datetime,
    period_end: datetime,
    lessons: list[dict],
    prev_week_lessons: list[dict] | None = None,
    exam_results: list[dict] | None = None,
) -> WeeklyReport:
    system = load_prompt("weekly_report")
    payload = {
        "academy": cfg.academy.name,
        "student": {
            "name": student.name,
            "class": student.class_name,
            "classin_id": student.classin_id,
        },
        "period": {
            "start": period_start.date().isoformat(),
            "end": period_end.date().isoformat(),
        },
        "this_week_lessons": lessons,
        "prev_week_lessons": prev_week_lessons or [],
        "this_week_exams": exam_results or [],
    }
    text = run_structured(
        cfg,
        system=system,
        user=json.dumps(payload, ensure_ascii=False, indent=2),
        model=cfg.anthropic.report_model,
        max_tokens=2048,
    )
    return WeeklyReport.parse(text)
