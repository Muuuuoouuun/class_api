from datetime import datetime, timezone
from types import SimpleNamespace

from classin_toolkit.config import AppConfig
from classin_toolkit.intelligence import skills
from classin_toolkit.storage.notion_repo import StudentRecord


def _cfg() -> AppConfig:
    return AppConfig.model_validate(
        {
            "academy": {"name": "Test Academy", "timezone": "Asia/Seoul"},
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
        }
    )


def test_agent_skills_registry_exposes_exam_tool() -> None:
    names = [tool["name"] for tool in skills.TOOLS]

    assert len(names) == len(set(names))
    assert "query_missing_homework" in set(names)
    assert "query_missing_exam" in set(names)
    assert "query_report_context" in set(names)
    assert all(tool.get("input_schema", {}).get("type") == "object" for tool in skills.TOOLS)


def test_execute_query_missing_exam() -> None:
    class FakeRepo:
        def find_missing_exam(self, *, exam_name, exam_date, class_name=None):
            assert exam_name == "April Monthly Exam"
            assert exam_date == datetime(2026, 4, 24, tzinfo=timezone.utc)
            assert class_name == "High2-A"
            return [{"student_classin_id": "10002", "student_name": "Kim Young-hee"}]

    result = skills.execute_tool(
        "query_missing_exam",
        {
            "exam_name": "April Monthly Exam",
            "exam_date": "2026-04-24",
            "class_name": "High2-A",
        },
        FakeRepo(),
        _cfg(),
    )

    assert result["missing_count"] == 1
    assert result["students"][0]["student_classin_id"] == "10002"


def test_execute_list_students() -> None:
    class FakeRepo:
        def list_active_students(self):
            return [
                StudentRecord(
                    page_id="p1",
                    classin_id="10001",
                    name="Hong Gil-dong",
                    parent_phone=None,
                    class_name="High2-A",
                )
            ]

    result = skills.execute_tool("list_students", {}, FakeRepo(), _cfg())

    assert result == [{"name": "Hong Gil-dong", "class": "High2-A", "classin_id": "10001"}]


def test_execute_query_report_context(monkeypatch) -> None:
    class FakeRepo:
        def list_active_students(self):
            return [
                StudentRecord(
                    page_id="p1",
                    classin_id="10001",
                    name="Hong Gil-dong",
                    parent_phone=None,
                    class_name="High2-A",
                ),
                StudentRecord(
                    page_id="p2",
                    classin_id="10002",
                    name="Kim Young-hee",
                    parent_phone=None,
                    class_name="High2-B",
                ),
            ]

    def fake_build_report_contexts(_cfg, rows):
        assert rows == [
            {
                "student_classin_id": "10001",
                "student_name": "Hong Gil-dong",
                "student_class_name": "High2-A",
            }
        ]
        return SimpleNamespace(
            summary={"students_with_context": 1, "needs_review": 1},
            contexts={
                "10001": {
                    "has_context": True,
                    "summary": "상담 메모 1건",
                    "badges": ["상담 메모 1건"],
                    "memos": 1,
                    "attachments": 0,
                    "sources": [
                        {
                            "kind": "memo",
                            "date": "2026-04-24",
                            "detail": "집중도 하락 상담",
                            "source": "local_data/inbox/memos/10001.md",
                        }
                    ],
                }
            },
            needs_review_items=[{"kind": "attachment", "source": "unknown.pdf"}],
        )

    monkeypatch.setattr(skills, "build_report_contexts", fake_build_report_contexts)

    result = skills.execute_tool(
        "query_report_context",
        {"student_name": "Hong"},
        FakeRepo(),
        _cfg(),
    )

    assert result["summary"]["students_with_context"] == 1
    assert result["students"][0]["student_name"] == "Hong Gil-dong"
    assert result["students"][0]["context"]["summary"] == "상담 메모 1건"
    assert result["students"][0]["context"]["sources"] == [
        {"kind": "memo", "date": "2026-04-24", "detail": "집중도 하락 상담"}
    ]
    assert result["needs_review_items"] == [{"kind": "attachment", "source": "unknown.pdf"}]


def test_execute_unknown_tool_returns_error() -> None:
    result = skills.execute_tool("missing_tool", {}, object(), _cfg())

    assert result == {"error": "unknown tool: missing_tool"}
