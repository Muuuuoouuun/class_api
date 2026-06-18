from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
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
            captured["render_context"] = inp.report_context["summary"]
            captured["render_quality_status"] = inp.quality["status"]
            return RenderResult(path=tmp_path / f"{inp.student_name}.html")

    repo = FakeRepo()
    captured: dict[str, Any] = {}

    def fake_build_weekly_report(**kwargs):
        captured["student"] = kwargs["student"].name
        captured["class_name"] = kwargs["student"].class_name
        captured["exam_name"] = kwargs["exam_results"][0]["exam_name"]
        captured["academy_context"] = kwargs["academy_context"]["summary"]
        return WeeklyReport(summary_markdown="요약", parent_message="문구")

    def fake_build_report_contexts(cfg, students):
        assert students == [
            {
                "student_classin_id": "10001",
                "student_name": "홍길동",
                "student_class_name": "고2-A",
            }
        ]
        return SimpleNamespace(
            contexts={
                "10001": {
                    "has_context": True,
                    "summary": "오프라인 시험 1건 · 상담 메모 1건",
                    "sources": [
                        {
                            "kind": "offline_score",
                            "date": "2026-04-23",
                            "detail": "단원평가 68점",
                            "source": "local_data/inbox/scores/april.xlsx",
                        }
                    ],
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
    assert captured == {
        "student": "홍길동",
        "class_name": "고2-A",
        "exam_name": "월말평가",
        "academy_context": "오프라인 시험 1건 · 상담 메모 1건",
        "render_context": "오프라인 시험 1건 · 상담 메모 1건",
        "render_quality_status": "review",
    }

    index = tmp_path / "weekly" / "2026-04-20_drafts.json"
    rows = json.loads(index.read_text(encoding="utf-8"))
    assert [row["student_name"] for row in rows] == ["홍길동"]
    assert rows[0]["report_context_summary"] == "오프라인 시험 1건 · 상담 메모 1건"
    assert rows[0]["quality_status"] == "review"
    assert rows[0]["quality_warnings"]


def test_approve_all_skips_blocked_quality_drafts_by_default(monkeypatch, tmp_path):
    class FakeRepo:
        pass

    cfg = _cfg(tmp_path)
    period_start = datetime.fromisoformat("2026-04-20T00:00:00+00:00")
    index = tmp_path / "weekly" / "2026-04-20_drafts.json"
    index.parent.mkdir()
    weekly._write_index_records(
        index,
        [
            weekly.DraftRecord(
                student_classin_id="10001",
                student_name="홍길동",
                html_path="hong.html",
                public_url=None,
                period_start=period_start.isoformat(),
                period_end="2026-04-26T23:59:00+00:00",
                summary_markdown="요약",
                parent_message="문구",
                quality_status="blocked",
                quality_score=30,
                quality_warnings=["표현 안전: 낙인 표현"],
            )
        ],
    )
    monkeypatch.setattr(weekly.NotionRepo, "from_config", staticmethod(lambda _cfg: FakeRepo()))

    result = weekly.approve_all(cfg, period_start=period_start)

    assert result.approved == 0
    assert result.skipped_blocked_quality == 1
    rows = json.loads(index.read_text(encoding="utf-8"))
    assert rows[0]["approved"] is False


def test_approve_all_can_force_blocked_quality_drafts(monkeypatch, tmp_path):
    class FakeRepo:
        pass

    cfg = _cfg(tmp_path)
    period_start = datetime.fromisoformat("2026-04-20T00:00:00+00:00")
    index = tmp_path / "weekly" / "2026-04-20_drafts.json"
    index.parent.mkdir()
    weekly._write_index_records(
        index,
        [
            weekly.DraftRecord(
                student_classin_id="10001",
                student_name="홍길동",
                html_path="hong.html",
                public_url=None,
                period_start=period_start.isoformat(),
                period_end="2026-04-26T23:59:00+00:00",
                summary_markdown="요약",
                parent_message="문구",
                quality_status="blocked",
                quality_score=30,
                quality_warnings=["표현 안전: 낙인 표현"],
            )
        ],
    )
    monkeypatch.setattr(weekly.NotionRepo, "from_config", staticmethod(lambda _cfg: FakeRepo()))

    result = weekly.approve_all(
        cfg,
        period_start=period_start,
        force_blocked_quality=True,
    )

    assert result.approved == 1
    assert result.skipped_blocked_quality == 0
    rows = json.loads(index.read_text(encoding="utf-8"))
    assert rows[0]["approved"] is True


def test_list_drafts_returns_quality_summary(tmp_path):
    cfg = _cfg(tmp_path)
    period_start = datetime.fromisoformat("2026-04-20T00:00:00+00:00")
    index = tmp_path / "weekly" / "2026-04-20_drafts.json"
    index.parent.mkdir()
    weekly._write_index_records(
        index,
        [
            weekly.DraftRecord(
                student_classin_id="10001",
                student_name="홍길동",
                html_path=str(tmp_path / "weekly" / "hong.html"),
                public_url=None,
                period_start=period_start.isoformat(),
                period_end="2026-04-26T23:59:00+00:00",
                summary_markdown="요약",
                parent_message="문구",
                report_context_summary="상담 메모 1건",
                quality_status="ready",
                quality_score=91,
            ),
            weekly.DraftRecord(
                student_classin_id="10002",
                student_name="김영희",
                html_path=str(tmp_path / "weekly" / "kim.html"),
                public_url=None,
                period_start=period_start.isoformat(),
                period_end="2026-04-26T23:59:00+00:00",
                summary_markdown="요약",
                parent_message="문구",
                quality_status="blocked",
                quality_score=35,
                quality_warnings=["다음 액션 부족"],
                approved=True,
            ),
        ],
    )

    result = weekly.list_drafts(cfg, period_start=period_start)

    assert result.exists is True
    assert result.summary == {
        "total": 2,
        "approved": 1,
        "pending": 1,
        "ready": 1,
        "review": 0,
        "blocked": 1,
        "ready_unapproved": 1,
        "review_unapproved": 0,
        "blocked_unapproved": 0,
        "with_context": 1,
        "with_public_url": 0,
    }
    assert [item["student_name"] for item in result.items] == ["홍길동", "김영희"]
    assert result.items[0]["preview_url"] == "/reports/weekly/hong.html"


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
