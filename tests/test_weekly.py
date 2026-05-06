from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from classin_toolkit.config import AppConfig
from classin_toolkit.intelligence.weekly_report import WeeklyReport
from classin_toolkit.pipelines import weekly
from classin_toolkit.storage.notion_repo import StudentRecord
from classin_toolkit.storage.output_port import RenderResult


def test_generate_drafts_filters_students_by_class_name(monkeypatch, tmp_path):
    class FakeRepo:
        def __init__(self):
            self.stats_calls: list[str] = []

        def list_active_students(self):
            return [
                StudentRecord("page-1", "10001", "홍길동", "01012345678", "고2-A"),
                StudentRecord("page-2", "10002", "김영희", "01000000000", "고2-B"),
            ]

        def weekly_student_stats(self, *, student_page_id, since, until):
            self.stats_calls.append(student_page_id)
            return [
                {
                    "date": since.date().isoformat(),
                    "attendance": "출석",
                    "homework_submitted": True,
                }
            ]

        def student_exam_results(self, *, student_page_id, since, until):
            return [
                {
                    "exam_name": "월말평가",
                    "exam_date": since.date().isoformat(),
                    "subject": "수학",
                    "score": 92,
                    "max_score": 100,
                    "attended": True,
                }
            ]

    class FakeRenderer:
        def write_draft(self, cfg, inp):
            return RenderResult(path=tmp_path / f"{inp.student_name}.html")

    repo = FakeRepo()
    captured: dict[str, Any] = {}

    def fake_build_weekly_report(**kwargs):
        captured["student"] = kwargs["student"].name
        captured["class_name"] = kwargs["student"].class_name
        captured["exam_name"] = kwargs["exam_results"][0]["exam_name"]
        return WeeklyReport(summary_markdown="요약", parent_message="문구")

    monkeypatch.setattr(weekly.NotionRepo, "from_config", staticmethod(lambda _cfg: repo))
    monkeypatch.setattr(weekly, "HtmlWeeklyRenderer", FakeRenderer)
    monkeypatch.setattr(weekly, "build_weekly_report", fake_build_weekly_report)

    count = weekly.generate_drafts(
        _cfg(tmp_path),
        reference=datetime.fromisoformat("2026-04-20T12:00:00+00:00"),
        class_name="고2-A",
    )

    assert count == 1
    assert repo.stats_calls == ["page-1", "page-1"]
    assert captured == {"student": "홍길동", "class_name": "고2-A", "exam_name": "월말평가"}

    index = tmp_path / "weekly" / "2026-04-20_drafts.json"
    rows = json.loads(index.read_text(encoding="utf-8"))
    assert [row["student_name"] for row in rows] == ["홍길동"]


def _cfg(tmp_path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "academy": {"name": "테스트학원", "timezone": "Asia/Seoul"},
            "classin": {
                "school_id": "sid",
                "secret_key": "secret",
                "webhook_secret": "webhook",
            },
            "notion": {
                "token": "secret_test",
                "databases": {
                    "students": "students",
                    "lessons": "lessons",
                    "reports": "reports",
                    "memos": "memos",
                    "exams": "exams",
                },
            },
            "anthropic": {"api_key": "sk-ant-test"},
            "output": {
                "weekly": {
                    "mode": "html",
                    "path": str(tmp_path / "weekly"),
                    "require_approval": True,
                },
            },
        }
    )
