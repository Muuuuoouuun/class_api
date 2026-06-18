from __future__ import annotations

from types import SimpleNamespace

from classin_toolkit.config import AppConfig
from classin_toolkit.intelligence import agent
from classin_toolkit.storage.notion_repo import StudentRecord


def test_query_academy_context_tool_returns_merged_student_context(monkeypatch, tmp_path):
    class FakeRepo:
        def list_active_students(self):
            return [
                StudentRecord("page-1", "10001", "홍길동", "01012345678", "고2-A"),
                StudentRecord("page-2", "10002", "김영희", "01000000000", "고2-B"),
            ]

    captured = {}

    def fake_build_report_contexts(cfg, rows):
        captured["academy"] = cfg.academy.name
        captured["rows"] = rows
        return SimpleNamespace(
            summary={
                "students_with_context": 1,
                "offline_scores": 1,
                "needs_review": 1,
            },
            contexts={
                "10001": {
                    "has_context": True,
                    "summary": "오프라인 시험 1건",
                    "badges": ["오프라인 시험 1건"],
                    "sources": [
                        {
                            "kind": "offline_score",
                            "date": "2026-04-23",
                            "detail": "단원평가 68점",
                            "source": "scores.xlsx",
                        }
                    ],
                }
            },
            needs_review_items=[
                {
                    "kind": "offline_attendance",
                    "student_name": "미등록",
                    "reason": "학생 자동 매칭 필요",
                }
            ],
        )

    monkeypatch.setattr(agent, "build_report_contexts", fake_build_report_contexts)

    result = agent._execute_tool(
        "query_academy_context",
        {"student_name": "홍", "class_name": "고2-A"},
        FakeRepo(),
        _cfg(tmp_path),
    )

    assert captured == {
        "academy": "테스트학원",
        "rows": [
            {
                "student_classin_id": "10001",
                "student_name": "홍길동",
                "student_class_name": "고2-A",
            }
        ],
    }
    assert result["summary"]["students_with_context"] == 1
    assert result["students"][0]["student_name"] == "홍길동"
    assert result["students"][0]["context"]["summary"] == "오프라인 시험 1건"
    assert result["needs_review_items"][0]["student_name"] == "미등록"


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
            "output": {"weekly": {"path": str(tmp_path / "weekly")}},
            "reports": {"output_dir": str(tmp_path)},
        }
    )
