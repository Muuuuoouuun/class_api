from datetime import datetime, timezone

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
    names = {tool["name"] for tool in skills.TOOLS}

    assert "query_missing_homework" in names
    assert "query_missing_exam" in names


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
