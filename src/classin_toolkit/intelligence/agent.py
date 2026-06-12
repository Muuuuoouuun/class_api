"""Claude tool-use chat loop for manual academy operations."""
from __future__ import annotations

import json
import logging

from rich.console import Console
from rich.markdown import Markdown

from ..config import AppConfig
from ..storage.notion_repo import NotionRepo
from .claude_client import get_claude, get_gemini_openai, resolve_model, resolve_provider
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
    if resolve_provider(cfg) == "gemini":
        return _run_gemini_agent_turn(cfg, messages)

    client = get_claude(cfg.anthropic.api_key)
    repo = NotionRepo.from_config(cfg)

    while True:
        response = client.messages.create(
            model=resolve_model(cfg),
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


def _run_gemini_agent_turn(
    cfg: AppConfig,
    messages: list[dict],
) -> tuple[str, list[dict]]:
    client = get_gemini_openai(cfg.anthropic.api_key)
    repo = NotionRepo.from_config(cfg)
    openai_tools = [_to_openai_tool(tool) for tool in TOOLS]
    messages = [{"role": "system", "content": _SYSTEM}, *messages]

    while True:
        response = client.chat.completions.create(
            model=resolve_model(cfg),
            max_tokens=4096,
            messages=messages,
            tools=openai_tools,
            tool_choice="auto",
        )
        message = response.choices[0].message
        assistant_message = _openai_assistant_message(message)
        messages.append(assistant_message)

        tool_calls = getattr(message, "tool_calls", None) or []
        if not tool_calls:
            return (message.content or "").strip(), _without_system(messages)

        for tool_call in tool_calls:
            try:
                tool_input = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError:
                tool_input = {"_raw_arguments": tool_call.function.arguments or ""}
            result = execute_tool(tool_call.function.name, tool_input, repo, cfg)
            log.debug("tool=%s result=%s", tool_call.function.name, str(result)[:200])
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )


def _to_openai_tool(tool: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


def _openai_assistant_message(message) -> dict:
    payload = {"role": "assistant", "content": message.content or ""}
    tool_calls = getattr(message, "tool_calls", None) or []
    if tool_calls:
        payload["tool_calls"] = [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments or "{}",
                },
            }
            for tool_call in tool_calls
        ]
    return payload


def _without_system(messages: list[dict]) -> list[dict]:
    if messages and messages[0].get("role") == "system":
        return messages[1:]
    return messages


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
