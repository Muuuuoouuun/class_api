"""Agent skill registry for Claude tool-use calls."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import AppConfig
from ..pipelines.demo_filter import without_seed_demo_rows, without_seed_demo_students
from ..pipelines.exams import query_missing_exam
from ..pipelines.weekly import run_weekly_reports
from ..storage.notion_repo import NotionRepo

ToolResult = dict[str, Any] | list[Any]
SkillHandler = Callable[[dict[str, Any], NotionRepo, AppConfig], ToolResult]


@dataclass(frozen=True)
class AgentSkill:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: SkillHandler

    def tool_definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def _query_missing_homework(
    tool_input: dict[str, Any],
    repo: NotionRepo,
    _cfg: AppConfig,
) -> ToolResult:
    now = datetime.now(tz=timezone.utc)
    window_hours = int(tool_input.get("window_hours") or 24)
    since = now - timedelta(hours=window_hours)
    rows = without_seed_demo_rows(repo.find_missing_homework(since=since))
    students = {
        student.page_id: student
        for student in without_seed_demo_students(repo.list_active_students())
    }
    return [
        {
            "student_name": students[row["student_page_id"]].name
            if row.get("student_page_id") in students
            else "미등록",
            "lesson_date": row.get("date"),
            "attendance": row.get("attendance"),
            "homework_submitted": row.get("homework_submitted"),
        }
        for row in rows
    ]


def _query_student_stats(
    tool_input: dict[str, Any],
    repo: NotionRepo,
    _cfg: AppConfig,
) -> ToolResult:
    now = datetime.now(tz=timezone.utc)
    student_name: str = tool_input["student_name"]
    days = int(tool_input.get("days") or 7)
    matched = [
        student
        for student in without_seed_demo_students(repo.list_active_students())
        if student_name in student.name
    ]
    if not matched:
        return {"error": f"Student not found: {student_name}"}
    student = matched[0]
    since = now - timedelta(days=days)
    rows = repo.weekly_student_stats(student_page_id=student.page_id, since=since, until=now)
    return {
        "student_name": student.name,
        "class": student.class_name,
        "period_days": days,
        "total_lessons": len(rows),
        "attended": sum(1 for row in rows if row.get("attendance") == "출석"),
        "absent": sum(1 for row in rows if row.get("attendance") == "결석"),
        "late": sum(1 for row in rows if row.get("attendance") == "지각"),
        "homework_submitted": sum(1 for row in rows if row.get("homework_submitted")),
    }


def _list_students(
    _tool_input: dict[str, Any],
    repo: NotionRepo,
    _cfg: AppConfig,
) -> ToolResult:
    return [
        {"name": student.name, "class": student.class_name, "classin_id": student.classin_id}
        for student in without_seed_demo_students(repo.list_active_students())
    ]


def _query_missing_exam(
    tool_input: dict[str, Any],
    repo: NotionRepo,
    cfg: AppConfig,
) -> ToolResult:
    exam_name: str = tool_input["exam_name"]
    exam_date: str = tool_input["exam_date"]
    class_name: str | None = tool_input.get("class_name")
    rows = query_missing_exam(
        cfg,
        exam_name=exam_name,
        exam_date=exam_date,
        class_name=class_name,
        repo=repo,
    )
    return {
        "exam_name": exam_name,
        "exam_date": exam_date,
        "class_name": class_name,
        "missing_count": len(rows),
        "students": rows,
    }


def _trigger_weekly_report(
    _tool_input: dict[str, Any],
    _repo: NotionRepo,
    cfg: AppConfig,
) -> ToolResult:
    return {"reports_generated": run_weekly_reports(cfg)}


SKILLS: tuple[AgentSkill, ...] = (
    AgentSkill(
        name="query_missing_homework",
        description="Query students with missing homework in the recent time window.",
        input_schema={
            "type": "object",
            "properties": {
                "window_hours": {
                    "type": "integer",
                    "description": "Lookback window in hours. Defaults to 24.",
                },
            },
        },
        handler=_query_missing_homework,
    ),
    AgentSkill(
        name="query_student_stats",
        description="Query one student's recent attendance and homework stats.",
        input_schema={
            "type": "object",
            "properties": {
                "student_name": {"type": "string", "description": "Student name substring"},
                "days": {"type": "integer", "description": "Lookback days. Defaults to 7."},
            },
            "required": ["student_name"],
        },
        handler=_query_student_stats,
    ),
    AgentSkill(
        name="list_students",
        description="List active students from the Notion student master.",
        input_schema={"type": "object", "properties": {}},
        handler=_list_students,
    ),
    AgentSkill(
        name="query_missing_exam",
        description="Query students missing a specific exam.",
        input_schema={
            "type": "object",
            "properties": {
                "exam_name": {"type": "string", "description": "Exam name"},
                "exam_date": {"type": "string", "description": "YYYY-MM-DD"},
                "class_name": {"type": "string", "description": "Optional class name"},
            },
            "required": ["exam_name", "exam_date"],
        },
        handler=_query_missing_exam,
    ),
    AgentSkill(
        name="trigger_weekly_report",
        description="Generate this week's reports for all active students.",
        input_schema={"type": "object", "properties": {}},
        handler=_trigger_weekly_report,
    ),
)
TOOLS: list[dict[str, Any]] = [skill.tool_definition() for skill in SKILLS]
_SKILL_BY_NAME = {skill.name: skill for skill in SKILLS}


def execute_tool(
    name: str,
    tool_input: dict[str, Any],
    repo: NotionRepo,
    cfg: AppConfig,
) -> ToolResult:
    skill = _SKILL_BY_NAME.get(name)
    if not skill:
        return {"error": f"unknown tool: {name}"}

    return skill.handler(tool_input, repo, cfg)
