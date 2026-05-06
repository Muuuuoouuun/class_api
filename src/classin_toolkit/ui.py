"""로컬 운영 UI (FastAPI).

원장/컨설턴트가 반복 실행하는 CLI 작업을 브라우저 버튼으로 감싼 얇은 계층이다.
비즈니스 로직은 pipelines/와 intelligence/의 기존 함수를 그대로 호출한다.
"""
from __future__ import annotations

import html
import json
import tempfile
from datetime import date as date_cls
from datetime import datetime, timezone
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .api_diagnostics import DiagnosticReport, diagnose_apis
from .config import AppConfig, DEFAULT_CONFIG_PATH, load_config
from .notify.dispatcher import load_notification_history, notification_history_path
from .pipelines.core_engine import run_core_engine
from .pipelines.daily import render_daily
from .pipelines.exams import import_exam_results
from .pipelines.missing_homework import query_missing_homework, sweep_missing_homework
from .pipelines.weekly import approve_all, generate_drafts
from .storage.notion_repo import NotionRepo


def create_app(
    config: AppConfig | None = None,
    *,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> FastAPI:
    app = FastAPI(title="ClassIn Toolkit UI", version="0.1.0")
    state = _ConfigState(config=config, config_path=Path(config_path))

    @app.get("/health")
    async def health() -> dict:
        cfg, error = state.load()
        return {
            "ok": error is None,
            "academy": cfg.academy.name if cfg else None,
            "error": error,
        }

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        cfg, error = state.load()
        return HTMLResponse(_render_shell(_status_payload(cfg, error, state.config_path)))

    @app.get("/api/status")
    async def api_status() -> dict:
        cfg, error = state.load()
        return _status_payload(cfg, error, state.config_path)

    @app.get("/api/diagnostics")
    async def api_diagnostics(live: bool = False) -> dict:
        cfg = _require_config(state)
        report = diagnose_apis(cfg, live=live)
        return _diagnostics_payload(report)

    @app.get("/api/schedule")
    async def api_schedule(
        start: str | None = None,
        days: int = 7,
    ) -> dict:
        cfg = _require_config(state)
        _require_service_config(cfg, "Notion")
        try:
            start_date = date_cls.fromisoformat(start) if start else date_cls.today()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="start는 YYYY-MM-DD 형식이어야 합니다.") from exc
        if days < 1 or days > 62:
            raise HTTPException(status_code=400, detail="days는 1~62 사이여야 합니다.")

        since = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
        until = since + timedelta(days=days)
        try:
            rows = NotionRepo.from_config(cfg).lesson_records(since=since, until=until)
        except Exception as exc:
            raise _service_error("스케줄 조회", exc) from exc
        return _schedule_payload(rows, start_date=start_date, days=days)

    @app.get("/api/missing-homework")
    async def api_missing_homework(
        window_hours: int = 24,
        lesson_id: str | None = None,
    ) -> dict:
        cfg = _require_config(state)
        _require_service_config(cfg, "Notion")
        try:
            rows = query_missing_homework(
                cfg,
                window_hours=window_hours,
                lesson_id=(lesson_id or "").strip() or None,
            )
            history = load_notification_history(cfg, limit=300)
        except Exception as exc:
            raise _service_error("미제출 조회", exc) from exc
        return _missing_homework_payload(rows, history)

    @app.get("/api/notifications")
    async def api_notifications(limit: int = 80) -> dict:
        cfg = _require_config(state)
        try:
            rows = load_notification_history(cfg, limit=limit)
        except Exception as exc:
            raise _service_error("알림 기록 조회", exc) from exc
        return {"ok": True, "items": rows, "summary": _notification_summary(rows)}

    @app.post("/api/render-daily")
    async def api_render_daily(request: Request) -> JSONResponse:
        cfg = _require_config(state)
        _require_service_config(cfg, "Notion")
        payload = await _json_payload(request)
        raw_date = (payload.get("date") or "").strip()
        try:
            target = date_cls.fromisoformat(raw_date) if raw_date else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="date는 YYYY-MM-DD 형식이어야 합니다.") from exc
        try:
            result = render_daily(cfg, target=target)
        except Exception as exc:
            raise _service_error("일일 현황 생성", exc) from exc
        if not result:
            return _ok("daily mode가 notion이라 HTML 생성을 건너뛰었습니다.")
        return _ok(
            "일일 현황 HTML을 생성했습니다.",
            path=str(result.path) if result.path else None,
            public_url=result.public_url,
        )

    @app.post("/api/generate-weekly-drafts")
    async def api_generate_weekly_drafts() -> JSONResponse:
        cfg = _require_config(state)
        _require_service_config(cfg, "Notion")
        try:
            count = generate_drafts(cfg)
        except Exception as exc:
            raise _service_error("주간 드래프트 생성", exc) from exc
        return _ok(f"주간 드래프트 {count}건을 생성했습니다.", count=count)

    @app.post("/api/approve-weekly")
    async def api_approve_weekly(request: Request) -> JSONResponse:
        cfg = _require_config(state)
        _require_service_config(cfg, "Notion")
        payload = await _json_payload(request)
        week = (payload.get("week") or "").strip()
        if not week:
            raise HTTPException(status_code=400, detail="week 값이 필요합니다.")
        try:
            period_start = datetime.fromisoformat(week).replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="week는 YYYY-MM-DD 형식이어야 합니다.") from exc
        try:
            count = approve_all(cfg, period_start=period_start)
        except Exception as exc:
            raise _service_error("주간 리포트 승인", exc) from exc
        return _ok(f"주간 리포트 {count}건을 승인 처리했습니다.", count=count)

    @app.post("/api/sweep-missing-homework")
    async def api_sweep_missing_homework(request: Request) -> JSONResponse:
        cfg = _require_config(state)
        _require_service_config(cfg, "Notion")
        payload = await _json_payload(request)
        window_hours = int(payload.get("window_hours") or 24)
        lesson_id = (payload.get("lesson_id") or "").strip() or None
        try:
            count = sweep_missing_homework(cfg, window_hours=window_hours, lesson_id=lesson_id)
        except Exception as exc:
            raise _service_error("미제출 sweep", exc) from exc
        return _ok(f"미제출 알림 {count}건을 처리했습니다.", count=count)

    @app.post("/api/parse-schedule-dry-run")
    async def api_parse_schedule_dry_run(request: Request) -> JSONResponse:
        cfg = _require_config(state)
        _require_service_config(cfg, "Claude")
        payload = await _json_payload(request)
        schedule_text = (payload.get("schedule_text") or "").strip()
        if not schedule_text:
            raise HTTPException(status_code=400, detail="schedule_text 값이 필요합니다.")
        try:
            result = run_core_engine(cfg, schedule_text=schedule_text, dry_run=True)
        except Exception as exc:
            raise _service_error("스케줄 dry-run", exc) from exc
        return _ok(
            "스케줄 dry-run을 완료했습니다.",
            summary={
                "courses": result.courses_created,
                "lessons": result.lessons_created,
                "homework": result.homework_created,
                "errors": len(result.errors),
            },
            errors=result.errors,
        )

    @app.post("/api/create-schedule")
    async def api_create_schedule(request: Request) -> JSONResponse:
        cfg = _require_config(state)
        _require_service_config(cfg, "Claude")
        payload = await _json_payload(request)
        schedule_text = (payload.get("schedule_text") or "").strip()
        dry_run = bool(payload.get("dry_run", True))
        if not schedule_text:
            raise HTTPException(status_code=400, detail="schedule_text 값이 필요합니다.")
        if not dry_run:
            _require_service_config(cfg, "ClassIn")
        try:
            result = run_core_engine(cfg, schedule_text=schedule_text, dry_run=dry_run)
        except Exception as exc:
            raise _service_error("스케줄 생성", exc) from exc
        return _ok(
            "스케줄 검토를 완료했습니다." if dry_run else "수업과 숙제를 생성했습니다.",
            summary={
                "courses": result.courses_created,
                "lessons": result.lessons_created,
                "homework": result.homework_created,
                "errors": len(result.errors),
            },
            errors=result.errors,
            dry_run=dry_run,
        )

    @app.post("/api/generate-class-reports")
    async def api_generate_class_reports(request: Request) -> JSONResponse:
        cfg = _require_config(state)
        _require_service_config(cfg, "Notion")
        _require_service_config(cfg, "Claude")
        payload = await _json_payload(request)
        raw_week = (payload.get("week") or "").strip()
        class_name = (payload.get("class_name") or "").strip() or None
        reference = None
        if raw_week:
            try:
                reference = datetime.fromisoformat(raw_week).replace(tzinfo=timezone.utc)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="week는 YYYY-MM-DD 형식이어야 합니다.") from exc
        try:
            count = generate_drafts(cfg, reference=reference, class_name=class_name)
        except Exception as exc:
            raise _service_error("반별 리포트 생성", exc) from exc
        return _ok(
            f"{class_name or '전체 반'} 리포트 드래프트 {count}건을 생성했습니다.",
            count=count,
            class_name=class_name,
            week=raw_week,
            includes=["출결", "숙제", "시험 점수"],
        )

    @app.post("/api/import-exam-results")
    async def api_import_exam_results(request: Request) -> JSONResponse:
        cfg = _require_config(state)
        _require_service_config(cfg, "Notion")
        payload = await _json_payload(request)
        csv_text = (payload.get("csv_text") or "").strip()
        if not csv_text:
            raise HTTPException(status_code=400, detail="csv_text 값이 필요합니다.")

        tmp_path = _temp_text_file(csv_text, suffix=".csv")
        try:
            result = import_exam_results(
                cfg,
                path=tmp_path,
                exam_name=(payload.get("exam_name") or "").strip() or None,
                exam_date=(payload.get("exam_date") or "").strip() or None,
                class_name=(payload.get("class_name") or "").strip() or None,
                source=(payload.get("source") or "").strip() or "ui-csv-import",
                dry_run=bool(payload.get("dry_run", True)),
            )
        except Exception as exc:
            raise _service_error("시험 결과 가져오기", exc) from exc
        finally:
            tmp_path.unlink(missing_ok=True)

        return _ok(
            "시험 결과 dry-run을 완료했습니다." if result.dry_run else "시험 결과를 가져왔습니다.",
            summary={
                "total": result.total_rows,
                "merged": result.merged_rows,
                "unresolved": result.unresolved_rows,
                "skipped": result.skipped_rows,
                "errors": len(result.errors),
            },
            errors=result.errors[:20],
            dry_run=result.dry_run,
        )

    @app.post("/api/write-memo")
    async def api_write_memo(request: Request) -> JSONResponse:
        cfg = _require_config(state)
        _require_service_config(cfg, "Notion")
        payload = await _json_payload(request)
        classin_id = (payload.get("classin_id") or "").strip()
        text = (payload.get("text") or "").strip()
        tag = (payload.get("tag") or "").strip() or None
        if not classin_id or not text:
            raise HTTPException(status_code=400, detail="classin_id와 text가 필요합니다.")
        if cfg.output.memo.mode == "off":
            return _ok("memo mode가 off라 기록을 건너뛰었습니다.")
        try:
            page_id = NotionRepo.from_config(cfg).write_memo(
                student_classin_id=classin_id,
                text=text,
                tag=tag,
            )
        except Exception as exc:
            raise _service_error("메모 저장", exc) from exc
        return _ok("메모를 저장했습니다.", page_id=page_id)

    @app.post("/api/agent")
    async def api_agent(request: Request) -> JSONResponse:
        cfg = _require_config(state)
        _require_service_config(cfg, "Claude")
        payload = await _json_payload(request)
        question = (payload.get("question") or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="question 값이 필요합니다.")
        from .intelligence.agent import run_agent_turn

        try:
            answer, _messages = run_agent_turn(cfg, [{"role": "user", "content": question}])
        except Exception as exc:
            raise _service_error("AI 응답 생성", exc) from exc
        return _ok("AI 응답을 생성했습니다.", answer=answer)

    @app.get("/reports/{kind}/{filename}")
    async def serve_report(kind: str, filename: str) -> FileResponse:
        cfg = _require_config(state)
        if kind not in ("daily", "weekly"):
            raise HTTPException(status_code=404)
        if "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(status_code=400)
        base = Path(cfg.output.daily.path) if kind == "daily" else Path(cfg.output.weekly.path)
        path = base / filename
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="text/html; charset=utf-8")

    return app


class _ConfigState:
    def __init__(self, *, config: AppConfig | None, config_path: Path) -> None:
        self._config = config
        self.config_path = config_path

    def load(self) -> tuple[AppConfig | None, str | None]:
        if self._config:
            return self._config, None
        try:
            return load_config(self.config_path), None
        except Exception as exc:
            return None, str(exc)


def _require_config(state: _ConfigState) -> AppConfig:
    cfg, error = state.load()
    if cfg:
        return cfg
    raise HTTPException(status_code=400, detail=error or "config.yaml을 읽을 수 없습니다.")


async def _json_payload(request: Request) -> dict[str, Any]:
    try:
        body = await request.body()
        if not body:
            return {}
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="JSON body를 읽을 수 없습니다.") from exc


def _ok(message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, **extra})


def _temp_text_file(text: str, *, suffix: str) -> Path:
    tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=suffix, delete=False)
    try:
        tmp.write(text)
        return Path(tmp.name)
    finally:
        tmp.close()


def _service_error(action: str, exc: Exception) -> HTTPException:
    return HTTPException(status_code=502, detail=f"{action} 실패: {_safe_exception(exc)}")


def _require_service_config(cfg: AppConfig, service: str) -> None:
    blockers = [
        item
        for item in diagnose_apis(cfg, live=False).items
        if item.service == service and item.status in ("missing", "failed")
    ]
    if not blockers:
        return

    detail = "; ".join(f"{item.check}: {item.detail}" for item in blockers[:3])
    raise HTTPException(status_code=400, detail=f"{service} 설정이 필요합니다: {detail}")


def _safe_exception(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return " ".join(text.split())[:240]


def _status_payload(
    cfg: AppConfig | None,
    error: str | None,
    config_path: Path,
) -> dict[str, Any]:
    if not cfg:
        return {
            "ok": False,
            "config_path": str(config_path),
            "error": error,
            "academy": None,
            "webhook": None,
            "output": None,
            "counts": {},
        }

    daily_dir = Path(cfg.output.daily.path)
    weekly_dir = Path(cfg.output.weekly.path)
    incoming_dir = Path(cfg.webhook.dump_dir)
    return {
        "ok": True,
        "config_path": str(config_path),
        "error": None,
        "academy": cfg.academy.name,
        "webhook": {
            "host": cfg.webhook.host,
            "port": cfg.webhook.port,
            "dump_dir": cfg.webhook.dump_dir,
        },
        "output": {
            "daily_mode": cfg.output.daily.mode,
            "daily_path": cfg.output.daily.path,
            "weekly_mode": cfg.output.weekly.mode,
            "weekly_path": cfg.output.weekly.path,
            "memo_mode": cfg.output.memo.mode,
            "notify_mode": cfg.notify.mode,
        },
        "counts": {
            "incoming_json": _count_files(incoming_dir, "*.json"),
            "daily_html": _count_files(daily_dir, "*.html"),
            "weekly_html": _count_files(weekly_dir, "*.html"),
            "weekly_indexes": _count_files(weekly_dir, "*_drafts.json"),
            "notification_history": _count_history_rows(notification_history_path(cfg)),
        },
    }


def _count_files(path: Path, pattern: str) -> int:
    if not path.exists():
        return 0
    return sum(1 for p in path.glob(pattern) if p.is_file())


def _count_history_rows(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _schedule_payload(rows: list[dict], *, start_date: date_cls, days: int) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            row.get("date") or "",
            row.get("lesson_classin_id") or "",
            row.get("course_classin_id") or "",
        )
        item = grouped.setdefault(
            key,
            {
                "date": row.get("date") or "",
                "lesson_classin_id": row.get("lesson_classin_id") or "",
                "course_classin_id": row.get("course_classin_id") or "",
                "class_names": set(),
                "student_count": 0,
                "attendance": {"출석": 0, "지각": 0, "결석": 0, "미기록": 0},
                "homework_done": 0,
                "homework_missing": 0,
                "homework_unknown": 0,
                "students": [],
            },
        )
        class_name = row.get("student_class_name")
        if class_name:
            item["class_names"].add(class_name)

        attendance = row.get("attendance") or "미기록"
        if attendance not in item["attendance"]:
            item["attendance"][attendance] = 0
        item["attendance"][attendance] += 1

        homework_submitted = row.get("homework_submitted")
        if homework_submitted is True:
            item["homework_done"] += 1
        elif homework_submitted is False:
            item["homework_missing"] += 1
        else:
            item["homework_unknown"] += 1

        item["student_count"] += 1
        item["students"].append(
            {
                "student_classin_id": row.get("student_classin_id") or "",
                "student_name": row.get("student_name") or "미등록",
                "class_name": class_name or "",
                "attendance": attendance,
                "homework_submitted": homework_submitted,
                "homework_late": row.get("homework_late"),
                "homework_score": row.get("homework_score"),
            }
        )

    items = []
    for item in grouped.values():
        item["class_names"] = sorted(item["class_names"])
        item["students"].sort(key=lambda student: (student["class_name"], student["student_name"]))
        items.append(item)
    items.sort(key=lambda item: (item["date"], item["course_classin_id"], item["lesson_classin_id"]))

    summary = {
        "total_lessons": len(items),
        "total_student_rows": len(rows),
        "late": sum(item["attendance"].get("지각", 0) for item in items),
        "absent": sum(item["attendance"].get("결석", 0) for item in items),
        "homework_missing": sum(item["homework_missing"] for item in items),
    }
    return {
        "ok": True,
        "start": start_date.isoformat(),
        "days": days,
        "summary": summary,
        "items": items,
    }


def _missing_homework_payload(rows: list[dict], history: list[dict[str, Any]]) -> dict[str, Any]:
    latest_by_student = _latest_notification_by_student(history)
    items = []
    for row in rows:
        student_id = row.get("student_classin_id") or ""
        latest = latest_by_student.get(student_id)
        items.append(
            {
                "student_classin_id": student_id,
                "student_name": row.get("student_name") or "미등록",
                "class_name": row.get("student_class_name") or "",
                "parent_phone": row.get("parent_phone") or "",
                "has_parent_phone": bool(row.get("parent_phone")),
                "lesson_classin_id": row.get("lesson_classin_id") or "",
                "course_classin_id": row.get("course_classin_id") or "",
                "date": row.get("date") or "",
                "attendance": row.get("attendance") or "",
                "homework_late": row.get("homework_late"),
                "homework_score": row.get("homework_score"),
                "notification_status": latest.get("status") if latest else "pending",
                "notification_at": latest.get("created_at") if latest else "",
                "notification_provider": latest.get("provider") if latest else "",
            }
        )

    summary = {
        "total_missing": len(items),
        "with_parent_phone": sum(1 for item in items if item["has_parent_phone"]),
        "no_parent_phone": sum(1 for item in items if not item["has_parent_phone"]),
        "pending": sum(1 for item in items if item["notification_status"] == "pending"),
        "dry_run": sum(1 for item in items if item["notification_status"] == "dry_run"),
        "sent": sum(1 for item in items if item["notification_status"] == "sent"),
        "failed": sum(1 for item in items if item["notification_status"] == "failed"),
    }
    return {"ok": True, "summary": summary, "items": items}


def _latest_notification_by_student(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in history:
        student_id = row.get("student_classin_id")
        if not student_id or student_id in latest:
            continue
        latest[student_id] = row
    return latest


def _notification_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(rows),
        "dry_run": sum(1 for row in rows if row.get("status") == "dry_run"),
        "sent": sum(1 for row in rows if row.get("status") == "sent"),
        "failed": sum(1 for row in rows if row.get("status") == "failed"),
    }


def _diagnostics_payload(report: DiagnosticReport) -> dict[str, Any]:
    statuses = ("ok", "warn", "missing", "failed", "skipped")
    summary = {
        status: sum(1 for item in report.items if item.status == status)
        for status in statuses
    }
    return {
        "ok": True,
        "ready": report.ready,
        "live": report.live,
        "summary": summary,
        "items": [
            {
                "service": item.service,
                "check": item.check,
                "status": item.status,
                "detail": item.detail,
                "next_step": item.next_step,
            }
            for item in report.items
        ],
    }


def _render_shell(status: dict[str, Any]) -> str:
    status_json = _json_for_script(status)
    today = date_cls.today().isoformat()
    week_start = _week_start(date_cls.today()).isoformat()
    title = html.escape(status.get("academy") or "ClassIn Toolkit")
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ClassIn Toolkit UI</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d9dee7;
      --text: #172033;
      --muted: #657085;
      --primary: #0f766e;
      --primary-strong: #0b5f59;
      --accent: #b45309;
      --danger: #b42318;
      --ok: #16784b;
      --shadow: 0 1px 2px rgba(16, 24, 40, .07);
      --nav: #111827;
      --nav-muted: #cbd5e1;
      --nav-line: rgba(255, 255, 255, .12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-width: 320px;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
      letter-spacing: 0;
    }}
    header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 18px;
      border-bottom: 1px solid var(--line);
      background: #ffffff;
    }}
    h1 {{
      margin: 0;
      font-size: 20px;
      line-height: 1.2;
      font-weight: 700;
    }}
    main {{
      width: min(1180px, calc(100vw - 32px));
      margin: 18px auto 32px;
    }}
    .app-shell {{
      display: grid;
      grid-template-columns: 220px minmax(0, 1fr);
      min-height: 100vh;
    }}
    .sidebar {{
      position: sticky;
      top: 0;
      height: 100vh;
      display: flex;
      flex-direction: column;
      gap: 14px;
      padding: 14px;
      color: #f8fafc;
      background: var(--nav);
      border-right: 1px solid #0b1220;
    }}
    .brand {{
      display: grid;
      grid-template-columns: 36px minmax(0, 1fr);
      gap: 10px;
      align-items: center;
      min-height: 40px;
    }}
    .brand-mark {{
      display: grid;
      place-items: center;
      width: 36px;
      height: 36px;
      border: 1px solid var(--nav-line);
      border-radius: 8px;
      background: #0f766e;
      color: #fff;
      font-weight: 800;
      font-size: 14px;
    }}
    .brand h1 {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: #fff;
      font-size: 16px;
    }}
    .brand span {{
      display: block;
      margin-top: 3px;
      color: var(--nav-muted);
      font-size: 12px;
    }}
    .sidebar .tabs {{
      display: grid;
      gap: 5px;
      margin: 0;
      padding: 0;
      overflow: visible;
      border: 0;
      border-radius: 0;
      background: transparent;
      box-shadow: none;
    }}
    .sidebar .tab-button {{
      justify-content: flex-start;
      width: 100%;
      min-height: 40px;
      border-color: transparent;
      color: var(--nav-muted);
      padding: 8px 10px;
      text-align: left;
    }}
    .sidebar .tab-button:hover {{
      background: rgba(255, 255, 255, .08);
      color: #fff;
    }}
    .sidebar .tab-button.active {{
      border-color: rgba(255, 255, 255, .18);
      background: #ffffff;
      color: #172033;
    }}
    .sidebar-footer {{
      display: grid;
      gap: 8px;
      margin-top: auto;
      padding-top: 14px;
      border-top: 1px solid var(--nav-line);
    }}
    .sidebar-footer .badge {{
      width: fit-content;
    }}
    .last-updated {{
      color: var(--nav-muted);
      font-size: 12px;
      line-height: 1.35;
    }}
    .workspace {{
      min-width: 0;
    }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 5;
      background: rgba(255, 255, 255, .94);
      backdrop-filter: blur(10px);
    }}
    .topbar-title {{
      display: grid;
      gap: 4px;
    }}
    .topbar-title h2 {{
      margin: 0;
      font-size: 18px;
    }}
    .topbar-title span {{
      color: var(--muted);
      font-size: 12px;
    }}
    .workspace main {{
      width: auto;
      margin: 0;
      padding: 14px 18px 24px;
    }}
    .status-strip {{
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 10px;
    }}
    .tabs {{
      display: flex;
      gap: 6px;
      margin: 0 0 14px;
      padding: 4px;
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
      box-shadow: var(--shadow);
    }}
    .tab-button {{
      min-height: 36px;
      flex: 0 0 auto;
      border-color: transparent;
      background: transparent;
      color: var(--muted);
      white-space: nowrap;
    }}
    .tab-button:hover {{
      background: #eef2f6;
      color: var(--text);
    }}
    .tab-button.active {{
      border-color: var(--primary);
      background: var(--primary);
      color: #fff;
    }}
    .tab-panel {{
      display: none;
    }}
    .tab-panel.active {{
      display: block;
    }}
    .metric, .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }}
    .metric {{
      padding: 9px 11px;
      min-height: 58px;
    }}
    .metric span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }}
    .metric strong {{
      display: block;
      margin-top: 4px;
      font-size: 18px;
      line-height: 1.1;
    }}
    .metric.warn strong {{ color: var(--accent); }}
    .metric.alert strong {{ color: var(--danger); }}
    .grid {{
      display: grid;
      grid-template-columns: 1.2fr .8fr;
      gap: 10px;
      align-items: start;
    }}
    .panel {{
      padding: 12px;
    }}
    .panel + .panel {{
      margin-top: 10px;
    }}
    h2 {{
      margin: 0 0 12px;
      font-size: 15px;
      line-height: 1.2;
    }}
    .actions {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .actions.compact {{
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }}
    .action-layout {{
      display: grid;
      gap: 10px;
      align-items: start;
    }}
    .action-grid {{
      display: grid;
      gap: 8px;
    }}
    .core-panel {{
      padding: 12px;
    }}
    .core-panel .panel-head {{
      margin-bottom: 10px;
    }}
    .core-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }}
    .core-button {{
      display: grid;
      grid-template-columns: 28px minmax(0, 1fr);
      grid-template-rows: auto auto;
      gap: 4px 9px;
      align-content: center;
      min-height: 92px;
      border-color: var(--line);
      background: #fff;
      color: var(--text);
      padding: 12px;
      text-align: left;
    }}
    .core-button:hover {{
      background: #eef2f6;
      color: var(--text);
    }}
    .core-button strong {{
      display: block;
      min-width: 0;
      overflow-wrap: anywhere;
      font-size: 14px;
      line-height: 1.25;
    }}
    .core-button span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      font-weight: 500;
    }}
    .core-number {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      grid-row: 1 / span 2;
      width: 28px;
      height: 28px;
      border-radius: 8px;
      color: #fff;
      background: var(--primary);
      font-size: 13px;
      font-weight: 800;
    }}
    .action-controls {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      align-items: stretch;
    }}
    .action-controls .panel {{
      margin-top: 0;
      padding: 12px;
    }}
    .action-controls .panel-head {{
      margin-bottom: 10px;
    }}
    .action-button {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
      min-height: 66px;
      border-color: var(--line);
      background: #fff;
      color: var(--text);
      text-align: left;
    }}
    .action-button:hover {{
      background: #eef2f6;
      color: var(--text);
    }}
    .action-button strong,
    .action-button span {{
      display: block;
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    .action-button span {{
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      font-weight: 500;
    }}
    .action-tag {{
      display: inline-flex;
      align-items: center;
      align-self: center;
      min-height: 26px;
      padding: 4px 8px;
      border-radius: 999px;
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
      font-style: normal;
      font-weight: 650;
      white-space: nowrap;
    }}
    .mini-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    label {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.3;
    }}
    input, select, textarea {{
      width: 100%;
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 9px;
      color: var(--text);
      background: #fff;
      font: inherit;
      font-size: 14px;
    }}
    textarea {{
      min-height: 92px;
      resize: vertical;
    }}
    textarea.large {{
      min-height: 104px;
    }}
    button {{
      min-height: 34px;
      border: 1px solid var(--primary);
      border-radius: 6px;
      padding: 7px 10px;
      background: var(--primary);
      color: #fff;
      font: inherit;
      font-weight: 650;
      cursor: pointer;
    }}
    button.secondary {{
      border-color: var(--line);
      background: #fff;
      color: var(--text);
    }}
    button:hover {{
      background: var(--primary-strong);
    }}
    button.secondary:hover {{
      background: #eef2f6;
    }}
    button:disabled {{
      cursor: progress;
      opacity: .62;
    }}
    .panel-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }}
    .panel-head h2 {{
      margin: 0;
    }}
    .inline-actions {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .row {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      align-items: end;
    }}
    .stack {{
      display: grid;
      gap: 8px;
    }}
    .action-preview {{
      min-height: 86px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fbfcfd;
      color: var(--text);
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-wrap;
    }}
    .action-preview strong {{
      display: block;
      margin-bottom: 4px;
      font-size: 14px;
    }}
    .action-preview ul {{
      margin: 8px 0 0;
      padding-left: 18px;
    }}
    .log {{
      min-height: 220px;
      max-height: 420px;
      overflow: auto;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: #111827;
      color: #e5e7eb;
      padding: 12px;
      font: 13px/1.5 Consolas, "Cascadia Mono", monospace;
      white-space: pre-wrap;
    }}
    .table-wrap {{
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 760px;
      font-size: 13px;
    }}
    th, td {{
      padding: 7px 9px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      background: #f8fafc;
      color: var(--muted);
      font-weight: 650;
      white-space: nowrap;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    .empty {{
      padding: 18px;
      color: var(--muted);
      text-align: center;
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: #fbfcfd;
    }}
    .status-pill {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border-radius: 999px;
      border: 1px solid var(--line);
      white-space: nowrap;
      font-size: 12px;
    }}
    .status-pill.pending {{ color: var(--accent); background: #fff8eb; border-color: #f3d2a1; }}
    .status-pill.dry_run {{ color: #0f5f8c; background: #edf7ff; border-color: #b7dcf3; }}
    .status-pill.sent {{ color: var(--ok); background: #effaf4; border-color: rgba(22, 120, 75, .25); }}
    .status-pill.failed {{ color: var(--danger); background: #fff1f0; border-color: rgba(180, 35, 24, .25); }}
    .status-pill.ok {{ color: var(--ok); background: #effaf4; border-color: rgba(22, 120, 75, .25); }}
    .status-pill.warn {{ color: var(--accent); background: #fff8eb; border-color: #f3d2a1; }}
    .status-pill.missing {{ color: var(--danger); background: #fff1f0; border-color: rgba(180, 35, 24, .25); }}
    .status-pill.skipped {{ color: var(--muted); background: #f8fafc; border-color: var(--line); }}
    .diagnostic-summary {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 12px;
    }}
    .diagnostic-count {{
      min-height: 58px;
      padding: 9px 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfd;
    }}
    .diagnostic-count span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.3;
    }}
    .diagnostic-count strong {{
      display: block;
      margin-top: 5px;
      font-size: 18px;
      line-height: 1.1;
    }}
    .diagnostic-count.ok strong {{ color: var(--ok); }}
    .diagnostic-count.warn strong {{ color: var(--accent); }}
    .diagnostic-count.failed strong,
    .diagnostic-count.missing strong {{ color: var(--danger); }}
    .schedule-summary {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 8px;
      margin: 12px 0;
    }}
    .schedule-summary .diagnostic-count strong {{
      color: var(--text);
    }}
    .student-list {{
      max-width: 280px;
      color: var(--muted);
      line-height: 1.55;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 4px 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
      background: #fff;
    }}
    .badge.ok {{
      color: var(--ok);
      border-color: rgba(22, 120, 75, .25);
      background: #effaf4;
    }}
    .badge.error {{
      color: var(--danger);
      border-color: rgba(180, 35, 24, .25);
      background: #fff1f0;
    }}
    dl {{
      display: grid;
      grid-template-columns: 120px 1fr;
      gap: 8px 12px;
      margin: 0;
      font-size: 13px;
      line-height: 1.45;
    }}
    dt {{ color: var(--muted); }}
    dd {{
      margin: 0;
      overflow-wrap: anywhere;
    }}
    @media (max-width: 860px) {{
      .app-shell {{
        display: block;
      }}
      .sidebar {{
        position: sticky;
        z-index: 10;
        height: auto;
        gap: 12px;
        padding: 12px 10px;
      }}
      .brand {{
        grid-template-columns: 34px minmax(0, 1fr);
      }}
      .brand-mark {{
        width: 34px;
        height: 34px;
      }}
      .sidebar .tabs {{
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 6px;
        overflow: visible;
      }}
      .sidebar .tab-button {{
        justify-content: center;
        width: 100%;
        white-space: nowrap;
      }}
      .sidebar-footer {{
        display: none;
      }}
      header {{
        align-items: flex-start;
        flex-direction: column;
      }}
      .status-strip {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .grid, .actions, .actions.compact, .action-layout, .action-controls, .mini-grid,
      .diagnostic-summary, .schedule-summary {{
        grid-template-columns: 1fr;
      }}
      .core-grid {{
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }}
      .core-button {{
        grid-template-columns: 1fr;
        grid-template-rows: auto auto;
        justify-items: center;
        min-height: 84px;
        padding: 9px 7px;
        text-align: center;
      }}
      .core-button strong {{
        font-size: 12px;
        line-height: 1.25;
      }}
      .core-button > span:not(.core-number) {{
        display: none;
      }}
      .core-number {{
        grid-row: auto;
        width: 24px;
        height: 24px;
        font-size: 11px;
      }}
      .metric {{
        min-height: 64px;
      }}
      .panel-head {{
        align-items: flex-start;
        flex-direction: column;
      }}
      main {{
        width: min(100vw - 20px, 720px);
        margin-top: 10px;
      }}
      .workspace main {{
        width: auto;
        padding: 10px;
      }}
      dl {{
        grid-template-columns: 1fr;
        gap: 3px;
      }}
    }}
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark">CI</div>
        <div>
          <h1>{title}</h1>
          <span>ClassIn Toolkit</span>
        </div>
      </div>
      <nav class="tabs" aria-label="운영 메뉴">
        <button class="tab-button active" data-tab="dashboard">대시보드</button>
        <button class="tab-button" data-tab="actions">액션</button>
        <button class="tab-button" data-tab="schedule">스케줄</button>
        <button class="tab-button" data-tab="missing">미제출</button>
        <button class="tab-button" data-tab="reports">리포트</button>
        <button class="tab-button" data-tab="memo">메모·AI</button>
        <button class="tab-button" data-tab="settings">설정</button>
      </nav>
      <div class="sidebar-footer">
        <span id="configBadge" class="badge">config</span>
        <span id="lastUpdated" class="last-updated">-</span>
      </div>
    </aside>
    <div class="workspace">
      <header class="topbar">
        <div class="topbar-title">
          <h2 id="viewTitle">대시보드</h2>
          <span id="viewMeta">{today}</span>
        </div>
        <div class="inline-actions">
          <button data-action="refreshStatus" class="secondary">상태 새로고침</button>
        </div>
      </header>
      <main>
    <section class="status-strip">
      <div class="metric"><span>Webhook 원본</span><strong id="incomingCount">0</strong></div>
      <div class="metric warn"><span>숙제 미제출</span><strong id="missingCount">0</strong></div>
      <div class="metric alert"><span>연락처 없음</span><strong id="noPhoneCount">0</strong></div>
      <div class="metric"><span>문구 생성</span><strong id="dryRunCount">0</strong></div>
      <div class="metric"><span>발송 완료</span><strong id="sentCount">0</strong></div>
      <div class="metric alert"><span>발송 실패</span><strong id="failedCount">0</strong></div>
    </section>

    <section id="tab-dashboard" class="tab-panel active">
      <section class="grid">
        <div>
          <div class="panel">
            <div class="panel-head">
              <h2>API 연결 점검</h2>
              <div class="inline-actions">
                <button data-action="refreshDiagnostics" class="secondary">설정 점검</button>
                <button data-action="runLiveDiagnostics">실 API 점검</button>
              </div>
            </div>
            <div id="diagnosticSummary" class="diagnostic-summary"></div>
            <div id="diagnosticTable"></div>
          </div>
        </div>
        <aside>
          <div class="panel">
            <h2>실행 로그</h2>
            <div id="log" class="log"></div>
          </div>
        </aside>
      </section>
    </section>

    <section id="tab-actions" class="tab-panel">
      <div class="action-layout">
        <div class="panel core-panel">
          <div class="panel-head">
            <h2>핵심 기능</h2>
            <span class="badge">기본 3개</span>
          </div>
          <div class="core-grid">
            <button data-action="focusMissingCore" class="core-button">
              <span class="core-number">1</span>
              <strong>숙제 미제출 전체 문자 발송</strong>
              <span>미제출 조회 후 연락처 있는 학부모에게 발송</span>
            </button>
            <button data-action="focusScheduleCore" class="core-button">
              <span class="core-number">2</span>
              <strong>스케줄표로 수업·숙제 생성</strong>
              <span>CSV/표 붙여넣기, 검토 후 ClassIn 생성</span>
            </button>
            <button data-action="focusReportCore" class="core-button">
              <span class="core-number">3</span>
              <strong>반별 리포트 생성</strong>
              <span>시험 점수, 숙제, 출결을 모아 초안 생성</span>
            </button>
          </div>
        </div>

        <div class="action-controls">
          <div class="panel">
            <div class="panel-head">
              <h2>숙제 미제출 문자</h2>
              <span id="actionModeBadge" class="badge">대기</span>
            </div>
            <div class="stack">
              <div class="mini-grid">
                <label>조회 범위<input id="windowHours" type="number" min="1" step="1" value="24"></label>
                <label>특정 수업만 보기<input id="lessonId" type="text"></label>
              </div>
              <div class="inline-actions">
                <button data-action="refreshMissing" class="secondary">미제출 조회</button>
                <button data-action="sendMissingHomeworkSms">숙제 미제출 전체 문자 발송</button>
              </div>
              <div id="actionPreview" class="action-preview">미제출 학생을 조회하면 발송 대상과 현재 발송 모드가 정리됩니다.</div>
            </div>
          </div>

          <div class="panel">
            <div class="panel-head">
              <h2>스케줄표 입력</h2>
              <span class="badge">수업·숙제</span>
            </div>
            <div class="stack">
              <input id="importFile" type="file" accept=".csv,.tsv,.txt">
              <label>스케줄표<textarea id="importText" class="large"></textarea></label>
              <div class="inline-actions">
                <button data-action="readSelectedFile" class="secondary">파일 읽기</button>
                <button data-action="downloadScheduleTemplate" class="secondary">스케줄 템플릿</button>
                <button data-action="runScheduleImportDryRun" class="secondary">먼저 검토</button>
                <button data-action="createScheduleFromText">수업·숙제 생성</button>
              </div>
              <div id="importPreview" class="action-preview">스케줄표를 붙여넣거나 파일을 읽어오세요.</div>
            </div>
          </div>

          <div class="panel">
            <div class="panel-head">
              <h2>반별 리포트</h2>
              <span class="badge">시험·숙제·출결</span>
            </div>
            <div class="stack">
              <div class="mini-grid">
                <label>반 이름<input id="reportClassName" type="text"></label>
                <label>주 시작일<input id="weekDate" type="date" value="{week_start}"></label>
              </div>
              <div class="inline-actions">
                <button data-action="generateClassReports">반별 리포트 생성</button>
                <button data-action="approveWeekly" class="secondary">승인</button>
              </div>
              <div id="reportPreview" class="action-preview">시험 점수, 숙제, 출결 기록을 모아 주간 리포트 초안을 생성합니다.</div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section id="tab-schedule" class="tab-panel">
      <div class="panel">
        <div class="panel-head">
          <h2>스케줄 표</h2>
          <button data-action="refreshSchedule" class="secondary">스케줄 새로고침</button>
        </div>
        <div class="actions compact">
          <label>시작일<input id="scheduleStart" type="date" value="{week_start}"></label>
          <label>기간<input id="scheduleDays" type="number" min="1" max="62" step="1" value="7"></label>
          <button data-action="loadThisWeekSchedule" class="secondary">이번 주</button>
          <button data-action="loadTodaySchedule" class="secondary">오늘</button>
        </div>
        <div id="scheduleSummary" class="schedule-summary"></div>
        <div id="scheduleTable"></div>
      </div>
    </section>

    <section id="tab-missing" class="tab-panel">
      <div class="grid">
        <div>
          <div class="panel">
            <div class="panel-head">
              <h2>미제출 목록</h2>
              <div class="inline-actions">
                <button data-action="focusMissingCore" class="secondary">문자 발송으로 이동</button>
                <button data-action="refreshMissing" class="secondary">미제출 조회</button>
              </div>
            </div>
            <div id="missingTable" style="margin-top:12px"></div>
          </div>

          <div class="panel">
            <h2>알림 발송 현황</h2>
            <div id="notificationTable"></div>
          </div>
        </div>
      </div>
    </section>

    <section id="tab-reports" class="tab-panel">
      <div class="panel">
        <div class="panel-head">
          <h2>리포트</h2>
          <button data-action="focusReportCore" class="secondary">반별 리포트로 이동</button>
        </div>
        <div class="inline-actions">
          <label>일자<input id="dailyDate" type="date" value="{today}"></label>
          <button data-action="renderDaily">일일 현황 생성</button>
          <button data-action="generateClassReports">반별 리포트 생성</button>
        </div>
      </div>
    </section>

    <section id="tab-memo" class="tab-panel">
      <div class="grid">
        <div>
          <div class="panel">
            <h2>메모</h2>
            <div class="stack">
              <div class="actions">
                <label>ClassIn ID<input id="memoClassinId" type="text"></label>
                <label>태그<input id="memoTag" type="text"></label>
              </div>
              <label>내용<textarea id="memoText"></textarea></label>
              <button data-action="writeMemo">메모 저장</button>
            </div>
          </div>

          <div class="panel">
            <h2>AI 질문</h2>
            <div class="stack">
              <label>질문<textarea id="agentQuestion"></textarea></label>
              <button data-action="askAgent">질문 보내기</button>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section id="tab-settings" class="tab-panel">
      <div class="panel">
        <h2>설정</h2>
        <dl id="settings"></dl>
      </div>
    </section>
      </main>
    </div>
  </div>

  <script id="initial-status" type="application/json">{status_json}</script>
  <script>
    const log = document.querySelector("#log");
    const statusNode = document.querySelector("#initial-status");
    let status = JSON.parse(statusNode.textContent);
    let diagnostics = null;
    let currentSchedule = [];
    let currentMissing = [];

    function writeLog(message, data) {{
      const now = new Date().toLocaleTimeString();
      const detail = data ? "\\n" + JSON.stringify(data, null, 2) : "";
      log.textContent = `[${{now}}] ${{message}}${{detail}}\\n\\n` + log.textContent;
    }}

    function renderStatus(next) {{
      status = next;
      const counts = status.counts || {{}};
      document.querySelector("#incomingCount").textContent = counts.incoming_json || 0;

      const badge = document.querySelector("#configBadge");
      badge.className = status.ok ? "badge ok" : "badge error";
      badge.textContent = status.ok ? "config loaded" : "config needed";
      document.querySelector("#lastUpdated").textContent =
        `updated ${{new Date().toLocaleTimeString()}}`;

      const output = status.output || {{}};
      const webhook = status.webhook || {{}};
      const rows = [
        ["config", status.config_path || ""],
        ["notify", output.notify_mode || ""],
        ["daily", `${{output.daily_mode || ""}} · ${{output.daily_path || ""}}`],
        ["weekly", `${{output.weekly_mode || ""}} · ${{output.weekly_path || ""}}`],
        ["memo", output.memo_mode || ""],
        ["webhook", webhook.port ? `${{webhook.host}}:${{webhook.port}}` : ""],
        ["dump", webhook.dump_dir || ""],
        ["daily html", counts.daily_html || 0],
        ["weekly html", counts.weekly_html || 0],
        ["draft indexes", counts.weekly_indexes || 0],
        ["notify history", counts.notification_history || 0],
      ];
      if (status.error) rows.push(["error", status.error]);
      document.querySelector("#settings").innerHTML = rows
        .map(([k, v]) => `<dt>${{escapeHtml(k)}}</dt><dd>${{escapeHtml(v)}}</dd>`)
        .join("");
    }}

    function escapeHtml(value) {{
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }}

    function statusLabel(status) {{
      const labels = {{
        pending: "대기",
        dry_run: "문구 생성",
        sent: "발송 완료",
        failed: "실패",
        ok: "정상",
        warn: "확인",
        missing: "누락",
        skipped: "건너뜀",
      }};
      return labels[status] || status || "-";
    }}

    function statusPill(status) {{
      const safe = escapeHtml(status || "pending");
      return `<span class="status-pill ${{safe}}">${{escapeHtml(statusLabel(status))}}</span>`;
    }}

    async function callApi(path, payload) {{
      const response = await fetch(path, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(payload || {{}}),
      }});
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || data.message || "request failed");
      }}
      return data;
    }}

    async function refreshStatus() {{
      const response = await fetch("/api/status");
      renderStatus(await response.json());
    }}

    async function loadSituation() {{
      if (!status.ok) {{
        renderMissing({{ summary: {{}}, items: [] }});
        renderNotifications({{ items: [] }});
        return;
      }}
      const params = new URLSearchParams();
      params.set("window_hours", document.querySelector("#windowHours").value || "24");
      const lessonId = document.querySelector("#lessonId").value.trim();
      if (lessonId) params.set("lesson_id", lessonId);

      const missingResponse = await fetch(`/api/missing-homework?${{params.toString()}}`);
      const missingData = await missingResponse.json();
      if (!missingResponse.ok || missingData.ok === false) {{
        throw new Error(missingData.detail || "missing homework load failed");
      }}
      renderMissing(missingData);

      const notifyResponse = await fetch("/api/notifications?limit=80");
      const notifyData = await notifyResponse.json();
      if (!notifyResponse.ok || notifyData.ok === false) {{
        throw new Error(notifyData.detail || "notification load failed");
      }}
      renderNotifications(notifyData);
    }}

    async function loadDiagnostics(live) {{
      const summaryTarget = document.querySelector("#diagnosticSummary");
      const tableTarget = document.querySelector("#diagnosticTable");
      if (!status.ok) {{
        summaryTarget.innerHTML = "";
        tableTarget.innerHTML = `<div class="empty">config.yaml을 읽은 뒤 점검할 수 있습니다.</div>`;
        return;
      }}
      const response = await fetch(`/api/diagnostics?live=${{live ? "true" : "false"}}`);
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || "diagnostics failed");
      }}
      diagnostics = data;
      renderDiagnostics(data);
      return data;
    }}

    function canLoadService(data, service) {{
      const items = (data && data.items) || [];
      return !items.some((item) =>
        item.service === service && ["missing", "failed"].includes(item.status)
      );
    }}

    function canLoadNotion(data) {{
      return canLoadService(data, "Notion");
    }}

    function renderBlockedSituation() {{
      currentMissing = [];
      renderMissing({{ summary: {{}}, items: [] }});
      document.querySelector("#missingTable").innerHTML =
        `<div class="empty">Notion 설정을 먼저 채워야 조회할 수 있습니다.</div>`;
      renderNotifications({{ items: [] }});
    }}

    function renderBlockedSchedule() {{
      currentSchedule = [];
      document.querySelector("#scheduleSummary").innerHTML = "";
      document.querySelector("#scheduleTable").innerHTML =
        `<div class="empty">Notion 설정을 먼저 채워야 스케줄을 조회할 수 있습니다.</div>`;
    }}

    async function loadSchedule() {{
      const data = await loadDiagnostics(false);
      if (!canLoadNotion(data)) {{
        renderBlockedSchedule();
        writeLog("Notion 설정을 먼저 채워야 스케줄을 조회할 수 있습니다.");
        return;
      }}

      const params = new URLSearchParams();
      params.set("start", document.querySelector("#scheduleStart").value);
      params.set("days", document.querySelector("#scheduleDays").value || "7");
      const response = await fetch(`/api/schedule?${{params.toString()}}`);
      const schedule = await response.json();
      if (!response.ok || schedule.ok === false) {{
        throw new Error(schedule.detail || "schedule load failed");
      }}
      renderSchedule(schedule);
      writeLog("스케줄을 갱신했습니다.", schedule.summary);
    }}

    function renderSchedule(data) {{
      const summary = data.summary || {{}};
      const summaryRows = [
        ["수업", summary.total_lessons || 0],
        ["학생 기록", summary.total_student_rows || 0],
        ["지각", summary.late || 0],
        ["결석", summary.absent || 0],
        ["숙제 미제출", summary.homework_missing || 0],
      ];
      document.querySelector("#scheduleSummary").innerHTML = summaryRows
        .map(([label, value]) => `
          <div class="diagnostic-count">
            <span>${{escapeHtml(label)}}</span>
            <strong>${{escapeHtml(value)}}</strong>
          </div>
        `)
        .join("");

      const items = data.items || [];
      currentSchedule = items;
      const target = document.querySelector("#scheduleTable");
      if (!items.length) {{
        target.innerHTML = `<div class="empty">조회 범위 안의 수업 기록이 없습니다.</div>`;
        return;
      }}
      target.innerHTML = `
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>일시</th>
                <th>반</th>
                <th>ClassIn</th>
                <th>학생</th>
                <th>출석</th>
                <th>숙제</th>
                <th>학생 목록</th>
              </tr>
            </thead>
            <tbody>
              ${{items.map((item) => `
                <tr>
                  <td>${{escapeHtml(formatDate(item.date))}}</td>
                  <td>${{escapeHtml((item.class_names || []).join(", ") || "-")}}</td>
                  <td>
                    <span class="badge">${{escapeHtml(item.lesson_classin_id || "-")}}</span><br>
                    <span class="badge">${{escapeHtml(item.course_classin_id || "-")}}</span>
                  </td>
                  <td>${{escapeHtml(item.student_count || 0)}}명</td>
                  <td>
                    출석 ${{escapeHtml((item.attendance || {{}})["출석"] || 0)}} ·
                    지각 ${{escapeHtml((item.attendance || {{}})["지각"] || 0)}} ·
                    결석 ${{escapeHtml((item.attendance || {{}})["결석"] || 0)}}
                  </td>
                  <td>
                    제출 ${{escapeHtml(item.homework_done || 0)}} ·
                    미제출 ${{escapeHtml(item.homework_missing || 0)}} ·
                    미기록 ${{escapeHtml(item.homework_unknown || 0)}}
                  </td>
                  <td><div class="student-list">${{escapeHtml(studentSummary(item.students || []))}}</div></td>
                </tr>
              `).join("")}}
            </tbody>
          </table>
        </div>`;
    }}

    function studentSummary(students) {{
      const names = students.map((student) => {{
        const state = student.attendance && student.attendance !== "출석"
          ? `(${{student.attendance}})`
          : "";
        return `${{student.student_name || "미등록"}}${{state}}`;
      }});
      const text = names.slice(0, 8).join(", ");
      return names.length > 8 ? `${{text}} 외 ${{names.length - 8}}명` : text || "-";
    }}

    function renderDiagnostics(data) {{
      const summary = data.summary || {{}};
      const labels = [
        ["ok", "정상"],
        ["warn", "확인"],
        ["missing", "누락"],
        ["failed", "실패"],
        ["skipped", "건너뜀"],
      ];
      document.querySelector("#diagnosticSummary").innerHTML = labels
        .map(([key, label]) => `
          <div class="diagnostic-count ${{key}}">
            <span>${{label}}</span>
            <strong>${{summary[key] || 0}}</strong>
          </div>
        `)
        .join("");

      const items = data.items || [];
      const target = document.querySelector("#diagnosticTable");
      if (!items.length) {{
        target.innerHTML = `<div class="empty">점검 결과가 없습니다.</div>`;
        return;
      }}
      target.innerHTML = `
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>서비스</th>
                <th>점검</th>
                <th>상태</th>
                <th>내용</th>
                <th>다음 조치</th>
              </tr>
            </thead>
            <tbody>
              ${{items.map((item) => `
                <tr>
                  <td>${{escapeHtml(item.service)}}</td>
                  <td>${{escapeHtml(item.check)}}</td>
                  <td>${{statusPill(item.status)}}</td>
                  <td>${{escapeHtml(item.detail || "-")}}</td>
                  <td>${{escapeHtml(item.next_step || "-")}}</td>
                </tr>
              `).join("")}}
            </tbody>
          </table>
        </div>`;
    }}

    function renderMissing(data) {{
      const summary = data.summary || {{}};
      document.querySelector("#missingCount").textContent = summary.total_missing || 0;
      document.querySelector("#noPhoneCount").textContent = summary.no_parent_phone || 0;
      document.querySelector("#dryRunCount").textContent = summary.dry_run || 0;
      document.querySelector("#sentCount").textContent = summary.sent || 0;
      document.querySelector("#failedCount").textContent = summary.failed || 0;

      const items = data.items || [];
      currentMissing = items;
      const withPhone = items.filter((item) => item.has_parent_phone).length;
      document.querySelector("#actionPreview").innerHTML =
        `<strong>숙제 미제출 전체 문자 발송</strong>대상: ${{items.length}}명\\n연락처 있음: ${{withPhone}}명\\n현재 발송 모드: ${{escapeHtml(notifyMode())}}`;
      const target = document.querySelector("#missingTable");
      if (!items.length) {{
        target.innerHTML = `<div class="empty">조회 범위 안의 숙제 미제출 학생이 없습니다.</div>`;
        return;
      }}
      target.innerHTML = `
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>학생</th>
                <th>반</th>
                <th>수업</th>
                <th>출석</th>
                <th>학부모 연락처</th>
                <th>알림 상태</th>
              </tr>
            </thead>
            <tbody>
              ${{items.map((item) => `
                <tr>
                  <td>${{escapeHtml(item.student_name)}}<br><span class="badge">${{escapeHtml(item.student_classin_id)}}</span></td>
                  <td>${{escapeHtml(item.class_name || "-")}}</td>
                  <td>${{escapeHtml(item.lesson_classin_id || "-")}}<br><span class="badge">${{escapeHtml(formatDate(item.date))}}</span></td>
                  <td>${{escapeHtml(item.attendance || "-")}}</td>
                  <td>${{item.has_parent_phone ? escapeHtml(item.parent_phone) : '<span class="status-pill failed">없음</span>'}}</td>
                  <td>${{statusPill(item.notification_status)}}<br><span class="badge">${{escapeHtml(formatDate(item.notification_at))}}</span></td>
                </tr>
              `).join("")}}
            </tbody>
          </table>
        </div>`;
    }}

    function renderNotifications(data) {{
      const items = data.items || [];
      const target = document.querySelector("#notificationTable");
      if (!items.length) {{
        target.innerHTML = `<div class="empty">아직 알림 생성/발송 기록이 없습니다.</div>`;
        return;
      }}
      target.innerHTML = `
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>시간</th>
                <th>학생</th>
                <th>수신자</th>
                <th>상태</th>
                <th>문구</th>
              </tr>
            </thead>
            <tbody>
              ${{items.map((item) => `
                <tr>
                  <td>${{escapeHtml(formatDate(item.created_at))}}</td>
                  <td>${{escapeHtml(item.student_name || "미등록")}}<br><span class="badge">${{escapeHtml(item.student_classin_id || "")}}</span></td>
                  <td>${{escapeHtml(item.parent_phone || "-")}}</td>
                  <td>${{statusPill(item.status)}}<br><span class="badge">${{escapeHtml(item.provider || "-")}}</span></td>
                  <td>${{escapeHtml(shorten(item.message || "", 90))}}</td>
                </tr>
              `).join("")}}
            </tbody>
          </table>
        </div>`;
    }}

    function formatDate(value) {{
      if (!value) return "-";
      return String(value).replace("T", " ").replace("+00:00", "").replace("Z", "");
    }}

    function shorten(value, max) {{
      const text = String(value);
      return text.length > max ? text.slice(0, max - 1) + "..." : text;
    }}

    function localIsoDate(value) {{
      const offset = value.getTimezoneOffset() * 60000;
      return new Date(value.getTime() - offset).toISOString().slice(0, 10);
    }}

    function weekStartIso(value) {{
      const date = new Date(value);
      const day = (date.getDay() + 6) % 7;
      date.setDate(date.getDate() - day);
      return localIsoDate(date);
    }}

    function activateTab(tab) {{
      document.querySelectorAll(".tab-button").forEach((button) => {{
        button.classList.toggle("active", button.dataset.tab === tab);
      }});
      document.querySelectorAll(".tab-panel").forEach((panel) => {{
        panel.classList.toggle("active", panel.id === `tab-${{tab}}`);
      }});
      document.querySelector("#viewTitle").textContent = viewTitles[tab] || "대시보드";
      document.querySelector("#viewMeta").textContent = localIsoDate(new Date());
    }}

    function setActionMode(mode, html) {{
      document.querySelector("#actionModeBadge").textContent = mode;
      document.querySelector("#actionPreview").innerHTML = html;
    }}

    function notifyMode() {{
      return ((status.output || {{}}).notify_mode || "-");
    }}

    function focusMissingCore() {{
      activateTab("actions");
      document.querySelector("#windowHours").focus();
      const withPhone = currentMissing.filter((item) => item.has_parent_phone).length;
      setActionMode(
        "미제출 문자",
        `<strong>숙제 미제출 전체 문자 발송</strong>대상: ${{currentMissing.length}}명\\n연락처 있음: ${{withPhone}}명\\n현재 발송 모드: ${{escapeHtml(notifyMode())}}`
      );
    }}

    function focusScheduleCore() {{
      activateTab("actions");
      document.querySelector("#importText").focus();
      document.querySelector("#importPreview").innerHTML =
        `<strong>스케줄표로 수업·숙제 생성</strong>스케줄표를 붙여넣고 먼저 검토한 뒤 생성하세요.`;
    }}

    function focusReportCore() {{
      activateTab("actions");
      document.querySelector("#reportClassName").focus();
      document.querySelector("#reportPreview").innerHTML =
        `<strong>반별 리포트 생성</strong>반 이름과 주 시작일을 확인한 뒤 리포트 초안을 만듭니다.`;
    }}

    async function sendMissingHomeworkSms() {{
      const data = await loadDiagnostics(false);
      if (!canLoadNotion(data)) {{
        renderBlockedSituation();
        writeLog("Notion 설정을 먼저 채워야 미제출 문자를 처리할 수 있습니다.");
        return;
      }}
      if (!currentMissing.length) {{
        await loadSituation();
      }}
      const withPhone = currentMissing.filter((item) => item.has_parent_phone).length;
      const mode = notifyMode();
      if (mode === "live") {{
        const ok = window.confirm(
          `실제 문자 발송 모드입니다. 연락처가 있는 미제출자 ${{withPhone}}명에게 발송합니다.`
        );
        if (!ok) return;
      }}
      const result = await callApi("/api/sweep-missing-homework", {{
        window_hours: document.querySelector("#windowHours").value,
        lesson_id: document.querySelector("#lessonId").value,
      }});
      await loadSituation();
      setActionMode(
        "문자 처리",
        `<strong>숙제 미제출 전체 문자 발송</strong>처리: ${{escapeHtml(result.count || 0)}}건\\n모드: ${{escapeHtml(mode)}}`
      );
      writeLog(result.message, result);
    }}

    function inspectDelimited(text) {{
      const lines = String(text)
        .split(/\\r?\\n/)
        .filter((line) => line.trim());
      const first = lines[0] || "";
      const delimiter = first.includes("\\t") ? "\\t" : ",";
      const headers = first
        .split(delimiter)
        .map((header) => header.trim())
        .filter(Boolean);
      return {{
        rows: Math.max(lines.length - 1, 0),
        headers,
        delimiter: delimiter === "\\t" ? "TSV" : "CSV",
      }};
    }}

    function renderImportPreview(text, source) {{
      const parsed = inspectDelimited(text);
      document.querySelector("#importPreview").innerHTML =
        `<strong>${{escapeHtml(source || "가져오기 데이터")}}</strong>` +
        `형식: ${{escapeHtml(parsed.delimiter)}}\\n` +
        `행: ${{parsed.rows}}\\n` +
        `헤더: ${{escapeHtml(parsed.headers.join(", ") || "-")}}`;
    }}

    async function readSelectedFile() {{
      const input = document.querySelector("#importFile");
      const file = input.files && input.files[0];
      if (!file) throw new Error("읽을 파일을 선택하세요.");
      const text = await file.text();
      document.querySelector("#importText").value = text;
      renderImportPreview(text, file.name);
      writeLog("가져오기 파일을 읽었습니다.", {{ name: file.name, size: file.size }});
    }}

    function csvEscape(value) {{
      const text = String(value ?? "");
      return /[",\\n\\r]/.test(text) ? `"${{text.replaceAll('"', '""')}}"` : text;
    }}

    function downloadCsv(filename, headers, rows) {{
      const csv = "\\ufeff" + [headers, ...rows]
        .map((row) => row.map(csvEscape).join(","))
        .join("\\n");
      const blob = new Blob([csv], {{ type: "text/csv;charset=utf-8" }});
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.style.display = "none";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    }}

    function exportScheduleCsv() {{
      if (!currentSchedule.length) throw new Error("먼저 스케줄 표를 조회하세요.");
      const headers = [
        "date",
        "class_names",
        "course_classin_id",
        "lesson_classin_id",
        "student_count",
        "attendance",
        "homework_done",
        "homework_missing",
        "homework_unknown",
        "students",
      ];
      const rows = currentSchedule.map((item) => [
        formatDate(item.date),
        (item.class_names || []).join("|"),
        item.course_classin_id || "",
        item.lesson_classin_id || "",
        item.student_count || 0,
        JSON.stringify(item.attendance || {{}}),
        item.homework_done || 0,
        item.homework_missing || 0,
        item.homework_unknown || 0,
        studentSummary(item.students || []),
      ]);
      downloadCsv(`schedule-${{document.querySelector("#scheduleStart").value || localIsoDate(new Date())}}.csv`, headers, rows);
      writeLog("스케줄 CSV를 만들었습니다.", {{ rows: rows.length }});
    }}

    function exportMissingCsv() {{
      if (!currentMissing.length) throw new Error("먼저 미제출 목록을 조회하세요.");
      const headers = [
        "student_classin_id",
        "student_name",
        "class_name",
        "parent_phone",
        "lesson_classin_id",
        "date",
        "attendance",
        "notification_status",
      ];
      const rows = currentMissing.map((item) => [
        item.student_classin_id || "",
        item.student_name || "",
        item.class_name || "",
        item.parent_phone || "",
        item.lesson_classin_id || "",
        formatDate(item.date),
        item.attendance || "",
        item.notification_status || "",
      ]);
      downloadCsv(`missing-homework-${{localIsoDate(new Date())}}.csv`, headers, rows);
      writeLog("미제출 CSV를 만들었습니다.", {{ rows: rows.length }});
    }}

    function downloadScheduleTemplate() {{
      const headers = [
        "course_name",
        "teacher",
        "date",
        "start",
        "end",
        "lesson_title",
        "homework_title",
        "homework_due",
      ];
      const rows = [[
        "고2 수학 A반",
        "김선생",
        "2026-05-06",
        "19:00",
        "21:00",
        "수1 - 지수함수 1",
        "워크북 p.42-48",
        "2026-05-08 23:59",
      ]];
      downloadCsv("schedule-template.csv", headers, rows);
      writeLog("스케줄 템플릿을 만들었습니다.");
    }}

    function renderDryRunResult(title, data) {{
      const summary = data.summary || {{}};
      const errors = data.errors || [];
      document.querySelector("#importPreview").innerHTML =
        `<strong>${{escapeHtml(title)}}</strong>` +
        Object.entries(summary)
          .map(([key, value]) => `${{escapeHtml(key)}}: ${{escapeHtml(value)}}`)
          .join("\\n") +
        (errors.length
          ? `\\n\\n오류\\n${{escapeHtml(errors.slice(0, 8).join("\\n"))}}`
          : "");
    }}

    async function runScheduleImportDryRun() {{
      const text = document.querySelector("#importText").value.trim();
      if (!text) throw new Error("스케줄 dry-run에 사용할 CSV / 표 데이터가 필요합니다.");
      const data = await callApi("/api/create-schedule", {{
        schedule_text: text,
        dry_run: true,
      }});
      renderDryRunResult("스케줄 검토", data);
      writeLog(data.message, data.summary);
    }}

    async function createScheduleFromText() {{
      const text = document.querySelector("#importText").value.trim();
      if (!text) throw new Error("생성할 스케줄표가 필요합니다.");
      const ok = window.confirm("ClassIn에 실제 수업과 숙제를 생성합니다.");
      if (!ok) return;
      const data = await callApi("/api/create-schedule", {{
        schedule_text: text,
        dry_run: false,
      }});
      renderDryRunResult("수업·숙제 생성", data);
      writeLog(data.message, data.summary);
    }}

    async function generateClassReports() {{
      const data = await callApi("/api/generate-class-reports", {{
        class_name: document.querySelector("#reportClassName").value,
        week: document.querySelector("#weekDate").value,
      }});
      document.querySelector("#reportPreview").innerHTML =
        `<strong>반별 리포트 생성</strong>${{escapeHtml(data.message)}}`;
      writeLog(data.message, data);
      await refreshStatus();
    }}

    const viewTitles = {{
      dashboard: "대시보드",
      actions: "핵심 기능",
      schedule: "스케줄",
      missing: "미제출",
      reports: "리포트",
      memo: "메모·AI",
      settings: "설정",
    }};

    const actions = {{
      async focusMissingCore() {{
        focusMissingCore();
      }},
      async focusScheduleCore() {{
        focusScheduleCore();
      }},
      async focusReportCore() {{
        focusReportCore();
      }},
      async sendMissingHomeworkSms() {{
        await sendMissingHomeworkSms();
      }},
      async createScheduleFromText() {{
        await createScheduleFromText();
      }},
      async generateClassReports() {{
        await generateClassReports();
      }},
      async exportScheduleCsv() {{
        exportScheduleCsv();
      }},
      async exportMissingCsv() {{
        exportMissingCsv();
      }},
      async readSelectedFile() {{
        await readSelectedFile();
      }},
      async downloadScheduleTemplate() {{
        downloadScheduleTemplate();
      }},
      async runScheduleImportDryRun() {{
        await runScheduleImportDryRun();
      }},
      async renderDaily() {{
        const data = await callApi("/api/render-daily", {{
          date: document.querySelector("#dailyDate").value,
        }});
        writeLog(data.message, data);
        await refreshStatus();
      }},
      async generateWeekly() {{
        const data = await callApi("/api/generate-weekly-drafts");
        writeLog(data.message, data);
        await refreshStatus();
      }},
      async approveWeekly() {{
        const data = await callApi("/api/approve-weekly", {{
          week: document.querySelector("#weekDate").value,
        }});
        writeLog(data.message, data);
        await refreshStatus();
      }},
      async sweepMissing() {{
        const data = await callApi("/api/sweep-missing-homework", {{
          window_hours: document.querySelector("#windowHours").value,
          lesson_id: document.querySelector("#lessonId").value,
        }});
        writeLog(data.message, data);
        await loadSituation();
      }},
      async refreshMissing() {{
        const data = await loadDiagnostics(false);
        if (!canLoadNotion(data)) {{
          renderBlockedSituation();
          writeLog("Notion 설정을 먼저 채워야 조회할 수 있습니다.");
          return;
        }}
        await loadSituation();
        writeLog("미제출/알림 상황을 갱신했습니다.");
      }},
      async refreshSchedule() {{
        await loadSchedule();
      }},
      async loadThisWeekSchedule() {{
        document.querySelector("#scheduleStart").value = weekStartIso(new Date());
        document.querySelector("#scheduleDays").value = "7";
        await loadSchedule();
      }},
      async loadTodaySchedule() {{
        document.querySelector("#scheduleStart").value = localIsoDate(new Date());
        document.querySelector("#scheduleDays").value = "1";
        await loadSchedule();
      }},
      async writeMemo() {{
        const data = await callApi("/api/write-memo", {{
          classin_id: document.querySelector("#memoClassinId").value,
          tag: document.querySelector("#memoTag").value,
          text: document.querySelector("#memoText").value,
        }});
        writeLog(data.message, data);
      }},
      async askAgent() {{
        const data = await callApi("/api/agent", {{
          question: document.querySelector("#agentQuestion").value,
        }});
        writeLog(data.answer || data.message, {{ message: data.message }});
      }},
      async refreshStatus() {{
        await refreshStatus();
        const data = await loadDiagnostics(false);
        if (canLoadNotion(data)) {{
          await loadSituation();
          await loadSchedule();
        }} else {{
          renderBlockedSituation();
          renderBlockedSchedule();
        }}
        writeLog("상태를 갱신했습니다.");
      }},
      async refreshDiagnostics() {{
        const data = await loadDiagnostics(false);
        writeLog("설정 점검을 완료했습니다.", {{
          ready: data.ready,
          summary: data.summary,
        }});
      }},
      async runLiveDiagnostics() {{
        const data = await loadDiagnostics(true);
        writeLog("실 API 점검을 완료했습니다.", {{
          ready: data.ready,
          summary: data.summary,
        }});
      }},
    }};

    document.addEventListener("click", async (event) => {{
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      const action = actions[button.dataset.action];
      if (!action) return;
      button.disabled = true;
      try {{
        await action();
      }} catch (error) {{
        writeLog(error.message);
      }} finally {{
        button.disabled = false;
      }}
    }});

    document.addEventListener("click", (event) => {{
      const tabButton = event.target.closest("button[data-tab]");
      if (!tabButton) return;
      activateTab(tabButton.dataset.tab);
    }});

    renderStatus(status);
    loadDiagnostics(false)
      .then((data) => {{
        if (canLoadNotion(data)) {{
          return Promise.all([loadSituation(), loadSchedule()]);
        }}
        renderBlockedSituation();
        renderBlockedSchedule();
      }})
      .catch((error) => writeLog(error.message));
  </script>
</body>
</html>"""


def _week_start(value: date_cls) -> date_cls:
    return date_cls.fromordinal(value.toordinal() - value.weekday())


def _json_for_script(value: dict[str, Any]) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
