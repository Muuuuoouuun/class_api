"""Filters for demo-seed data that should not appear in live operations."""
from __future__ import annotations

from typing import Any, TypeVar

T = TypeVar("T")

_DEMO_STUDENT_SIGNATURES = {
    ("10001", "박성실", "010-0000-0001", "고2-A"),
    ("10002", "김지각", "010-0000-0002", "고2-A"),
    ("10003", "이하락", "010-0000-0003", "고2-A"),
    ("10004", "정활발", "010-0000-0004", "고2-A"),
    ("10005", "최결석", "", "고2-A"),
    ("10005", "최결석", "010-0000-0005", "고2-A"),
}


def without_seed_demo_students(students: list[T]) -> list[T]:
    return [student for student in students if not is_seed_demo_student(student)]


def without_seed_demo_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if not is_seed_demo_row(row)]


def is_seed_demo_student(student: Any) -> bool:
    return (
        str(getattr(student, "classin_id", "") or ""),
        str(getattr(student, "name", "") or ""),
        str(getattr(student, "parent_phone", "") or ""),
        str(getattr(student, "class_name", "") or ""),
    ) in _DEMO_STUDENT_SIGNATURES


def is_seed_demo_row(row: dict[str, Any]) -> bool:
    course_id = str(row.get("course_classin_id") or "")
    lesson_id = str(row.get("lesson_classin_id") or "")
    return course_id.startswith("DEMO-") or lesson_id.startswith("DEMO-")
