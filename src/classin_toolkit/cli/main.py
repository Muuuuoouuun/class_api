"""classin-toolkit CLI."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import typer
from rich.console import Console

from ..config import load_config
from ..pipelines.core_engine import run_core_engine
from ..pipelines.missing_homework import sweep_missing_homework
from ..pipelines.weekly import run_weekly_reports

app = typer.Typer(add_completion=False, help="ClassIn Toolkit CLI")
console = Console()


@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.command("parse-schedule")
def parse_schedule_cmd(
    path: Path = typer.Argument(..., exists=True, readable=True),
    dry_run: bool = typer.Option(True, "--dry-run/--live"),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    cfg = load_config(config)
    text = path.read_text(encoding="utf-8")
    result = run_core_engine(cfg, schedule_text=text, dry_run=dry_run)
    console.print(result)


@app.command("replay-webhook")
def replay_webhook(
    path: Path = typer.Argument(..., exists=True, readable=True),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    """저장된 Webhook JSON 을 수신기와 동일한 Cmd 디스패치로 재생."""
    import asyncio

    from ..classin.webhook_schemas import (
        AttendanceEvent,
        EndEvent,
        HomeworkScoreEvent,
        HomeworkSubmitEvent,
        parse_event,
    )
    from ..pipelines.ingest import (
        ingest_attendance,
        ingest_end_summary,
        ingest_homework_score,
        ingest_homework_submit,
    )

    cfg = load_config(config)
    raw = json.loads(path.read_text(encoding="utf-8"))
    event = parse_event(raw)

    handlers = {
        AttendanceEvent: ingest_attendance,
        EndEvent: ingest_end_summary,
        HomeworkSubmitEvent: ingest_homework_submit,
        HomeworkScoreEvent: ingest_homework_score,
    }
    for etype, fn in handlers.items():
        if isinstance(event, etype):
            asyncio.run(fn(event, cfg))
            console.print(f"[green]ingested[/green] cmd={event.Cmd}")
            return
    console.print(f"[yellow]no handler for cmd={event.Cmd}[/yellow]")


@app.command("sweep-missing-homework")
def sweep_missing(
    window_hours: int = typer.Option(24, "--window-hours"),
    lesson_id: str | None = typer.Option(None, "--lesson-id"),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    cfg = load_config(config)
    n = sweep_missing_homework(cfg, window_hours=window_hours, lesson_id=lesson_id)
    console.print(f"[green]dispatched {n} messages[/green]")


@app.command("weekly-reports")
def weekly_reports(
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    cfg = load_config(config)
    n = run_weekly_reports(cfg)
    console.print(f"[green]generated {n} reports[/green]")


@app.command("sso-link")
def sso_link(
    uid: str = typer.Option(...),
    course_id: str = typer.Option(...),
    class_id: str = typer.Option(...),
    telephone: str = typer.Option(...),
    device_type: int = typer.Option(1, help="1=PC/Mac, 2=iOS, 3=Android"),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    """학생/교사용 ClassIn 클라이언트 호출 링크 생성."""
    from ..classin.sso import get_login_linked

    cfg = load_config(config)
    url = get_login_linked(
        base_url=cfg.classin.base_url,
        sid=cfg.classin.school_id,
        safe_key=cfg.classin.secret_key,
        uid=uid,
        course_id=course_id,
        class_id=class_id,
        telephone=telephone,
        device_type=device_type,
    )
    console.print(url)


@app.command("agent")
def agent_cmd(
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    """원장/교사용 AI 어시스턴트 채팅 인터페이스."""
    from ..intelligence.agent import chat_loop

    cfg = load_config(config)
    chat_loop(cfg)


if __name__ == "__main__":
    app()
