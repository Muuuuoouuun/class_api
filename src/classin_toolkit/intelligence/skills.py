"""Agent skill registry for Claude tool-use calls."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..config import AppConfig
from ..pipelines.exams import query_missing_exam
from ..pipelines.weekly import run_weekly_reports
from ..storage.notion_repo import NotionRepo


TOOLS: list[dict] = [
    {
        "name": "query_missing_homework",
        "description": "Query students with missing homework in the recent time window.",
        "input_schema": {
            "type": "object",
            "properties": {
                "window_hours": {
                    "type": "integer",
                    "description": "Lookback window in hours. Defaults to 24.",
                },
            },
        },
    },
    {
        "name": "query_student_stats",
        "description": "Query one student's recent attendance and homework stats.",
        "input_schema": {
            "type": "object",
            "properties": {
                "student_name": {"type": "string", "description": "Student name substring"},
                "days": {"type": "integer", "description": "Lookback days. Defaults to 7."},
            },
            "required": ["student_name"],
        },
    },
    {
        "name": "list_students",
        "description": "List active students from the Notion student master.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "query_missing_exam",
        "description": "Query students missing a specific exam.",
        "input_schema": {
            "type": "object",
            "properties": {
                "exam_name": {"type": "string", "description": "Exam name"},
                "exam_date": {"type": "string", "description": "YYYY-MM-DD"},
                "class_name": {"type": "string", "description": "Optional class name"},
            },
            "required": ["exam_name", "exam_date"],
        },
    },
    {
        "name": "trigger_weekly_report",
        "description": "Generate this week's reports for all active students.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def execute_tool(name: str, tool_input: dict, repo: NotionRepo, cfg: AppConfig) -> dict | list:
    now = datetime.now(tz=timezone.utc)

    if name == "query_missing_homework":
        window_hours = int(tool_input.get("window_hours") or 24)
        since = now - timedelta(hours=window_hours)
        rows = repo.find_missing_homework(since=since)
        students = {student.page_id: student for student in repo.list_active_students()}
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

    if name == "query_student_stats":
        student_name: str = tool_input["student_name"]
        days = int(tool_input.get("days") or 7)
        matched = [student for student in repo.list_active_students() if student_name in student.name]
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

    if name == "list_students":
        return [
            {"name": student.name, "class": student.class_name, "classin_id": student.classin_id}
            for student in repo.list_active_students()
        ]

    if name == "query_missing_exam":
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

    if name == "trigger_weekly_report":
        return {"reports_generated": run_weekly_reports(cfg)}

    return {"error": f"unknown tool: {name}"}
