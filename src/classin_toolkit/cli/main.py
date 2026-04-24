"""classin-toolkit CLI."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ..config import load_config
from ..pipelines.core_engine import run_core_engine
from ..pipelines.daily import render_daily
from ..pipelines.exams import import_exam_results, sweep_missing_exam
from ..pipelines.missing_homework import sweep_missing_homework
from ..pipelines.weekly import approve_all, generate_drafts, run_weekly_reports
from ..readiness import ReadinessItem, check_readiness

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


@app.command("import-exam-results")
def import_exam_results_cmd(
    path: Path = typer.Argument(..., exists=True, readable=True),
    exam_name: str | None = typer.Option(None, "--exam-name"),
    exam_date: str | None = typer.Option(None, "--exam-date", help="YYYY-MM-DD"),
    class_name: str | None = typer.Option(None, "--class-name"),
    source: str | None = typer.Option("academy-db", "--source"),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    """시험 결과 CSV/JSON 을 학생 Master 와 병합해 Notion 시험 DB 에 적재."""
    cfg = load_config(config)
    result = import_exam_results(
        cfg,
        path=path,
        exam_name=exam_name,
        exam_date=exam_date,
        class_name=class_name,
        source=source,
    )
    console.print(
        "[green]merged {merged} / {total} rows[/green] "
        "(unresolved={unresolved}, skipped={skipped})".format(
            merged=result.merged_rows,
            total=result.total_rows,
            unresolved=result.unresolved_rows,
            skipped=result.skipped_rows,
        )
    )
    for error in result.errors[:20]:
        console.print(f"[yellow]- {error}[/yellow]")


@app.command("sweep-missing-exam")
def sweep_missing_exam_cmd(
    exam_name: str = typer.Option(..., "--exam-name"),
    exam_date: str = typer.Option(..., "--exam-date", help="YYYY-MM-DD"),
    class_name: str | None = typer.Option(None, "--class-name"),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    """특정 시험의 미응시 학생을 찾아 알림 문구를 일괄 발송한다."""
    cfg = load_config(config)
    n = sweep_missing_exam(
        cfg,
        exam_name=exam_name,
        exam_date=exam_date,
        class_name=class_name,
    )
    console.print(f"[green]dispatched {n} messages[/green]")


@app.command("weekly-reports")
def weekly_reports(
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    """[DEPRECATED alias] generate-weekly-drafts 와 동일."""
    cfg = load_config(config)
    n = run_weekly_reports(cfg)
    console.print(f"[green]generated {n} drafts[/green]")


@app.command("generate-weekly-drafts")
def weekly_drafts(
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    """이번 주 학생별 리포트 드래프트(HTML) 생성. 승인 필수 모드면 아카이브 안 됨."""
    cfg = load_config(config)
    n = generate_drafts(cfg)
    console.print(
        f"[green]{n} drafts written to {cfg.output.weekly.path}[/green]\n"
        "리뷰 후 [bold]classin-toolkit approve-weekly --week YYYY-MM-DD[/bold] 로 아카이브"
    )


@app.command("approve-weekly")
def approve_weekly(
    week: str = typer.Option(..., help="주 시작일(월요일) YYYY-MM-DD"),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    """드래프트 HTML 리뷰 후 Notion 아카이브."""
    from datetime import datetime, timezone

    cfg = load_config(config)
    period_start = datetime.fromisoformat(week).replace(tzinfo=timezone.utc)
    n = approve_all(cfg, period_start=period_start)
    console.print(f"[green]approved {n} reports[/green]")


@app.command("render-daily")
def render_daily_cmd(
    date_str: str | None = typer.Option(None, "--date", help="YYYY-MM-DD (기본: 오늘)"),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    """일일 현황 HTML 생성."""
    from datetime import date as date_cls, datetime

    cfg = load_config(config)
    target = date_cls.fromisoformat(date_str) if date_str else datetime.now().date()
    result = render_daily(cfg, target=target)
    if not result:
        console.print("[yellow]daily mode=notion → HTML 생성 건너뜀[/yellow]")
        return
    console.print(f"[green]{result.path}[/green]")
    if result.public_url:
        console.print(f"[cyan]{result.public_url}[/cyan]")


@app.command("write-memo")
def write_memo_cmd(
    classin_id: str = typer.Option(..., "--classin-id"),
    text: str = typer.Option(..., "--text"),
    tag: str | None = typer.Option(None, "--tag"),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    """원장 메모·대응기록을 Notion 메모 DB 에 기록."""
    from ..storage.notion_repo import NotionRepo

    cfg = load_config(config)
    if cfg.output.memo.mode == "off":
        console.print("[yellow]memo mode=off[/yellow]")
        return
    page_id = NotionRepo.from_config(cfg).write_memo(
        student_classin_id=classin_id, text=text, tag=tag
    )
    console.print(f"[green]memo saved[/green] {page_id or '(skipped)'}")


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


@app.command("check-ready")
def check_ready_cmd(
    mode: str = typer.Option(
        "local-demo",
        "--mode",
        help="local-demo | classin-live | kakao-live",
    ),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    """테스트 버전 준비 상태와 config.yaml 누락값 점검."""
    try:
        cfg = load_config(config)
        report = check_readiness(cfg, mode=mode, project_root=Path.cwd())
    except FileNotFoundError as e:
        console.print(f"[red]config not found[/red] {e}")
        raise typer.Exit(1) from e
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from e

    table = Table(title=f"ClassIn Toolkit readiness: {report.mode}")
    table.add_column("상태", no_wrap=True)
    table.add_column("단계", no_wrap=True)
    table.add_column("항목")
    table.add_column("내용")
    table.add_column("다음 조치")

    for item in report.items:
        table.add_row(
            _status_label(item),
            item.stage,
            item.label,
            item.detail,
            item.fix or "-",
        )
    console.print(table)

    if report.ready:
        console.print("[green]ready[/green] 이 모드에서 막는 항목은 없습니다.")
        if report.warnings:
            console.print(f"[yellow]warnings[/yellow] {len(report.warnings)}개는 확인하세요.")
        return

    console.print(f"[red]not ready[/red] 해결 필요한 항목 {len(report.blockers)}개")
    raise typer.Exit(1)


@app.command("ui")
def ui_cmd(
    config: Path = typer.Option(Path("config.yaml"), "--config"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8790, "--port"),
) -> None:
    """로컬 브라우저 운영 UI 실행."""
    import uvicorn

    from ..ui import create_app

    console.print(f"[green]ClassIn Toolkit UI[/green] http://{host}:{port}")
    uvicorn.run(create_app(config_path=config), host=host, port=port, reload=False)


def _status_label(item: ReadinessItem) -> str:
    labels = {
        "ok": "[green]OK[/green]",
        "warn": "[yellow]WARN[/yellow]",
        "missing": "[red]MISSING[/red]",
        "blocked": "[magenta]BLOCKED[/magenta]",
    }
    return labels[item.status]


if __name__ == "__main__":
    app()
