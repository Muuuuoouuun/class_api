"""Claude Agent — 원장/교사용 수동 오더 채팅 인터페이스 (Layer 3).

자동화 엔진(Webhook 기반)과 별개로, 원장이 자연어로 질문하면
Notion/ClassIn 데이터를 tool_use로 조회해 답변한다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from rich.console import Console
from rich.markdown import Markdown

from ..config import AppConfig
from ..intelligence.claude_client import get_claude
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
        "name": "list_students",
        "description": "Notion DB의 전체 학생 목록을 반환합니다.",
        "input_schema": {"type": "object", "properties": {}},
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

    if name == "list_students":
        students = repo.list_active_students()
        return [
            {"name": s.name, "class": s.class_name, "classin_id": s.classin_id}
            for s in students
        ]

    if name == "trigger_weekly_report":
        n = run_weekly_reports(cfg)
        return {"reports_generated": n}

    return {"error": f"알 수 없는 도구: {name}"}


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
