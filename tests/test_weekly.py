from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from classin_toolkit.config import AppConfig
from classin_toolkit.intelligence import weekly_report as weekly_intel
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
        captured["report_context"] = kwargs["report_context"]
        return WeeklyReport(summary_markdown="요약", parent_message="문구")

    def fake_build_report_contexts(_cfg, rows):
        captured["context_rows"] = rows
        return SimpleNamespace(
            contexts={
                "10001": {
                    "has_context": True,
                    "summary": "오프라인 시험 1건",
                    "badges": ["오프라인 시험 1건"],
                }
            }
        )

    monkeypatch.setattr(weekly.NotionRepo, "from_config", staticmethod(lambda _cfg: repo))
    monkeypatch.setattr(weekly, "HtmlWeeklyRenderer", FakeRenderer)
    monkeypatch.setattr(weekly, "build_weekly_report", fake_build_weekly_report)
    monkeypatch.setattr(weekly, "build_report_contexts", fake_build_report_contexts)

    count = weekly.generate_drafts(
        _cfg(tmp_path),
        reference=datetime.fromisoformat("2026-04-20T12:00:00+00:00"),
        class_name="고2-A",
        student_classin_ids=["10001"],
    )

    assert count == 1
    assert repo.stats_calls == ["page-1", "page-1"]
    assert captured["student"] == "홍길동"
    assert captured["class_name"] == "고2-A"
    assert captured["exam_name"] == "월말평가"
    assert captured["context_rows"] == [
        {
            "student_classin_id": "10001",
            "student_name": "홍길동",
            "student_class_name": "고2-A",
        }
    ]
    assert captured["report_context"]["summary"] == "오프라인 시험 1건"

    index = tmp_path / "weekly" / "2026-04-20_drafts.json"
    rows = json.loads(index.read_text(encoding="utf-8"))
    assert [row["student_name"] for row in rows] == ["홍길동"]


def test_build_weekly_report_includes_compact_report_context(monkeypatch, tmp_path):
    captured: dict[str, Any] = {}

    def fake_run_structured(_cfg, *, system, user, model, max_tokens):
        captured["system"] = system
        captured["payload"] = json.loads(user)
        captured["model"] = model
        captured["max_tokens"] = max_tokens
        return "## 이번 주 요약\n요약\n\n## 학부모 카톡 문구\n문구"

    monkeypatch.setattr(weekly_intel, "load_prompt", lambda _name: "weekly prompt")
    monkeypatch.setattr(weekly_intel, "run_structured", fake_run_structured)

    report = weekly_intel.build_weekly_report(
        cfg=_cfg(tmp_path),
        student=StudentRecord("page-1", "10001", "홍길동", "01012345678", "고2-A"),
        period_start=datetime.fromisoformat("2026-04-20T00:00:00+00:00"),
        period_end=datetime.fromisoformat("2026-04-26T23:59:00+00:00"),
        lessons=[{"attendance": "출석", "homework_submitted": True}],
        prev_week_lessons=[],
        exam_results=[],
        report_context={
            "has_context": True,
            "summary": "상담 메모 1건 · 공유 자료 1건",
            "badges": ["상담 메모 1건", "공유 자료 1건"],
            "offline_attendance": 0,
            "offline_scores": 0,
            "memos": 1,
            "attachments": 1,
            "sources": [
                {
                    "kind": "memo",
                    "date": "2026-04-24",
                    "detail": "집중도 하락 상담",
                    "source": "local_data/inbox/memos/10001.md",
                    "student": "홍길동",
                }
            ],
        },
    )

    context = captured["payload"]["report_context"]
    assert report.parent_message == "문구"
    assert context["summary"] == "상담 메모 1건 · 공유 자료 1건"
    assert context["memos"] == 1
    assert context["attachments"] == 1
    assert context["sources"] == [
        {"kind": "memo", "date": "2026-04-24", "detail": "집중도 하락 상담"}
    ]


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
