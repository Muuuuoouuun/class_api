"""Claude tool-use chat loop for manual academy operations."""
from __future__ import annotations

import json
import logging

from rich.console import Console
from rich.markdown import Markdown

from ..config import AppConfig
from ..storage.notion_repo import NotionRepo
from .claude_client import get_claude
from .skills import TOOLS, execute_tool

log = logging.getLogger(__name__)
console = Console()

_SYSTEM = """\
당신은 학원 운영 AI 어시스턴트입니다. ClassIn 수업 데이터와 Notion DB를 기반으로
원장·교사의 질문에 답합니다.

- 학생 데이터가 필요하면 반드시 제공된 도구를 먼저 호출하세요.
- 숫자는 구체적으로, 판단은 간결하게 말하세요.
- 학부모 발송 문구를 요청받으면 정중하고 전문적인 한국어로 작성하세요.
"""


def run_agent_turn(
    cfg: AppConfig,
    messages: list[dict],
) -> tuple[str, list[dict]]:
    """Run one agent turn and return the final text plus updated messages."""
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
                block.text for block in response.content if getattr(block, "type", "") == "text"
            )
            return text.strip(), messages

        tool_results = []
        for block in response.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            result = execute_tool(block.name, block.input, repo, cfg)
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
                block.text for block in response.content if getattr(block, "type", "") == "text"
            )
            return text.strip(), messages

        messages = messages + [{"role": "user", "content": tool_results}]


def chat_loop(cfg: AppConfig) -> None:
    """Interactive terminal chat loop."""
    console.print("[bold green]ClassIn AI Assistant[/bold green] (종료: exit 또는 Ctrl+C)\n")
    messages: list[dict] = []

    while True:
        try:
            user_input = console.input("[bold cyan]원장님>[/bold cyan] ").strip()
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
