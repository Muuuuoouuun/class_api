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
from .pipelines.missing_homework import (
    missing_homework_selection_key,
    query_missing_homework,
    sweep_missing_homework,
)
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

    @app.get("/api/report-targets")
    async def api_report_targets(class_name: str | None = None) -> dict:
        cfg = _require_config(state)
        _require_service_config(cfg, "Notion")
        target_class = (class_name or "").strip() or None
        try:
            students = NotionRepo.from_config(cfg).list_active_students()
        except Exception as exc:
            raise _service_error("리포트 대상 조회", exc) from exc
        if target_class:
            students = [student for student in students if student.class_name == target_class]
        return _report_targets_payload(students, class_name=target_class)

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
        selection_keys = _optional_string_list(payload, "selection_keys", "문자 발송 대상")
        try:
            count = sweep_missing_homework(
                cfg,
                window_hours=window_hours,
                lesson_id=lesson_id,
                selection_keys=selection_keys,
            )
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
        student_classin_ids = _optional_string_list(
            payload,
            "student_classin_ids",
            "리포트 대상 학생",
        )
        reference = None
        if raw_week:
            try:
                reference = datetime.fromisoformat(raw_week).replace(tzinfo=timezone.utc)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="week는 YYYY-MM-DD 형식이어야 합니다.") from exc
        try:
            count = generate_drafts(
                cfg,
                reference=reference,
                class_name=class_name,
                student_classin_ids=student_classin_ids,
            )
        except Exception as exc:
            raise _service_error("반별 리포트 생성", exc) from exc
        return _ok(
            f"{class_name or '전체 반'} 리포트 드래프트 {count}건을 생성했습니다.",
            count=count,
            class_name=class_name,
            selected=len(student_classin_ids) if student_classin_ids is not None else None,
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


def _optional_string_list(
    payload: dict[str, Any],
    key: str,
    label: str,
) -> list[str] | None:
    if key not in payload:
        return None
    raw = payload.get(key)
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail=f"{key} 값은 배열이어야 합니다.")
    values = [str(item).strip() for item in raw if str(item).strip()]
    if not values:
        raise HTTPException(status_code=400, detail=f"{label}을 선택하세요.")
    return values


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
                "selection_key": missing_homework_selection_key(row),
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


def _report_targets_payload(students: list[Any], *, class_name: str | None) -> dict[str, Any]:
    items = [
        {
            "student_classin_id": student.classin_id,
            "student_name": student.name,
            "class_name": student.class_name or "",
            "has_parent_phone": bool(student.parent_phone),
        }
        for student in students
    ]
    items.sort(key=lambda item: (item["class_name"], item["student_name"], item["student_classin_id"]))
    classes = sorted({item["class_name"] for item in items if item["class_name"]})
    return {
        "ok": True,
        "class_name": class_name,
        "summary": {
            "total": len(items),
            "classes": len(classes),
            "with_parent_phone": sum(1 for item in items if item["has_parent_phone"]),
        },
        "classes": classes,
        "items": items,
    }


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
    academy_name = status.get("academy") or "ClassIn Toolkit"
    title = html.escape(academy_name)
    brand_initial = html.escape((academy_name.strip() or "C")[:1])
    notify_mode = html.escape((status.get("output") or {}).get("notify_mode") or "dry_run")
    return f"""<!doctype html>
<html lang="ko" data-theme="light">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ClassIn 운영 콘솔 · {title}</title>
  <link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable.min.css">
  <style>
    :root {{
      color-scheme: light;
      --primary: #2A6FDB;
      --primary-d: #1f57b0;
      --primary-soft: #eaf1fc;
      --primary-softer: #f4f8fe;

      --bg: #f6f7f9;
      --panel: #ffffff;
      --surface-2: #fbfcfd;
      --line: #e7eaef;
      --line-strong: #d6dbe3;
      --text: #1a2230;
      --muted: #57606e;
      --text-3: #8a93a2;

      --ok: #1f8a5b;
      --ok-soft: #e6f4ed;
      --accent: #c2790c;
      --accent-soft: #fdf3e2;
      --danger: #d23f3f;
      --danger-soft: #fbeaea;
      --info: #2a6fdb;
      --info-soft: #eaf1fc;

      --radius: 12px;
      --radius-sm: 9px;
      --shadow-sm: 0 1px 2px rgba(20,30,50,.05), 0 1px 1px rgba(20,30,50,.04);
      --shadow-md: 0 4px 16px rgba(20,30,50,.08), 0 1px 3px rgba(20,30,50,.05);
      --shadow-lg: 0 20px 50px rgba(15,25,45,.22), 0 6px 16px rgba(15,25,45,.12);

      --sidebar-w: 232px;
      --font: "Pretendard Variable", Pretendard, -apple-system, "Apple SD Gothic Neo", system-ui, sans-serif;
      --mono: "SF Mono", ui-monospace, Menlo, Consolas, monospace;
    }}
    [data-theme="dark"] {{
      color-scheme: dark;
      --bg: #0e1320;
      --panel: #161c2b;
      --surface-2: #131927;
      --line: #263049;
      --line-strong: #33405e;
      --text: #e9edf5;
      --muted: #a6b0c2;
      --text-3: #6f7a90;
      --primary-soft: #1a2842;
      --primary-softer: #151d31;
      --ok-soft: #142a22;
      --accent-soft: #2e2414;
      --danger-soft: #2e1a1c;
      --info-soft: #1a2842;
      --shadow-sm: 0 1px 2px rgba(0,0,0,.3);
      --shadow-md: 0 4px 16px rgba(0,0,0,.4);
      --shadow-lg: 0 24px 60px rgba(0,0,0,.6);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-width: 320px;
      background: var(--bg);
      color: var(--text);
      font-family: var(--font);
      font-size: 14px;
      line-height: 1.5;
      letter-spacing: -0.01em;
      -webkit-font-smoothing: antialiased;
    }}
    h1, h2 {{ letter-spacing: -0.02em; }}
    ::selection {{ background: var(--primary-soft); }}

    .app-shell {{
      display: grid;
      grid-template-columns: var(--sidebar-w) minmax(0, 1fr);
      min-height: 100vh;
    }}

    /* ── Sidebar ─────────────────────────── */
    .sidebar {{
      position: sticky;
      top: 0;
      height: 100vh;
      display: flex;
      flex-direction: column;
      padding: 0;
      background: var(--panel);
      border-right: 1px solid var(--line);
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 10px;
      height: 60px;
      padding: 0 18px;
      border-bottom: 1px solid var(--line);
      flex-shrink: 0;
    }}
    .brand-mark {{
      display: grid;
      place-items: center;
      width: 30px;
      height: 30px;
      border-radius: 8px;
      background: linear-gradient(135deg, var(--primary), var(--primary-d));
      color: #fff;
      font-weight: 800;
      font-size: 15px;
      box-shadow: var(--shadow-sm);
      flex-shrink: 0;
    }}
    .brand > div {{ min-width: 0; }}
    .brand h1 {{
      margin: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--text);
      font-size: 14px;
      font-weight: 700;
    }}
    .brand span {{
      display: block;
      margin-top: 1px;
      color: var(--text-3);
      font-size: 11px;
      white-space: nowrap;
    }}
    .sidebar .tabs {{
      display: flex;
      flex-direction: column;
      gap: 2px;
      flex: 1;
      margin: 0;
      padding: 10px;
      overflow-y: auto;
      border: 0;
      background: transparent;
      box-shadow: none;
    }}
    .nav-sep {{
      padding: 14px 12px 5px;
      color: var(--text-3);
      font-size: 10.5px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .06em;
    }}
    .sidebar .tab-button {{
      display: flex;
      align-items: center;
      gap: 11px;
      width: 100%;
      min-height: 0;
      padding: 9px 11px;
      border: none;
      border-radius: var(--radius-sm);
      background: none;
      color: var(--muted);
      font-size: 13.5px;
      font-weight: 550;
      text-align: left;
      white-space: nowrap;
    }}
    .sidebar .tab-button .ic {{ width: 19px; height: 19px; flex-shrink: 0; }}
    .sidebar .tab-button:hover {{ background: var(--surface-2); color: var(--text); }}
    .sidebar .tab-button.active {{
      background: var(--primary-soft);
      color: var(--primary);
      font-weight: 650;
    }}
    [data-theme="dark"] .sidebar .tab-button.active {{ color: #7da9ee; }}
    .sidebar-footer {{
      flex-shrink: 0;
      margin: 0;
      padding: 12px;
      border-top: 1px solid var(--line);
    }}
    .server-pill {{
      display: flex;
      align-items: center;
      gap: 9px;
      padding: 8px 10px;
      border-radius: var(--radius-sm);
      background: var(--surface-2);
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
      overflow: hidden;
    }}
    .dot {{ width: 8px; height: 8px; border-radius: 99px; flex-shrink: 0; }}
    .dot.live {{ background: var(--ok); box-shadow: 0 0 0 3px var(--ok-soft); animation: pulse 2.4s ease-in-out infinite; }}
    @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:.45}} }}
    .sidebar-footer .badge {{ width: fit-content; margin-bottom: 8px; }}
    .last-updated {{ color: var(--text-3); font-size: 11.5px; line-height: 1.35; }}

    /* ── Workspace ─────────────────────────── */
    .workspace {{ min-width: 0; display: flex; flex-direction: column; }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 5;
      display: flex;
      align-items: center;
      gap: 14px;
      height: 60px;
      padding: 0 22px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
    }}
    .topbar-title {{ display: flex; align-items: baseline; gap: 10px; min-width: 0; }}
    .topbar-title h2 {{ margin: 0; font-size: 16px; font-weight: 700; }}
    .topbar-title span {{ color: var(--text-3); font-size: 13px; }}
    .topbar .inline-actions {{ margin-left: auto; }}
    .icon-btn {{
      display: grid;
      place-items: center;
      width: 34px;
      height: 34px;
      min-height: 0;
      padding: 0;
      border: 1px solid var(--line);
      border-radius: 9px;
      background: var(--panel);
      color: var(--muted);
    }}
    .icon-btn:hover {{ background: var(--surface-2); color: var(--text); border-color: var(--line-strong); }}
    .topbar-user {{ display: flex; align-items: center; gap: 8px; }}
    .topbar-user .avatar {{
      display: grid;
      place-items: center;
      width: 30px;
      height: 30px;
      border-radius: 8px;
      background: var(--primary);
      color: #fff;
      font-weight: 700;
      font-size: 12px;
    }}
    .topbar-user div {{ line-height: 1.2; }}
    .topbar-user .nm {{ font-size: 12.5px; font-weight: 650; }}
    .topbar-user .rl {{ font-size: 11px; color: var(--text-3); }}
    .workspace main {{
      flex: 1;
      width: auto;
      margin: 0;
      padding: 24px;
    }}

    /* ── Metrics ─────────────────────────── */
    .status-strip {{
      display: flex;
      gap: 0;
      margin: -24px -24px 20px;
      padding: 0 22px;
      height: 64px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      overflow-x: auto;
    }}
    .metric {{
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 2px;
      min-width: 118px;
      padding: 0 22px;
      border: none;
      border-right: 1px solid var(--line);
      border-radius: 0;
      background: transparent;
      box-shadow: none;
    }}
    .metric:first-child {{ padding-left: 0; }}
    .metric span {{ order: 2; color: var(--text-3); font-size: 11.5px; font-weight: 550; }}
    .metric strong {{
      order: 1;
      display: flex;
      align-items: center;
      gap: 7px;
      margin: 0;
      font-size: 21px;
      font-weight: 750;
      line-height: 1;
    }}
    .metric strong::before {{
      content: "";
      width: 6px;
      height: 6px;
      border-radius: 99px;
      background: var(--text-3);
    }}
    .metric.warn strong::before {{ background: var(--accent); }}
    .metric.alert strong::before, .metric.danger strong::before {{ background: var(--danger); }}
    .metric.info strong::before {{ background: var(--info); }}
    .metric.ok strong::before {{ background: var(--ok); }}
    .metric.warn strong, .metric.alert strong, .metric.danger strong,
    .metric.info strong, .metric.ok strong {{ color: var(--text); }}

    /* ── Tabs / panels ─────────────────────────── */
    .tab-panel {{ display: none; }}
    .tab-panel.active {{ display: block; }}

    .panel {{
      padding: var(--pad, 20px);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow-sm);
    }}
    .panel + .panel {{ margin-top: 16px; }}
    h2 {{ margin: 0 0 14px; font-size: 15px; font-weight: 700; line-height: 1.2; }}
    .panel-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }}
    .panel-head h2 {{ margin: 0; }}
    .grid {{
      display: grid;
      grid-template-columns: 1.2fr .8fr;
      gap: 16px;
      align-items: start;
    }}

    /* ── Buttons ─────────────────────────── */
    button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
      min-height: 38px;
      padding: 0 16px;
      border: 1px solid transparent;
      border-radius: var(--radius-sm);
      background: var(--primary);
      color: #fff;
      font: inherit;
      font-size: 13.5px;
      font-weight: 650;
      white-space: nowrap;
      cursor: pointer;
      transition: filter .12s, background .12s, border-color .12s;
    }}
    button:hover {{ filter: brightness(1.06); background: var(--primary); }}
    button.secondary {{
      background: var(--panel);
      border-color: var(--line);
      color: var(--text);
    }}
    button.secondary:hover {{ filter: none; background: var(--surface-2); border-color: var(--line-strong); }}
    button:disabled {{ cursor: progress; opacity: .6; filter: none; }}

    /* ── Action hub ─────────────────────────── */
    .action-layout {{ display: grid; gap: 16px; align-items: start; }}
    .core-panel {{ padding: var(--pad, 20px); }}
    .core-panel .panel-head {{ margin-bottom: 14px; }}
    .core-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }}
    .core-button {{
      display: grid;
      grid-template-columns: 44px minmax(0, 1fr);
      grid-template-rows: auto auto;
      gap: 4px 12px;
      align-content: start;
      min-height: 92px;
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--panel);
      color: var(--text);
      text-align: left;
      box-shadow: var(--shadow-sm);
      transition: border-color .14s, box-shadow .14s, transform .14s;
    }}
    .core-button:hover {{ background: var(--panel); filter: none; border-color: var(--primary); box-shadow: var(--shadow-md); transform: translateY(-2px); }}
    .core-button strong {{ display: block; min-width: 0; overflow-wrap: anywhere; font-size: 15px; font-weight: 700; line-height: 1.3; }}
    .core-button span {{ display: block; color: var(--muted); font-size: 12.5px; line-height: 1.45; font-weight: 500; }}
    .core-button .core-number {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      grid-row: 1 / span 2;
      width: 44px;
      height: 44px;
      border-radius: 11px;
      background: var(--primary);
      color: #fff;
      font-size: 17px;
      font-weight: 800;
      line-height: 1;
    }}

    .action-controls {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 16px;
      align-items: start;
    }}
    .action-controls .panel {{ margin-top: 0; }}
    .action-button {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
      min-height: 66px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--panel);
      color: var(--text);
      text-align: left;
    }}
    .action-button:hover {{ filter: none; background: var(--surface-2); }}
    .action-button strong, .action-button span {{ display: block; min-width: 0; overflow-wrap: anywhere; }}
    .action-button span {{ margin-top: 4px; color: var(--muted); font-size: 12px; line-height: 1.35; font-weight: 500; }}
    .action-tag {{
      display: inline-flex;
      align-items: center;
      align-self: center;
      min-height: 23px;
      padding: 0 9px;
      border-radius: 7px;
      background: var(--surface-2);
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 11.5px;
      font-weight: 650;
      white-space: nowrap;
    }}

    .actions {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .actions.compact {{ grid-template-columns: repeat(4, minmax(0, 1fr)); align-items: end; }}
    .action-grid {{ display: grid; gap: 10px; }}
    .mini-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .row {{ display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: end; }}
    .stack {{ display: grid; gap: 12px; }}
    .range-picker {{ display: grid; gap: 8px; }}
    .field-hint {{ color: var(--text); font-weight: 700; }}

    .segmented {{
      display: inline-flex;
      gap: 2px;
      padding: 3px;
      border: 1px solid var(--line);
      border-radius: 9px;
      background: var(--surface-2);
    }}
    .segmented button {{
      flex: 1;
      min-height: 30px;
      padding: 5px 13px;
      border: none;
      border-radius: 6px;
      background: none;
      color: var(--muted);
      font-size: 12.5px;
      font-weight: 600;
    }}
    .segmented button:hover {{ filter: none; background: transparent; }}
    .segmented button.active {{ background: var(--panel); color: var(--text); box-shadow: var(--shadow-sm); }}
    [data-theme="dark"] .segmented button.active {{ background: var(--line); }}

    /* ── Forms ─────────────────────────── */
    label {{ display: grid; gap: 6px; color: var(--muted); font-size: 12.5px; font-weight: 600; line-height: 1.3; }}
    .checkbox-field {{
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      min-height: 48px;
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      color: var(--text);
    }}
    input, select, textarea {{
      width: 100%;
      min-height: 38px;
      padding: 9px 12px;
      border: 1px solid var(--line-strong);
      border-radius: var(--radius-sm);
      background: var(--panel);
      color: var(--text);
      font: inherit;
      font-size: 13.5px;
    }}
    textarea {{ min-height: 92px; resize: vertical; }}
    textarea.large {{ min-height: 150px; font-family: var(--mono); font-size: 12.5px; line-height: 1.6; }}
    input:focus, select:focus, textarea:focus {{
      outline: none;
      border-color: var(--primary);
      box-shadow: 0 0 0 3px var(--primary-soft);
    }}
    input[type="file"] {{ padding: 7px 12px; }}
    .checkbox-field input[type="checkbox"] {{
      width: 18px;
      min-height: 18px;
      padding: 0;
      accent-color: var(--primary);
    }}

    .panel-head, .inline-actions {{ }}
    .inline-actions {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}

    .action-preview {{
      min-height: 86px;
      padding: 11px 13px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface-2);
      color: var(--muted);
      font-size: 12.5px;
      line-height: 1.6;
      white-space: pre-wrap;
    }}
    .action-preview strong {{ display: block; margin-bottom: 4px; color: var(--text); font-size: 14px; font-weight: 700; }}
    .action-preview ul {{ margin: 8px 0 0; padding-left: 18px; }}

    .selection-summary {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      min-height: 38px;
      padding: 8px 12px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface-2);
      color: var(--muted);
      font-size: 12.5px;
    }}
    .selection-summary strong {{ color: var(--text); }}
    .selectable-list {{
      display: grid;
      gap: 6px;
      max-height: 220px;
      overflow: auto;
      padding: 8px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface-2);
    }}
    .selectable-row {{
      display: grid;
      grid-template-columns: 22px minmax(0, 1fr);
      gap: 10px;
      align-items: start;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--panel);
      color: var(--text);
      font-size: 12.5px;
      line-height: 1.4;
    }}
    .selectable-row input[type="checkbox"],
    .table-wrap input[type="checkbox"] {{
      width: 18px;
      min-height: 18px;
      margin: 1px 0 0;
      padding: 0;
      accent-color: var(--primary);
    }}
    .selectable-row strong {{ display: block; margin-bottom: 2px; font-size: 13px; font-weight: 650; line-height: 1.25; }}
    .selectable-row span {{ display: block; color: var(--muted); overflow-wrap: anywhere; }}
    .selectable-row.is-disabled {{ opacity: .55; background: var(--surface-2); }}

    .log {{
      min-height: 220px;
      max-height: 420px;
      overflow: auto;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: #0e1320;
      color: #d8def0;
      font: 12.5px/1.6 var(--mono);
      white-space: pre-wrap;
    }}

    /* ── Tables ─────────────────────────── */
    .table-wrap {{ overflow: auto; border: 1px solid var(--line); border-radius: var(--radius-sm); }}
    table {{ width: 100%; border-collapse: collapse; min-width: 760px; font-size: 13px; }}
    th, td {{ padding: 11px 14px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{
      background: var(--surface-2);
      color: var(--text-3);
      font-size: 11.5px;
      font-weight: 650;
      text-transform: uppercase;
      letter-spacing: .03em;
      white-space: nowrap;
    }}
    tbody tr:hover {{ background: var(--surface-2); }}
    tr:last-child td {{ border-bottom: 0; }}
    .empty {{
      padding: 40px 20px;
      color: var(--text-3);
      text-align: center;
      border: 1px dashed var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface-2);
    }}

    /* ── Chips & pills ─────────────────────────── */
    .status-pill, .badge {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      min-height: 23px;
      padding: 0 9px;
      border-radius: 7px;
      border: 1px solid transparent;
      font-size: 11.5px;
      font-weight: 650;
      white-space: nowrap;
    }}
    .badge {{ background: var(--surface-2); border-color: var(--line); color: var(--muted); }}
    .badge.ok {{ color: var(--ok); background: var(--ok-soft); border-color: transparent; }}
    .badge.error {{ color: var(--danger); background: var(--danger-soft); border-color: transparent; }}
    .status-pill.pending {{ color: var(--accent); background: var(--accent-soft); }}
    .status-pill.dry_run {{ color: var(--info); background: var(--info-soft); }}
    .status-pill.sent, .status-pill.ok {{ color: var(--ok); background: var(--ok-soft); }}
    .status-pill.failed, .status-pill.missing {{ color: var(--danger); background: var(--danger-soft); }}
    .status-pill.warn {{ color: var(--accent); background: var(--accent-soft); }}
    .status-pill.skipped {{ color: var(--muted); background: var(--surface-2); border-color: var(--line); }}

    .diagnostic-summary {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }}
    .diagnostic-count {{
      min-height: 62px;
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface-2);
    }}
    .diagnostic-count span {{ display: block; color: var(--text-3); font-size: 11.5px; line-height: 1.3; }}
    .diagnostic-count strong {{ display: block; margin-top: 5px; font-size: 20px; font-weight: 750; line-height: 1.1; }}
    .diagnostic-count.ok strong {{ color: var(--ok); }}
    .diagnostic-count.warn strong {{ color: var(--accent); }}
    .diagnostic-count.failed strong, .diagnostic-count.missing strong {{ color: var(--danger); }}
    .schedule-summary {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin: 14px 0; }}
    .schedule-summary .diagnostic-count strong {{ color: var(--text); }}
    .student-list {{ max-width: 280px; color: var(--muted); line-height: 1.55; }}

    /* Quieter inline ids/dates inside dense tables (cohesion with new chips). */
    td .badge {{
      min-height: 20px;
      padding: 0 7px;
      background: transparent;
      border-color: var(--line);
      color: var(--text-3);
      font-size: 11px;
      font-weight: 600;
    }}

    /* Busy state — button shows an inline spinner while its action runs. */
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    button.is-busy {{ pointer-events: none; opacity: .82; }}
    button.is-busy::before {{
      content: "";
      width: 14px;
      height: 14px;
      border: 2px solid currentColor;
      border-top-color: transparent;
      border-radius: 50%;
      animation: spin .6s linear infinite;
    }}

    /* Toasts — action feedback surfaced anywhere, not just the dashboard log. */
    .toast-wrap {{
      position: fixed;
      bottom: 22px;
      left: 50%;
      transform: translateX(-50%);
      z-index: 80;
      display: flex;
      flex-direction: column;
      gap: 8px;
      align-items: center;
      pointer-events: none;
    }}
    .toast {{
      display: flex;
      align-items: center;
      gap: 9px;
      max-width: min(560px, 92vw);
      padding: 11px 16px;
      border-radius: 11px;
      background: var(--text);
      color: var(--panel);
      font-size: 13px;
      font-weight: 600;
      line-height: 1.45;
      box-shadow: var(--shadow-lg);
      transition: opacity .2s ease;
      animation: toastrise .2s ease;
    }}
    .toast .tdot {{ width: 8px; height: 8px; border-radius: 99px; flex-shrink: 0; background: #93c5fd; }}
    .toast.ok .tdot {{ background: #4ade80; }}
    .toast.err .tdot {{ background: #f87171; }}
    [data-theme="dark"] .toast {{ background: #f4f6fb; color: #11151f; }}
    @keyframes toastrise {{ from {{ opacity: 0; transform: translateY(8px); }} }}

    dl {{ display: grid; grid-template-columns: 150px 1fr; gap: 10px 14px; margin: 0; font-size: 13px; line-height: 1.45; }}
    dt {{ color: var(--text-3); font-weight: 600; }}
    dd {{ margin: 0; overflow-wrap: anywhere; }}

    /* ── Responsive ─────────────────────────── */
    @media (max-width: 920px) {{
      .app-shell {{ display: block; }}
      .sidebar {{ position: sticky; z-index: 10; height: auto; }}
      .brand {{ height: auto; padding: 14px 16px; }}
      .sidebar .tabs {{
        flex-direction: row;
        flex-wrap: nowrap;
        gap: 6px;
        padding: 10px 12px;
        overflow-x: auto;
        scrollbar-width: none;
      }}
      .sidebar .tabs::-webkit-scrollbar {{ display: none; }}
      .sidebar .tab-button {{
        width: auto;
        flex: 0 0 auto;
      }}
      .nav-sep {{ display: none; }}
      .sidebar-footer {{ display: none; }}
      .topbar {{
        height: auto;
        flex-wrap: wrap;
        align-items: flex-start;
        padding: 12px 14px;
      }}
      .topbar-title {{
        width: 100%;
        gap: 10px;
      }}
      .topbar-title h2 {{
        white-space: nowrap;
      }}
      .topbar .inline-actions {{
        width: 100%;
        margin-left: 0;
        justify-content: flex-start;
        flex-wrap: nowrap;
        overflow-x: auto;
        padding-bottom: 2px;
        scrollbar-width: none;
      }}
      .topbar .inline-actions::-webkit-scrollbar {{ display: none; }}
      .topbar .status-pill,
      .topbar button {{
        flex: 0 0 auto;
      }}
      .topbar-user {{
        display: none;
      }}
      .status-strip {{ flex-wrap: nowrap; }}
      .grid, .actions, .actions.compact, .action-layout, .action-controls, .mini-grid,
      .diagnostic-summary, .schedule-summary, .core-grid {{ grid-template-columns: 1fr; }}
      .workspace main {{ padding: 16px; }}
      .status-strip {{ margin: -16px -16px 16px; }}
      dl {{ grid-template-columns: 1fr; gap: 3px; }}
    }}
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark">{brand_initial}</div>
        <div>
          <h1>{title}</h1>
          <span>ClassIn 운영 콘솔</span>
        </div>
      </div>
      <nav class="tabs" aria-label="운영 메뉴">
        <div class="nav-sep">운영</div>
        <button class="tab-button active" data-tab="dashboard"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg><span class="label">대시보드</span></button>
        <button class="tab-button" data-tab="actions"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z"/></svg><span class="label">액션</span></button>
        <div class="nav-sep">학생</div>
        <button class="tab-button" data-tab="missing"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.3 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.3a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/></svg><span class="label">미제출</span></button>
        <button class="tab-button" data-tab="schedule"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4.5" width="18" height="17" rx="2.5"/><path d="M3 9h18M8 2.5v4M16 2.5v4"/></svg><span class="label">스케줄</span></button>
        <button class="tab-button" data-tab="reports"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h6M8 17h8"/></svg><span class="label">리포트</span></button>
        <div class="nav-sep">도구</div>
        <button class="tab-button" data-tab="memo"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg><span class="label">메모·AI</span></button>
        <button class="tab-button" data-tab="settings"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/></svg><span class="label">설정</span></button>
      </nav>
      <div class="sidebar-footer">
        <span id="configBadge" class="badge">config</span>
        <div class="server-pill">
          <span class="dot live"></span>
          <span id="lastUpdated" class="last-updated">-</span>
        </div>
      </div>
    </aside>
    <div class="workspace">
      <header class="topbar">
        <div class="topbar-title">
          <h2 id="viewTitle">대시보드</h2>
          <span id="viewMeta">{today}</span>
        </div>
        <div class="inline-actions">
          <span class="status-pill dry_run" title="현재 notify 출력 모드"><span class="dot" style="background:currentColor"></span>{notify_mode} 모드</span>
          <button class="icon-btn" title="다크 모드" aria-label="다크 모드 전환" onclick="document.documentElement.dataset.theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/></svg></button>
          <button data-action="refreshStatus" class="secondary">상태 새로고침</button>
          <div class="topbar-user">
            <span class="avatar">{brand_initial}</span>
            <div>
              <div class="nm">{title}</div>
              <div class="rl">운영자</div>
            </div>
          </div>
        </div>
      </header>
      <main>
    <section class="status-strip">
      <div class="metric"><span>Webhook 원본</span><strong id="incomingCount">0</strong></div>
      <div class="metric warn"><span>숙제 미제출</span><strong id="missingCount">0</strong></div>
      <div class="metric danger"><span>연락처 없음</span><strong id="noPhoneCount">0</strong></div>
      <div class="metric info"><span>문구 생성</span><strong id="dryRunCount">0</strong></div>
      <div class="metric ok"><span>발송 완료</span><strong id="sentCount">0</strong></div>
      <div class="metric danger"><span>발송 실패</span><strong id="failedCount">0</strong></div>
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
              <strong>숙제 미제출 문자 발송</strong>
              <span>조회 후 선택한 학부모에게 발송</span>
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
              <div class="range-picker">
                <label>조회 범위 <span id="windowRangeLabel" class="field-hint">오늘 0시 이후</span><input id="windowHours" type="hidden" value="24"></label>
                <div class="segmented" aria-label="미제출 조회 범위">
                  <button data-window-hours="today" class="active">오늘</button>
                  <button data-window-hours="4">4시간</button>
                  <button data-window-hours="24">24시간</button>
                  <button data-window-hours="72">3일</button>
                  <button data-window-hours="168">7일</button>
                </div>
              </div>
              <div class="mini-grid">
                <label>특정 수업만 보기<input id="lessonId" type="text"></label>
                <label>현재 기준<input id="windowRangeReadonly" type="text" value="오늘 0시 이후" readonly></label>
              </div>
              <div class="inline-actions">
                <button data-action="refreshMissing" class="secondary">미제출 조회</button>
                <button data-action="selectAllMissing" class="secondary">전체 선택</button>
                <button data-action="clearMissingSelection" class="secondary">선택 해제</button>
                <button data-action="sendMissingHomeworkSms">선택 문자 발송</button>
              </div>
              <div id="missingSelectionSummary" class="selection-summary">조회 후 발송 대상을 선택하세요.</div>
              <div id="missingSelectionList" class="selectable-list">
                <div class="empty">미제출 조회를 누르면 선택 목록이 표시됩니다.</div>
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
                <label>반 선택<select id="reportClassName"><option value="">전체 반</option></select></label>
                <label>주 시작일<input id="weekDate" type="date" value="{week_start}"></label>
              </div>
              <div class="inline-actions">
                <button data-action="loadReportClasses" class="secondary">반 목록 새로고침</button>
                <button data-action="loadReportTargets" class="secondary">대상 조회</button>
                <button data-action="selectAllReports" class="secondary">전체 선택</button>
                <button data-action="clearReportSelection" class="secondary">선택 해제</button>
                <button data-action="generateClassReports">선택 리포트 생성</button>
                <button data-action="approveWeekly" class="secondary">승인</button>
              </div>
              <div id="reportSelectionSummary" class="selection-summary">대상 조회 후 리포트 생성 학생을 선택하세요.</div>
              <div id="reportTargetList" class="selectable-list">
                <div class="empty">반을 선택하고 대상 조회를 누르세요.</div>
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
          <div class="inline-actions">
            <button data-action="refreshSchedule" class="secondary">스케줄 새로고침</button>
            <button data-action="exportScheduleCsv" class="secondary">CSV 내보내기</button>
          </div>
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
                <button data-action="exportMissingCsv" class="secondary">CSV 내보내기</button>
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
          <button data-action="generateWeekly" class="secondary">전체 주간 드래프트</button>
          <button data-action="focusReportCore" class="secondary">반별 리포트 선택</button>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head">
          <h2>시험 결과 가져오기</h2>
          <span class="badge">CSV</span>
        </div>
        <div class="stack">
          <div class="actions compact">
            <label>시험명<input id="examName" type="text"></label>
            <label>시험일<input id="examDate" type="date" value="{today}"></label>
            <label>반<input id="examClassName" type="text"></label>
            <label class="checkbox-field">Dry-run<input id="examDryRun" type="checkbox" checked></label>
          </div>
          <input id="examFile" type="file" accept=".csv,.tsv,.txt">
          <label>시험 CSV<textarea id="examCsvText" class="large"></textarea></label>
          <div class="inline-actions">
            <button data-action="readExamFile" class="secondary">파일 읽기</button>
            <button data-action="downloadExamTemplate" class="secondary">시험 템플릿</button>
            <button data-action="importExamResults">시험 결과 가져오기</button>
          </div>
          <div id="examPreview" class="action-preview">시험 CSV를 붙여넣고 먼저 dry-run으로 학생 매칭을 확인하세요.</div>
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

  <div id="toastWrap" class="toast-wrap" aria-live="polite"></div>

  <script id="initial-status" type="application/json">{status_json}</script>
  <script>
    const log = document.querySelector("#log");
    const statusNode = document.querySelector("#initial-status");
    let status = JSON.parse(statusNode.textContent);
    let diagnostics = null;
    let diagnosticsCacheAt = 0;
    const DIAGNOSTICS_TTL_MS = 12000;
    let currentSchedule = [];
    let currentMissing = [];
    let selectedMissingKeys = new Set();
    let currentReportTargets = [];
    let selectedReportIds = new Set();
    let reportClassesLoaded = false;

    function writeLog(message, data) {{
      const now = new Date().toLocaleTimeString();
      const detail = data ? "\\n" + JSON.stringify(data, null, 2) : "";
      log.textContent = `[${{now}}] ${{message}}${{detail}}\\n\\n` + log.textContent;
    }}

    const toastWrap = document.querySelector("#toastWrap");
    function toast(message, tone) {{
      if (!message) return;
      const el = document.createElement("div");
      el.className = "toast " + (tone || "ok");
      el.innerHTML = `<span class="tdot"></span><span>${{escapeHtml(message)}}</span>`;
      toastWrap.appendChild(el);
      setTimeout(() => {{
        el.style.opacity = "0";
        setTimeout(() => el.remove(), 220);
      }}, 3000);
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
      if (data.message) toast(data.message, "ok");
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

    async function loadDiagnostics(live, force) {{
      const summaryTarget = document.querySelector("#diagnosticSummary");
      const tableTarget = document.querySelector("#diagnosticTable");
      if (!status.ok) {{
        summaryTarget.innerHTML = "";
        tableTarget.innerHTML = `<div class="empty">config.yaml을 읽은 뒤 점검할 수 있습니다.</div>`;
        return;
      }}
      // 비-live 점검은 짧은 TTL 동안 캐시한다. 거의 모든 액션이 실행 전 게이트로
      // 설정 점검을 호출하므로, 연속 동작에서 /api/diagnostics 중복 호출을 줄인다.
      if (!live && !force && diagnostics && (Date.now() - diagnosticsCacheAt) < DIAGNOSTICS_TTL_MS) {{
        renderDiagnostics(diagnostics);
        return diagnostics;
      }}
      const response = await fetch(`/api/diagnostics?live=${{live ? "true" : "false"}}`);
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || "diagnostics failed");
      }}
      diagnostics = data;
      if (!live) diagnosticsCacheAt = Date.now();
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
      selectedMissingKeys = new Set();
      renderMissing({{ summary: {{}}, items: [] }});
      document.querySelector("#missingTable").innerHTML =
        `<div class="empty">Notion 설정을 먼저 채워야 조회할 수 있습니다.</div>`;
      document.querySelector("#missingSelectionList").innerHTML =
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
      selectedMissingKeys = new Set(
        items
          .filter((item) => item.has_parent_phone)
          .map((item) => item.selection_key)
          .filter(Boolean)
      );
      const withPhone = items.filter((item) => item.has_parent_phone).length;
      document.querySelector("#actionPreview").innerHTML =
        `<strong>숙제 미제출 문자 발송</strong>조회: ${{items.length}}명\\n선택 가능: ${{withPhone}}명\\n현재 발송 모드: ${{escapeHtml(notifyMode())}}`;
      renderMissingSelectionList();
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
                <th>선택</th>
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
                  <td>
                    <input
                      type="checkbox"
                      class="select-missing"
                      data-key="${{escapeHtml(item.selection_key || "")}}"
                      ${{item.has_parent_phone ? "" : "disabled"}}
                      ${{selectedMissingKeys.has(item.selection_key) ? "checked" : ""}}
                      aria-label="${{escapeHtml(item.student_name)}} 문자 대상 선택"
                    >
                  </td>
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

    function selectedMissingRows() {{
      return currentMissing.filter((item) => selectedMissingKeys.has(item.selection_key));
    }}

    function updateMissingSelectionSummary() {{
      const selected = selectedMissingRows();
      const withPhone = currentMissing.filter((item) => item.has_parent_phone).length;
      document.querySelector("#missingSelectionSummary").innerHTML =
        `<strong>${{selected.length}}명 선택</strong><span>조회 ${{currentMissing.length}}명 · 발송 가능 ${{withPhone}}명</span>`;
      document.querySelectorAll(".select-missing").forEach((checkbox) => {{
        checkbox.checked = selectedMissingKeys.has(checkbox.dataset.key);
      }});
    }}

    function renderMissingSelectionList() {{
      const target = document.querySelector("#missingSelectionList");
      if (!currentMissing.length) {{
        target.innerHTML = `<div class="empty">미제출 조회를 누르면 선택 목록이 표시됩니다.</div>`;
        updateMissingSelectionSummary();
        return;
      }}
      target.innerHTML = currentMissing.map((item) => `
        <label class="selectable-row ${{item.has_parent_phone ? "" : "is-disabled"}}">
          <input
            type="checkbox"
            class="select-missing"
            data-key="${{escapeHtml(item.selection_key || "")}}"
            ${{item.has_parent_phone ? "" : "disabled"}}
            ${{selectedMissingKeys.has(item.selection_key) ? "checked" : ""}}
          >
          <span>
            <strong>${{escapeHtml(item.student_name)}} · ${{escapeHtml(item.class_name || "-")}}</strong>
            <span>${{escapeHtml(formatDate(item.date))}} · 수업 ${{escapeHtml(item.lesson_classin_id || "-")}}</span>
            <span>${{item.has_parent_phone ? escapeHtml(item.parent_phone) : "연락처 없음"}}</span>
          </span>
        </label>
      `).join("");
      updateMissingSelectionSummary();
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

    function todayWindowHours() {{
      const now = new Date();
      const start = new Date(now);
      start.setHours(0, 0, 0, 0);
      return Math.max(1, Math.ceil((now.getTime() - start.getTime()) / 3600000));
    }}

    function missingWindowText(preset, hours) {{
      if (preset === "today") return "오늘 0시 이후";
      if (hours === 4) return "최근 4시간";
      if (hours === 24) return "최근 24시간";
      if (hours === 72) return "최근 3일";
      if (hours === 168) return "최근 7일";
      return `최근 ${{hours}}시간`;
    }}

    function setMissingWindowPreset(preset) {{
      const hours = preset === "today" ? todayWindowHours() : Number(preset || 24);
      const label = missingWindowText(preset, hours);
      document.querySelector("#windowHours").value = String(hours);
      document.querySelector("#windowRangeLabel").textContent = label;
      document.querySelector("#windowRangeReadonly").value = label;
      document.querySelectorAll("[data-window-hours]").forEach((button) => {{
        button.classList.toggle("active", button.dataset.windowHours === String(preset));
      }});
    }}

    function activateTab(tab) {{
      document.querySelectorAll(".tab-button").forEach((button) => {{
        button.classList.toggle("active", button.dataset.tab === tab);
      }});
      const activeButton = document.querySelector(`.tab-button[data-tab="${{tab}}"]`);
      activeButton?.scrollIntoView({{ block: "nearest", inline: "center" }});
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
      document.querySelector("[data-window-hours].active").focus();
      const withPhone = currentMissing.filter((item) => item.has_parent_phone).length;
      const selected = selectedMissingRows();
      setActionMode(
        "미제출 문자",
        `<strong>숙제 미제출 문자 발송</strong>조회: ${{currentMissing.length}}명\\n선택: ${{selected.length}}명\\n발송 가능: ${{withPhone}}명\\n현재 발송 모드: ${{escapeHtml(notifyMode())}}`
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
        `<strong>반별 리포트 생성</strong>반을 선택하고 주 시작일을 확인한 뒤 대상 조회를 누르세요.`;
      loadReportClasses().catch((error) => writeLog(error.message));
    }}

    function selectedReportTargets() {{
      return currentReportTargets.filter((item) => selectedReportIds.has(item.student_classin_id));
    }}

    function updateReportSelectionSummary() {{
      const selected = selectedReportTargets();
      const classes = new Set(currentReportTargets.map((item) => item.class_name).filter(Boolean));
      document.querySelector("#reportSelectionSummary").innerHTML =
        `<strong>${{selected.length}}명 선택</strong><span>조회 ${{currentReportTargets.length}}명 · 반 ${{classes.size || 0}}개</span>`;
      document.querySelectorAll(".select-report").forEach((checkbox) => {{
        checkbox.checked = selectedReportIds.has(checkbox.dataset.studentId);
      }});
    }}

    function renderReportTargets(data) {{
      const items = data.items || [];
      currentReportTargets = items;
      selectedReportIds = new Set(items.map((item) => item.student_classin_id).filter(Boolean));
      const target = document.querySelector("#reportTargetList");
      if (!items.length) {{
        target.innerHTML = `<div class="empty">조회된 리포트 대상 학생이 없습니다.</div>`;
        updateReportSelectionSummary();
        return;
      }}
      target.innerHTML = items.map((item) => `
        <label class="selectable-row">
          <input
            type="checkbox"
            class="select-report"
            data-student-id="${{escapeHtml(item.student_classin_id || "")}}"
            checked
          >
          <span>
            <strong>${{escapeHtml(item.student_name)}} · ${{escapeHtml(item.class_name || "-")}}</strong>
            <span>ClassIn ${{escapeHtml(item.student_classin_id || "-")}}</span>
            <span>${{item.has_parent_phone ? "학부모 연락처 있음" : "학부모 연락처 없음"}}</span>
          </span>
        </label>
      `).join("");
      updateReportSelectionSummary();
    }}

    function renderReportClassOptions(classes) {{
      const select = document.querySelector("#reportClassName");
      const current = select.value;
      const options = [`<option value="">전체 반</option>`].concat(
        (classes || []).map((name) => `<option value="${{escapeHtml(name)}}">${{escapeHtml(name)}}</option>`)
      );
      select.innerHTML = options.join("");
      if (current && (classes || []).includes(current)) {{
        select.value = current;
      }}
    }}

    async function loadReportClasses(force) {{
      if (reportClassesLoaded && !force) return;
      const data = await loadDiagnostics(false);
      if (!canLoadNotion(data)) {{
        document.querySelector("#reportTargetList").innerHTML =
          `<div class="empty">Notion 설정을 먼저 채워야 반 목록을 불러올 수 있습니다.</div>`;
        writeLog("Notion 설정을 먼저 채워야 반 목록을 불러올 수 있습니다.");
        return;
      }}
      const response = await fetch("/api/report-targets");
      const targets = await response.json();
      if (!response.ok || targets.ok === false) {{
        throw new Error(targets.detail || "report class load failed");
      }}
      renderReportClassOptions(targets.classes || []);
      reportClassesLoaded = true;
      document.querySelector("#reportPreview").innerHTML =
        `<strong>반 목록</strong>${{escapeHtml((targets.classes || []).length)}}개 반을 불러왔습니다.`;
      writeLog("반 목록을 불러왔습니다.", {{ classes: targets.classes || [] }});
    }}

    async function loadReportTargets() {{
      await loadReportClasses(false);
      const data = await loadDiagnostics(false);
      if (!canLoadNotion(data)) {{
        document.querySelector("#reportTargetList").innerHTML =
          `<div class="empty">Notion 설정을 먼저 채워야 리포트 대상을 조회할 수 있습니다.</div>`;
        writeLog("Notion 설정을 먼저 채워야 리포트 대상을 조회할 수 있습니다.");
        return;
      }}
      const params = new URLSearchParams();
      const className = document.querySelector("#reportClassName").value.trim();
      if (className) params.set("class_name", className);
      const response = await fetch(`/api/report-targets?${{params.toString()}}`);
      const targets = await response.json();
      if (!response.ok || targets.ok === false) {{
        throw new Error(targets.detail || "report target load failed");
      }}
      renderReportTargets(targets);
      document.querySelector("#reportPreview").innerHTML =
        `<strong>리포트 대상 조회</strong>조회: ${{escapeHtml((targets.summary || {{}}).total || 0)}}명\\n전체 선택 상태로 시작합니다.`;
      writeLog("리포트 대상을 조회했습니다.", targets.summary);
    }}

    function selectAllReports() {{
      selectedReportIds = new Set(
        currentReportTargets.map((item) => item.student_classin_id).filter(Boolean)
      );
      updateReportSelectionSummary();
      writeLog(`리포트 대상 ${{selectedReportIds.size}}명을 선택했습니다.`);
    }}

    function clearReportSelection() {{
      selectedReportIds = new Set();
      updateReportSelectionSummary();
      writeLog("리포트 대상 선택을 해제했습니다.");
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
      const selected = selectedMissingRows();
      if (!selected.length) {{
        throw new Error("미제출 조회 후 문자 발송 대상을 선택하세요.");
      }}
      const mode = notifyMode();
      if (mode === "live") {{
        const ok = window.confirm(
          `실제 문자 발송 모드입니다. 선택한 미제출자 ${{selected.length}}명에게 발송합니다.`
        );
        if (!ok) return;
      }}
      const result = await callApi("/api/sweep-missing-homework", {{
        window_hours: document.querySelector("#windowHours").value,
        lesson_id: document.querySelector("#lessonId").value,
        selection_keys: selected.map((item) => item.selection_key),
      }});
      await loadSituation();
      setActionMode(
        "문자 처리",
        `<strong>숙제 미제출 문자 발송</strong>선택: ${{selected.length}}명\\n처리: ${{escapeHtml(result.count || 0)}}건\\n모드: ${{escapeHtml(mode)}}`
      );
      writeLog(result.message, result);
    }}

    function selectAllMissing() {{
      selectedMissingKeys = new Set(
        currentMissing
          .filter((item) => item.has_parent_phone)
          .map((item) => item.selection_key)
          .filter(Boolean)
      );
      updateMissingSelectionSummary();
      writeLog(`문자 발송 대상 ${{selectedMissingKeys.size}}명을 선택했습니다.`);
    }}

    function clearMissingSelection() {{
      selectedMissingKeys = new Set();
      updateMissingSelectionSummary();
      writeLog("문자 발송 대상 선택을 해제했습니다.");
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

    async function readExamFile() {{
      const input = document.querySelector("#examFile");
      const file = input.files && input.files[0];
      if (!file) throw new Error("읽을 시험 파일을 선택하세요.");
      const text = await file.text();
      document.querySelector("#examCsvText").value = text;
      const parsed = inspectDelimited(text);
      document.querySelector("#examPreview").innerHTML =
        `<strong>${{escapeHtml(file.name)}}</strong>` +
        `형식: ${{escapeHtml(parsed.delimiter)}}\\n` +
        `행: ${{parsed.rows}}\\n` +
        `헤더: ${{escapeHtml(parsed.headers.join(", ") || "-")}}`;
      writeLog("시험 파일을 읽었습니다.", {{ name: file.name, size: file.size }});
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

    function downloadExamTemplate() {{
      const headers = [
        "student_name",
        "student_classin_id",
        "class_name",
        "subject",
        "score",
        "max_score",
        "attended",
      ];
      const rows = [[
        "홍길동",
        "10001",
        "고2-A",
        "수학",
        "92",
        "100",
        "응시",
      ]];
      downloadCsv("exam-results-template.csv", headers, rows);
      writeLog("시험 템플릿을 만들었습니다.");
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
      const data = await callApi("/api/parse-schedule-dry-run", {{
        schedule_text: text,
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
      if (!currentReportTargets.length) {{
        throw new Error("먼저 리포트 대상을 조회하세요.");
      }}
      const selected = selectedReportTargets();
      if (!selected.length) {{
        throw new Error("리포트 생성 대상을 선택하세요.");
      }}
      const data = await callApi("/api/generate-class-reports", {{
        class_name: document.querySelector("#reportClassName").value,
        week: document.querySelector("#weekDate").value,
        student_classin_ids: selected.map((item) => item.student_classin_id),
      }});
      document.querySelector("#reportPreview").innerHTML =
        `<strong>선택 리포트 생성</strong>선택: ${{selected.length}}명\\n${{escapeHtml(data.message)}}`;
      writeLog(data.message, data);
      await refreshStatus();
    }}

    function renderExamImportResult(data) {{
      const summary = data.summary || {{}};
      const errors = data.errors || [];
      document.querySelector("#examPreview").innerHTML =
        `<strong>${{escapeHtml(data.dry_run ? "시험 결과 dry-run" : "시험 결과 가져오기")}}</strong>` +
        Object.entries(summary)
          .map(([key, value]) => `${{escapeHtml(key)}}: ${{escapeHtml(value)}}`)
          .join("\\n") +
        (errors.length
          ? `\\n\\n오류\\n${{escapeHtml(errors.slice(0, 8).join("\\n"))}}`
          : "");
    }}

    async function importExamResults() {{
      const csvText = document.querySelector("#examCsvText").value.trim();
      if (!csvText) throw new Error("가져올 시험 CSV가 필요합니다.");
      const dryRun = document.querySelector("#examDryRun").checked;
      if (!dryRun) {{
        const ok = window.confirm("Notion 시험 DB에 시험 결과를 실제로 저장합니다.");
        if (!ok) return;
      }}
      const data = await callApi("/api/import-exam-results", {{
        csv_text: csvText,
        exam_name: document.querySelector("#examName").value,
        exam_date: document.querySelector("#examDate").value,
        class_name: document.querySelector("#examClassName").value,
        dry_run: dryRun,
      }});
      renderExamImportResult(data);
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
      async selectAllMissing() {{
        selectAllMissing();
      }},
      async clearMissingSelection() {{
        clearMissingSelection();
      }},
      async createScheduleFromText() {{
        await createScheduleFromText();
      }},
      async loadReportClasses() {{
        await loadReportClasses(true);
      }},
      async loadReportTargets() {{
        await loadReportTargets();
      }},
      async selectAllReports() {{
        selectAllReports();
      }},
      async clearReportSelection() {{
        clearReportSelection();
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
      async readExamFile() {{
        await readExamFile();
      }},
      async downloadScheduleTemplate() {{
        downloadScheduleTemplate();
      }},
      async downloadExamTemplate() {{
        downloadExamTemplate();
      }},
      async runScheduleImportDryRun() {{
        await runScheduleImportDryRun();
      }},
      async importExamResults() {{
        await importExamResults();
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
        const data = await loadDiagnostics(false, true);
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
        const data = await loadDiagnostics(false, true);
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
      button.classList.add("is-busy");
      try {{
        await action();
      }} catch (error) {{
        writeLog(error.message);
        toast(error.message, "err");
      }} finally {{
        button.disabled = false;
        button.classList.remove("is-busy");
      }}
    }});

    document.addEventListener("click", (event) => {{
      const tabButton = event.target.closest("button[data-tab]");
      if (!tabButton) return;
      activateTab(tabButton.dataset.tab);
    }});

    document.addEventListener("click", (event) => {{
      const rangeButton = event.target.closest("button[data-window-hours]");
      if (!rangeButton) return;
      setMissingWindowPreset(rangeButton.dataset.windowHours);
      if (currentMissing.length) {{
        document.querySelector("#actionPreview").innerHTML =
          `<strong>조회 범위 변경</strong>${{escapeHtml(document.querySelector("#windowRangeReadonly").value)}} 기준으로 다시 조회하세요.`;
      }}
    }});

    document.addEventListener("change", (event) => {{
      if (event.target.id === "reportClassName") {{
        currentReportTargets = [];
        selectedReportIds = new Set();
        document.querySelector("#reportTargetList").innerHTML =
          `<div class="empty">대상 조회를 누르면 선택 목록이 표시됩니다.</div>`;
        updateReportSelectionSummary();
        document.querySelector("#reportPreview").innerHTML =
          `<strong>반 선택</strong>${{escapeHtml(event.target.value || "전체 반")}} 기준으로 대상 조회를 누르세요.`;
        return;
      }}
      const missing = event.target.closest(".select-missing");
      if (missing) {{
        if (missing.checked) {{
          selectedMissingKeys.add(missing.dataset.key);
        }} else {{
          selectedMissingKeys.delete(missing.dataset.key);
        }}
        updateMissingSelectionSummary();
        return;
      }}
      const report = event.target.closest(".select-report");
      if (report) {{
        if (report.checked) {{
          selectedReportIds.add(report.dataset.studentId);
        }} else {{
          selectedReportIds.delete(report.dataset.studentId);
        }}
        updateReportSelectionSummary();
      }}
    }});

    setMissingWindowPreset("today");
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
