from __future__ import annotations

from classin_toolkit.config import AppConfig
from classin_toolkit.pipelines.demo_filter import (
    without_seed_demo_rows,
    without_seed_demo_students,
)
from classin_toolkit.pipelines.missing_homework import query_missing_homework
from classin_toolkit.storage.notion_repo import StudentRecord


def _cfg() -> AppConfig:
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
                },
            },
            "anthropic": {"api_key": "sk-ant-test"},
        }
    )


def test_seed_demo_students_are_removed_from_live_lists() -> None:
    students = [
        StudentRecord("p-demo", "10001", "박성실", "010-0000-0001", "고2-A"),
        StudentRecord("p-real", "90001", "실제학생", "01099990000", "중3-A"),
    ]

    assert without_seed_demo_students(students) == [students[1]]


def test_seed_demo_rows_are_removed_from_live_queries() -> None:
    rows = [
        {
            "student_classin_id": "10001",
            "student_name": "박성실",
            "student_class_name": "고2-A",
            "parent_phone": "010-0000-0001",
            "lesson_classin_id": "DEMO-2026-05-01-1",
            "course_classin_id": "DEMO-COURSE-G2A",
        },
        {
            "student_classin_id": "90001",
            "student_name": "실제학생",
            "student_class_name": "중3-A",
            "parent_phone": "01099990000",
            "lesson_classin_id": "L-real",
            "course_classin_id": "C-real",
        },
    ]

    assert without_seed_demo_rows(rows) == [rows[1]]


def test_live_rows_with_demo_names_are_kept_when_ids_are_real() -> None:
    rows = [
        {
            "student_classin_id": "10005",
            "student_name": "최결석",
            "student_class_name": "고2-A",
            "lesson_classin_id": "L-real",
            "course_classin_id": "C-real",
        },
    ]

    assert without_seed_demo_rows(rows) == rows


def test_missing_homework_query_excludes_seed_demo_rows() -> None:
    class Repo:
        def find_missing_homework(self, *, since, lesson_id=None):
            return [
                {
                    "student_classin_id": "10002",
                    "student_name": "김지각",
                    "student_class_name": "고2-A",
                    "parent_phone": "010-0000-0002",
                    "lesson_classin_id": "DEMO-2026-05-01-1",
                    "course_classin_id": "DEMO-COURSE-G2A",
                },
                {
                    "student_classin_id": "90001",
                    "student_name": "실제학생",
                    "student_class_name": "중3-A",
                    "parent_phone": "01099990000",
                    "lesson_classin_id": "L-real",
                    "course_classin_id": "C-real",
                },
            ]

    rows = query_missing_homework(_cfg(), repo=Repo())

    assert [row["student_classin_id"] for row in rows] == ["90001"]
