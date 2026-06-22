"""주간 학생 리포트 생성 — 학생별 개인화 Claude 호출."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

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
    report_context: dict[str, Any] | None = None,
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
        "report_context": _compact_report_context(report_context),
    }
    text = run_structured(
        cfg,
        system=system,
        user=json.dumps(payload, ensure_ascii=False, indent=2),
        model=cfg.anthropic.report_model,
        max_tokens=2048,
    )
    return WeeklyReport.parse(text)


def _compact_report_context(context: dict[str, Any] | None) -> dict[str, Any]:
    if not context or not context.get("has_context"):
        return {}
    return {
        "summary": context.get("summary", ""),
        "badges": list(context.get("badges") or [])[:6],
        "offline_attendance": int(context.get("offline_attendance") or 0),
        "offline_scores": int(context.get("offline_scores") or 0),
        "memos": int(context.get("memos") or 0),
        "attachments": int(context.get("attachments") or 0),
        "sources": [
            {
                "kind": source.get("kind", ""),
                "date": source.get("date", ""),
                "detail": source.get("detail", ""),
            }
            for source in list(context.get("sources") or [])[:8]
            if isinstance(source, dict)
        ],
    }
