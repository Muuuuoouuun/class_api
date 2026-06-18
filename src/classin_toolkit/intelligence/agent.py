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
from ..pipelines.exams import query_missing_exam
from ..pipelines.weekly import run_weekly_reports
from ..storage.notion_repo import NotionRepo
from .academy_context import build_report_contexts

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
        "name": "query_academy_context",
        "description": (
            "학생별 주간 리포트 상태와 학원 로컬 데이터(오프라인 출결, 성적, 상담 메모) "
            "병합 맥락을 조회합니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "student_name": {"type": "string", "description": "학생 이름 부분 매칭 (선택)"},
                "class_name": {"type": "string", "description": "반 이름 필터 (선택)"},
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
- 오프라인 출결, 성적, 상담 메모, 리포트 상태가 필요하면 query_academy_context 도구를 호출하세요.
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
            {"name": s.name, "class": s.class_name, "classin_id": s.classin_id} for s in students
        ]

    if name == "query_academy_context":
        student_name = str(tool_input.get("student_name") or "").strip()
        class_name = str(tool_input.get("class_name") or "").strip()
        students = repo.list_active_students()
        if student_name:
            students = [student for student in students if student_name in student.name]
        if class_name:
            students = [student for student in students if student.class_name == class_name]
        rows = [
            {
                "student_classin_id": student.classin_id,
                "student_name": student.name,
                "student_class_name": student.class_name,
            }
            for student in students
        ]
        result = build_report_contexts(cfg, rows)
        by_id = {student.classin_id: student for student in students}
        return {
            "summary": result.summary,
            "students": [
                {
                    "student_name": by_id[classin_id].name,
                    "class": by_id[classin_id].class_name,
                    "classin_id": classin_id,
                    "context": context,
                }
                for classin_id, context in result.contexts.items()
                if classin_id in by_id and context.get("has_context")
            ],
            "needs_review_items": result.needs_review_items[:20],
        }

    if name == "trigger_weekly_report":
        n = run_weekly_reports(cfg)
        return {"reports_generated": n}

    return {"error": f"알 수 없는 도구: {name}"}


# ---------------------------------------------------------------------------
# 에이전트 루프
# ---------------------------------------------------------------------------


def _anthropic_ready(cfg: AppConfig) -> bool:
    """agent 챗(tool-use)은 Anthropic 을 사용한다 — 실제 키 설정 여부 확인.

    llm.provider 와 독립적이다. provider 가 gemini 여도 anthropic.api_key 만 채워져 있으면
    agent 챗은 동작한다(자동 라인은 계속 provider 설정을 따른다).
    """
    key = (cfg.anthropic.api_key or "").strip()
    return key.startswith("sk-ant-") and "REPLACE" not in key.upper()


def run_agent_turn(
    cfg: AppConfig,
    messages: list[dict],
) -> tuple[str, list[dict]]:
    """단일 에이전트 턴. (최종 텍스트, 업데이트된 messages) 반환."""
    if not _anthropic_ready(cfg):
        raise RuntimeError(
            "agent 챗(tool-use)은 Anthropic tool-use 를 사용합니다. "
            f"자동 라인(스케줄/미제출/리포트)은 LLM provider({cfg.llm.provider})로 동작하지만, "
            "agent 챗은 anthropic.api_key 가 필요합니다. "
            "config.yaml 의 anthropic.api_key 를 채우세요(provider 가 gemini 여도 agent 챗만 anthropic 사용)."
        )
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
            text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
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
            text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
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
