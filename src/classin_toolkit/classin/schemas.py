"""ClassIn 도메인 모델.

주의: 필드명은 ClassIn 공식 스펙 확정 전 추정치를 포함한다.
실제 스펙(MOON 내부 문서) 확보 후 alias 를 맞춰 조정한다.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Student(BaseModel):
    """학생(학부/학생 계정) 생성/조회 모델."""

    classin_id: str | None = Field(default=None, description="ClassIn 반환 UID")
    name: str
    phone: str
    password: str | None = None
    parent_phone: str | None = None
    class_name: str | None = None


class Course(BaseModel):
    """과목(Course) — 한 학기 반 단위에 대응."""

    classin_id: str | None = Field(default=None, description="ClassIn CourseID")
    name: str
    teacher_ids: list[str] = Field(default_factory=list)
    student_ids: list[str] = Field(default_factory=list)


class Lesson(BaseModel):
    """개별 수업(Lesson) — Course 아래 특정 시간 슬롯."""

    classin_id: str | None = Field(default=None, description="ClassIn LessonID")
    course_id: str
    title: str
    start_at: datetime
    end_at: datetime
    teacher_id: str | None = None


class Homework(BaseModel):
    classin_id: str | None = Field(default=None, description="ClassIn HomeworkID")
    lesson_id: str
    title: str
    description: str = ""
    due_at: datetime | None = None
    attachments: list[str] = Field(default_factory=list)
