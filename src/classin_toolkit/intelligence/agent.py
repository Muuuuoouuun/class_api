"""Claude Agent — 원장/교사용 수동 오더 채팅 인터페이스 (Layer 3).

자동화 엔진(Webhook 기반)과 별개로, 원장이 자연어로 질문하면
Notion/ClassIn 데이터를 tool_use로 조회해 답변한다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from rich.console import Console
from rich.markdown import Markdown

from ..config import AppConfig
from ..intelligence.claude_client import get_claude
from ..pipelines.data_merge import build_report_contexts
from ..pipelines.exams import query_missing_exam
from ..pipelines.weekly import run_weekly_reports
from ..storage.notion_repo import NotionRepo

log = logging.getLogger(__name__)
console = Console()

# ---------------------------------------------------------------------------
# Tool 정의
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "query_missing_homework",
        "description": "최근 N시간 내 숙제 미제출 학생 목록을 Notion DB에서 조회합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "window_hours": {
                    "type": "integer",
                    "description": "조회 기준 시간 범위 (기본값: 24)",
                },
            },
        },
    },
    {
        "name": "query_student_stats",
        "description": "특정 학생의 출석·숙제 제출 통계를 조회합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "student_name": {"type": "string", "description": "학생 이름 (부분 매칭)"},
                "days": {"type": "integer", "description": "최근 N일 기준 (기본값: 7)"},
            },
            "required": ["student_name"],
        },
    },
    {
        "name": "query_missing_exam",
        "description": "특정 시험의 미응시 학생 목록을 조회합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "exam_name": {"type": "string", "description": "시험명"},
                "exam_date": {"type": "string", "description": "시험일 YYYY-MM-DD"},
                "class_name": {"type": "string", "description": "반 이름 (선택)"},
            },
            "required": ["exam_name", "exam_date"],
        },
    },
    {
        "name": "list_students",
        "description": "Notion DB의 전체 학생 목록을 반환합니다.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "query_report_context",
        "description": "학생별 주간 리포트와 로컬/오프라인 병합 context를 조회합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "student_name": {
                    "type": "string",
                    "description": "학생 이름 부분 검색 (선택)",
                },
            },
        },
    },
    {
        "name": "trigger_weekly_report",
        "description": "이번 주 전체 학생 개인화 리포트를 생성하고 Notion에 저장합니다.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

_SYSTEM = """\
당신은 학원 운영 AI 어시스턴트입니다. ClassIn 수업 데이터와 Notion DB를 기반으로 원장·교사의 질문에 답합니다.
- 학생 데이터가 필요하면 반드시 도구를 먼저 호출하세요.
- 상담 메모, 오프라인 성적, 첨부 자료처럼 보고서 맥락이 필요하면 query_report_context를 호출하세요.
- 숫자는 구체적으로, 판단은 간결하게.
- 학부모 발송용 문구를 요청받으면 따뜻하고 전문적인 한국어로 작성하세요.
"""


# ---------------------------------------------------------------------------
# Tool 실행
# ---------------------------------------------------------------------------

def _execute_tool(name: str, tool_input: dict, repo: NotionRepo, cfg: AppConfig) -> dict | list:
    now = datetime.now(tz=timezone.utc)

    if name == "query_missing_homework":
        window_hours = int(tool_input.get("window_hours") or 24)
        since = now - timedelta(hours=window_hours)
        rows = repo.find_missing_homework(since=since)
        students = {s.page_id: s for s in repo.list_active_students()}
        return [
            {
                "student_name": students[r["student_page_id"]].name
                if r.get("student_page_id") in students
                else "미등록",
                "lesson_date": r.get("date"),
                "attendance": r.get("attendance"),
                "homework_submitted": r.get("homework_submitted"),
            }
            for r in rows
        ]

    if name == "query_student_stats":
        student_name: str = tool_input["student_name"]
        days: int = int(tool_input.get("days") or 7)
        all_students = repo.list_active_students()
        matched = [s for s in all_students if student_name in s.name]
        if not matched:
            return {"error": f"학생 '{student_name}'을 찾을 수 없습니다."}
        student = matched[0]
        since = now - timedelta(days=days)
        rows = repo.weekly_student_stats(student_page_id=student.page_id, since=since, until=now)
        return {
            "student_name": student.name,
            "class": student.class_name,
            "period_days": days,
            "total_lessons": len(rows),
            "attended": sum(1 for r in rows if r.get("attendance") == "출석"),
            "absent": sum(1 for r in rows if r.get("attendance") == "결석"),
            "late": sum(1 for r in rows if r.get("attendance") == "지각"),
            "homework_submitted": sum(1 for r in rows if r.get("homework_submitted")),
        }

    if name == "query_missing_exam":
        exam_name: str = tool_input["exam_name"]
        exam_date: str = tool_input["exam_date"]
        class_name: str | None = tool_input.get("class_name")
        return query_missing_exam(
            cfg,
            exam_name=exam_name,
            exam_date=exam_date,
            class_name=class_name,
            repo=repo,
        )

    if name == "list_students":
        students = repo.list_active_students()
        return [
            {"name": s.name, "class": s.class_name, "classin_id": s.classin_id}
            for s in students
        ]

    if name == "query_report_context":
        student_name = str(tool_input.get("student_name") or "").strip()
        students = repo.list_active_students()
        if student_name:
            students = [student for student in students if student_name in student.name]
        result = build_report_contexts(cfg, [_student_context_row(student) for student in students])
        return {
            "summary": result.summary,
            "students": [
                {
                    "student_name": student.name,
                    "class": student.class_name,
                    "classin_id": student.classin_id,
                    "context": _compact_report_context(result.contexts.get(student.classin_id)),
                }
                for student in students
            ],
            "needs_review_items": result.needs_review_items[:10],
        }

    if name == "trigger_weekly_report":
        n = run_weekly_reports(cfg)
        return {"reports_generated": n}

    return {"error": f"알 수 없는 도구: {name}"}


def _student_context_row(student: Any) -> dict[str, str]:
    return {
        "student_classin_id": student.classin_id,
        "student_name": student.name,
        "student_class_name": student.class_name,
    }


def _compact_report_context(context: dict[str, Any] | None) -> dict[str, Any]:
    if not context:
        return {"has_context": False}
    return {
        "has_context": bool(context.get("has_context")),
        "summary": context.get("summary", ""),
        "badges": list(context.get("badges") or [])[:6],
        "offline_attendance": int(context.get("offline_attendance") or 0),
        "offline_scores": int(context.get("offline_scores") or 0),
        "memos": int(context.get("memos") or 0),
        "attachments": int(context.get("attachments") or 0),
        "sources": [
            {
                "kind": source.get("kind", ""),
                "date": source.get("date", ""),
                "detail": source.get("detail", ""),
            }
            for source in list(context.get("sources") or [])[:8]
            if isinstance(source, dict)
        ],
    }


# ---------------------------------------------------------------------------
# 에이전트 루프
# ---------------------------------------------------------------------------

def run_agent_turn(
    cfg: AppConfig,
    messages: list[dict],
) -> tuple[str, list[dict]]:
    """단일 에이전트 턴. (최종 텍스트, 업데이트된 messages) 반환."""
    client = get_claude(cfg.anthropic.api_key)
    repo = NotionRepo.from_config(cfg)

    while True:
        response = client.messages.create(
            model=cfg.anthropic.model,
            max_tokens=4096,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            messages=messages,
        )

        messages = messages + [{"role": "assistant", "content": response.content}]

        if response.stop_reason == "end_turn":
            text = "".join(
                b.text for b in response.content if getattr(b, "type", "") == "text"
            )
            return text.strip(), messages

        tool_results = []
        for block in response.content:
            if getattr(block, "type", "") == "tool_use":
                result = _execute_tool(block.name, block.input, repo, cfg)
                log.debug("tool=%s result=%s", block.name, str(result)[:200])
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )

        if not tool_results:
            text = "".join(
                b.text for b in response.content if getattr(b, "type", "") == "text"
            )
            return text.strip(), messages

        messages = messages + [{"role": "user", "content": tool_results}]


def chat_loop(cfg: AppConfig) -> None:
    """터미널 채팅 루프."""
    console.print("[bold green]ClassIn AI 어시스턴트[/bold green] (종료: exit 또는 Ctrl+C)\n")
    messages: list[dict] = []

    while True:
        try:
            user_input = console.input("[bold cyan]원장님 >[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]종료합니다.[/dim]")
            break

        if not user_input or user_input.lower() in ("exit", "quit", "종료"):
            console.print("[dim]종료합니다.[/dim]")
            break

        messages.append({"role": "user", "content": user_input})

        with console.status("[dim]생각 중...[/dim]"):
            text, messages = run_agent_turn(cfg, messages)

        console.print()
        console.print(Markdown(text))
        console.print()
