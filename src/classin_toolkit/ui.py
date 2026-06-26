"""로컬 운영 UI (FastAPI).

원장/컨설턴트가 반복 실행하는 CLI 작업을 브라우저 버튼으로 감싼 얇은 계층이다.
비즈니스 로직은 pipelines/와 intelligence/의 기존 함수를 그대로 호출한다.
"""
from __future__ import annotations

import html
import json
import re
import tempfile
from datetime import date as date_cls
from datetime import datetime, timezone
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .api_diagnostics import DiagnosticReport, diagnose_apis
from .classin.sso import get_login_linked
from .config import AppConfig, DEFAULT_CONFIG_PATH, load_config
from .intelligence.action_queue import build_teacher_action_queue
from .intelligence.report_composition import compose_individual_report
from .notify.dispatcher import load_notification_history, notification_history_path
from .notify.message import OutgoingMessage
from .pipelines.core_engine import run_core_engine
from .pipelines.daily import render_daily
from .pipelines.data_merge import build_report_contexts
from .pipelines.demo_seed import (
    DEMO_STUDENTS,
    build_demo_lesson_records,
    build_demo_missing_homework_rows,
    build_demo_notification_history,
)
from .pipelines.exams import create_answer_sheet_activity, import_exam_results
from .pipelines.missing_homework import (
    missing_homework_selection_key,
    preview_missing_homework_messages,
    query_missing_homework,
    sweep_missing_homework,
)
from .pipelines.weekly import approve_all, generate_drafts, list_drafts
from .readiness import ReadinessReport, check_readiness
from .storage.notion_repo import NotionRepo
from .storage.notion_setup import dry_run_schema

_DEMO_ACADEMY = "ClassIn Demo Academy"
_OPS_HANDOFF_INDEX = "ops_handoffs.json"
_MAX_OPS_HANDOFFS = 50


def create_app(
    config: AppConfig | None = None,
    *,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    demo: bool = False,
) -> FastAPI:
    app = FastAPI(title="ClassIn Toolkit UI", version="0.1.0")
    state = _ConfigState(config=config, config_path=Path(config_path), demo=demo)

    @app.get("/health")
    async def health() -> dict:
        if state.demo:
            return {"ok": True, "academy": _DEMO_ACADEMY, "error": None, "mode": "demo"}
        cfg, error = state.load()
        return {
            "ok": error is None,
            "academy": cfg.academy.name if cfg else None,
            "error": error,
        }

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        if state.demo:
            return HTMLResponse(_render_shell(_demo_status_payload(state.config_path)))
        cfg, error = state.load()
        return HTMLResponse(_render_shell(_status_payload(cfg, error, state.config_path)))

    @app.get("/api/status")
    async def api_status() -> dict:
        if state.demo:
            return _demo_status_payload(state.config_path)
        cfg, error = state.load()
        return _status_payload(cfg, error, state.config_path)

    @app.get("/api/readiness")
    async def api_readiness(mode: str = "local-demo") -> dict:
        if state.demo:
            return _demo_readiness_payload(mode)
        cfg = _require_config(state)
        try:
            report = check_readiness(cfg, mode=mode, project_root=Path.cwd())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _readiness_payload(report)

    @app.get("/api/notion-schema")
    async def api_notion_schema(prefix: str = "ClassIn Toolkit") -> dict:
        target_prefix = _parse_schema_prefix(prefix)
        return _notion_schema_payload(target_prefix)

    @app.get("/api/pilot-brief")
    async def api_pilot_brief() -> dict:
        if state.demo:
            return _pilot_brief_payload(None, demo=True)
        cfg, error = state.load()
        return _pilot_brief_payload(cfg, demo=False, config_error=error)

    @app.get("/api/webhook-inbox")
    async def api_webhook_inbox(limit: int = 30) -> dict:
        _validate_webhook_limit(limit)
        if state.demo:
            return _demo_webhook_inbox_payload(limit=limit)
        cfg, error = state.load()
        if not cfg:
            raise HTTPException(status_code=400, detail=error or "config.yaml을 읽을 수 없습니다.")
        return _webhook_inbox_payload(Path(cfg.webhook.dump_dir), limit=limit)

    @app.get("/api/academy-contexts")
    async def api_academy_contexts(class_name: str | None = None) -> dict:
        target_class = (class_name or "").strip() or None
        if state.demo:
            return _demo_academy_contexts_payload(class_name=target_class)
        cfg = _require_config(state)
        _require_service_config(cfg, "Notion")
        try:
            students = NotionRepo.from_config(cfg).list_active_students()
        except Exception as exc:
            raise _service_error("학원 데이터 융합 조회", exc) from exc
        if target_class:
            students = [student for student in students if student.class_name == target_class]
        return _academy_contexts_payload(cfg, students, class_name=target_class)

    @app.get("/api/ops-hub")
    async def api_ops_hub(
        window_hours: int = 24,
        lesson_id: str | None = None,
    ) -> dict:
        return _load_ops_hub_payload(state, window_hours=window_hours, lesson_id=lesson_id)

    @app.get("/api/ops-report")
    async def api_ops_report(
        window_hours: int = 24,
        lesson_id: str | None = None,
    ) -> dict:
        return _load_ops_report_payload(state, window_hours=window_hours, lesson_id=lesson_id)

    @app.post("/api/ops-handoff")
    async def api_ops_handoff(request: Request) -> JSONResponse:
        payload = await _json_payload(request)
        window_hours = _parse_window_hours(payload.get("window_hours"))
        lesson_id = (payload.get("lesson_id") or "").strip() or None
        report = _load_ops_report_payload(state, window_hours=window_hours, lesson_id=lesson_id)
        if state.demo:
            return _demo_ok(
                "Demo mode: 운영 리포트를 저장한 것으로 표시됩니다.",
                item=_demo_ops_handoff_item(report),
                markdown=report["markdown"],
            )
        cfg = _require_config(state)
        item = _save_ops_handoff(cfg, report)
        return _ok("운영 리포트를 저장했습니다.", item=item, markdown=report["markdown"])

    @app.get("/api/ops-handoffs")
    async def api_ops_handoffs(limit: int = 8) -> dict:
        _validate_handoff_limit(limit)
        if state.demo:
            report = _load_ops_report_payload(state, window_hours=24, lesson_id=None)
            return {
                "ok": True,
                "demo": True,
                "items": [_demo_ops_handoff_item(report)],
            }
        cfg = _require_config(state)
        return {
            "ok": True,
            "demo": False,
            "items": _load_ops_handoffs(cfg, limit=limit),
        }

    @app.get("/api/ops-playbook")
    async def api_ops_playbook(
        window_hours: int = 24,
        lesson_id: str | None = None,
    ) -> dict:
        hub = _load_ops_hub_payload(state, window_hours=window_hours, lesson_id=lesson_id)
        return _ops_playbook_payload(hub=hub)

    @app.get("/api/diagnostics")
    async def api_diagnostics(live: bool = False) -> dict:
        if state.demo:
            return {
                "ok": True,
                "ready": True,
                "live": False,
                "summary": {"ok": 4, "warn": 0, "missing": 0, "failed": 0, "skipped": 0},
                "items": [
                    {
                        "service": "Demo",
                        "check": "외부 API 쓰기",
                        "status": "ok",
                        "detail": "비활성화",
                        "next_step": "",
                    }
                ],
            }
        cfg = _require_config(state)
        report = diagnose_apis(cfg, live=live)
        return _diagnostics_payload(report)

    @app.get("/api/schedule")
    async def api_schedule(
        start: str | None = None,
        days: int = 7,
    ) -> dict:
        if state.demo:
            try:
                start_date = date_cls.fromisoformat(start) if start else date_cls.today()
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="start는 YYYY-MM-DD 형식이어야 합니다.") from exc
            if days < 1 or days > 62:
                raise HTTPException(status_code=400, detail="days는 1~62 사이여야 합니다.")
            rows = _demo_lesson_rows(start_date=start_date, days=days)
            return _schedule_payload(rows, start_date=start_date, days=days)
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
        if state.demo:
            return _demo_missing_homework_payload()
        cfg = _require_config(state)
        _require_service_config(cfg, "Notion")
        _validate_window_hours(window_hours)
        try:
            rows = query_missing_homework(
                cfg,
                window_hours=window_hours,
                lesson_id=(lesson_id or "").strip() or None,
            )
            history = load_notification_history(cfg, limit=300)
            report_contexts = build_report_contexts(cfg, rows)
        except Exception as exc:
            raise _service_error("미제출 조회", exc) from exc
        return _missing_homework_payload(
            rows,
            history,
            report_contexts=report_contexts.contexts,
            data_context={
                "summary": report_contexts.summary,
                "needs_review_items": report_contexts.needs_review_items[:20],
            },
        )

    @app.get("/api/notifications")
    async def api_notifications(limit: int = 80) -> dict:
        if state.demo:
            rows = _demo_notification_history()[:limit]
            return {"ok": True, "items": rows, "summary": _notification_summary(rows)}
        cfg = _require_config(state)
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=400, detail="limit은 1~500 사이여야 합니다.")
        try:
            rows = load_notification_history(cfg, limit=limit)
        except Exception as exc:
            raise _service_error("알림 기록 조회", exc) from exc
        return {"ok": True, "items": rows, "summary": _notification_summary(rows)}

    @app.post("/api/render-daily")
    async def api_render_daily(request: Request) -> JSONResponse:
        if state.demo:
            return _demo_ok("Demo mode: 일일 현황 생성은 외부 쓰기 없이 처리되었습니다.")
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
        if state.demo:
            return _demo_ok("Demo mode: 주간 드래프트 5건이 준비된 것으로 표시됩니다.", count=5)
        cfg = _require_config(state)
        _require_service_config(cfg, "Notion")
        try:
            count = generate_drafts(cfg)
        except Exception as exc:
            raise _service_error("주간 드래프트 생성", exc) from exc
        return _ok(f"주간 드래프트 {count}건을 생성했습니다.", count=count)

    @app.get("/api/weekly-drafts")
    async def api_weekly_drafts(week: str | None = None) -> dict:
        period_start = _parse_week_start(week)
        if state.demo:
            return _demo_weekly_drafts_payload(period_start)
        cfg = _require_config(state)
        try:
            result = list_drafts(cfg, period_start=period_start)
        except Exception as exc:
            raise _service_error("주간 드래프트 조회", exc) from exc
        return {"ok": True, **result.__dict__}

    @app.get("/api/report-targets")
    async def api_report_targets(class_name: str | None = None) -> dict:
        if state.demo:
            target_class = (class_name or "").strip() or None
            items = _demo_report_target_items()
            if target_class:
                items = [item for item in items if item["class_name"] == target_class]
            classes = sorted({item["class_name"] for item in items if item["class_name"]})
            return {
                "ok": True,
                "class_name": target_class,
                "summary": {
                    "total": len(items),
                    "classes": len(classes),
                    "with_parent_phone": sum(1 for item in items if item["has_parent_phone"]),
                },
                "classes": classes,
                "items": items,
            }
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

    @app.get("/api/report-compositions")
    async def api_report_compositions(
        week: str | None = None,
        class_name: str | None = None,
    ) -> dict:
        period_start = _parse_week_start(week)
        target_class = (class_name or "").strip() or None
        if state.demo:
            return _demo_report_compositions_payload(period_start, class_name=target_class)
        cfg = _require_config(state)
        _require_service_config(cfg, "Notion")
        try:
            repo = NotionRepo.from_config(cfg)
            students = repo.list_active_students()
            if target_class:
                students = [student for student in students if student.class_name == target_class]
            payload = _report_compositions_payload(
                cfg,
                repo,
                students,
                period_start=period_start,
                class_name=target_class,
            )
        except Exception as exc:
            raise _service_error("리포트 구성 조회", exc) from exc
        return payload

    @app.get("/api/student-report-pack")
    async def api_student_report_pack(
        student_classin_id: str,
        week: str | None = None,
        class_name: str | None = None,
    ) -> dict:
        target_student_id = (student_classin_id or "").strip()
        if not target_student_id:
            raise HTTPException(status_code=400, detail="student_classin_id 값이 필요합니다.")
        period_start = _parse_week_start(week)
        target_class = (class_name or "").strip() or None
        if state.demo:
            compositions = _demo_report_compositions_payload(period_start, class_name=target_class)
            return _student_report_pack_payload(
                compositions,
                student_classin_id=target_student_id,
            )
        cfg = _require_config(state)
        _require_service_config(cfg, "Notion")
        try:
            repo = NotionRepo.from_config(cfg)
            students = repo.list_active_students()
            if target_class:
                students = [student for student in students if student.class_name == target_class]
            compositions = _report_compositions_payload(
                cfg,
                repo,
                students,
                period_start=period_start,
                class_name=target_class,
            )
        except Exception as exc:
            raise _service_error("개별 리포트 초안 생성", exc) from exc
        return _student_report_pack_payload(
            compositions,
            student_classin_id=target_student_id,
        )

    @app.post("/api/approve-weekly")
    async def api_approve_weekly(request: Request) -> JSONResponse:
        if state.demo:
            return _demo_ok("Demo mode: 주간 리포트 승인 흐름을 외부 쓰기 없이 확인했습니다.")
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
            result = approve_all(
                cfg,
                period_start=period_start,
                force_blocked_quality=bool(payload.get("force_blocked_quality", False)),
            )
        except Exception as exc:
            raise _service_error("주간 리포트 승인", exc) from exc
        message = f"주간 리포트 {result.approved}건을 승인 처리했습니다."
        if result.skipped_blocked_quality:
            message += f" 품질 점검 blocked {result.skipped_blocked_quality}건은 건너뛰었습니다."
        return _ok(
            message,
            count=result.approved,
            skipped={
                "blocked_quality": result.skipped_blocked_quality,
                "missing_student": result.skipped_missing_student,
                "already_approved": result.skipped_already_approved,
                "total": result.skipped,
            },
        )

    @app.post("/api/sweep-missing-homework")
    async def api_sweep_missing_homework(request: Request) -> JSONResponse:
        if state.demo:
            return _demo_ok("Demo mode: 미제출 sweep이 dry-run으로 처리되었습니다.", count=3)
        cfg = _require_config(state)
        _require_service_config(cfg, "Notion")
        payload = await _json_payload(request)
        window_hours = _parse_window_hours(payload.get("window_hours", 24))
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

    @app.post("/api/preview-missing-homework")
    async def api_preview_missing_homework(request: Request) -> JSONResponse:
        payload = await _json_payload(request)
        window_hours = _parse_window_hours(payload.get("window_hours", 24))
        lesson_id = (payload.get("lesson_id") or "").strip() or None
        selection_keys = _optional_string_list(payload, "selection_keys", "문구 미리보기 대상")
        if state.demo:
            return JSONResponse(_demo_missing_homework_preview_payload(selection_keys=selection_keys))
        cfg = _require_config(state)
        _require_service_config(cfg, "Notion")
        try:
            messages = preview_missing_homework_messages(
                cfg,
                window_hours=window_hours,
                lesson_id=lesson_id,
                selection_keys=selection_keys,
            )
        except Exception as exc:
            raise _service_error("미제출 알림 문구 미리보기", exc) from exc
        return JSONResponse(
            _missing_homework_preview_payload(
                messages,
                notify_mode=cfg.notify.mode,
                demo=False,
            )
        )

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
            raise _service_error("스케줄 사전 검토", exc) from exc
        return _ok(
            "스케줄 사전 검토를 완료했습니다.",
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
        if state.demo:
            payload = await _json_payload(request)
            student_classin_ids = _optional_string_list(
                payload,
                "student_classin_ids",
                "리포트 대상 학생",
            )
            count = len(student_classin_ids or _demo_report_target_items())
            return _demo_ok(
                f"Demo mode: 리포트 드래프트 {count}건이 준비된 것으로 표시됩니다.",
                count=count,
                class_name=(payload.get("class_name") or "").strip() or None,
                selected=len(student_classin_ids) if student_classin_ids is not None else None,
                week=(payload.get("week") or "").strip(),
                includes=["출결", "숙제", "시험 점수"],
            )
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

    @app.post("/api/create-answer-sheet")
    async def api_create_answer_sheet(request: Request) -> JSONResponse:
        cfg = _require_config(state)
        payload = await _json_payload(request)
        dry_run = bool(payload.get("dry_run", True))
        if not dry_run:
            _require_service_config(cfg, "ClassIn")

        course_id = (payload.get("course_id") or "").strip()
        unit_id = (payload.get("unit_id") or "").strip()
        name = (payload.get("name") or "").strip()
        if not course_id or not unit_id or not name:
            raise HTTPException(status_code=400, detail="course_id, unit_id, name 값이 필요합니다.")
        try:
            result = create_answer_sheet_activity(
                cfg,
                course_id=course_id,
                unit_id=unit_id,
                name=name,
                teacher_uid=(payload.get("teacher_uid") or "").strip() or None,
                start_at=_parse_optional_datetime(payload.get("start_at")),
                end_at=_parse_optional_datetime(payload.get("end_at")),
                release=bool(payload.get("release", False)),
                dry_run=dry_run,
            )
        except Exception as exc:
            raise _service_error("OMR 답안지 생성", exc) from exc
        return _ok(
            "OMR 답안지 생성 전 검토를 완료했습니다." if result.dry_run else "OMR 답안지를 생성했습니다.",
            activity_id=result.activity_id,
            name=result.name,
            released=result.released,
            dry_run=result.dry_run,
        )

    @app.post("/api/sso-link")
    async def api_sso_link(request: Request) -> JSONResponse:
        payload = await _json_payload(request)
        uid = _required_payload_text(payload, "uid", "사용자 식별번호")
        course_id = _required_payload_text(payload, "course_id", "강좌 ID")
        class_id = _required_payload_text(payload, "class_id", "수업 ID")
        telephone = _required_payload_text(payload, "telephone", "전화번호")
        device_type = _parse_sso_device_type(payload.get("device_type", 1))
        life_time = _parse_sso_life_time(payload.get("life_time", 86400))

        if state.demo:
            link = _demo_sso_link(
                uid=uid,
                course_id=course_id,
                class_id=class_id,
                device_type=device_type,
                life_time=life_time,
            )
            return _demo_ok(
                "데모 접속 링크를 생성했습니다.",
                link=link,
                masked_link=_mask_sso_link(link),
                device_type=device_type,
                device_label=_sso_device_label(device_type),
                life_time=life_time,
            )

        cfg = _require_config(state)
        _require_service_config(cfg, "ClassIn")
        try:
            link = get_login_linked(
                base_url=cfg.classin.base_url,
                sid=cfg.classin.school_id,
                secret_key=cfg.classin.secret_key,
                uid=uid,
                course_id=course_id,
                class_id=class_id,
                telephone=telephone,
                device_type=device_type,
                life_time=life_time,
            )
        except Exception as exc:
            raise _service_error("ClassIn 앱 접속 링크 생성", exc) from exc
        return _ok(
            "ClassIn 앱 접속 링크를 생성했습니다.",
            link=link,
            masked_link=_mask_sso_link(link),
            device_type=device_type,
            device_label=_sso_device_label(device_type),
            life_time=life_time,
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
            "시험 결과 매칭 검토를 완료했습니다." if result.dry_run else "시험 결과를 가져왔습니다.",
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
        if state.demo:
            return _demo_ok("Demo mode: 메모 저장은 외부 쓰기 없이 처리되었습니다.")
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
        if state.demo:
            payload = await _json_payload(request)
            question = (payload.get("question") or "").strip()
            answer = (
                "데모 기준으로 김지각, 이하락, 최결석 학생이 숙제 확인 대상입니다. "
                "연락처가 없는 학생은 먼저 학생 정보를 보완해야 합니다."
            )
            return _demo_ok(
                "Demo mode: AI 응답을 샘플로 생성했습니다.",
                answer=answer,
                question=question,
            )
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
        if state.demo:
            raise HTTPException(status_code=404)
        cfg = _require_config(state)
        if kind not in ("daily", "weekly", "ops"):
            raise HTTPException(status_code=404)
        if "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(status_code=400)
        if kind == "daily":
            base = Path(cfg.output.daily.path)
            media_type = "text/html; charset=utf-8"
        elif kind == "weekly":
            base = Path(cfg.output.weekly.path)
            media_type = "text/html; charset=utf-8"
        else:
            base = _ops_handoff_dir(cfg)
            media_type = "text/markdown; charset=utf-8"
        path = base / filename
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type=media_type)

    return app


class _ConfigState:
    def __init__(self, *, config: AppConfig | None, config_path: Path, demo: bool) -> None:
        self._config = config
        self.config_path = config_path
        self.demo = demo

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


def _load_ops_hub_payload(
    state: _ConfigState,
    *,
    window_hours: int,
    lesson_id: str | None,
) -> dict[str, Any]:
    if state.demo:
        return _demo_ops_hub_payload(state.config_path)
    _validate_window_hours(window_hours)
    cfg, error = state.load()
    if not cfg:
        status = _status_payload(cfg, error, state.config_path)
        return _ops_hub_payload(
            status=status,
            diagnostics=None,
            weekly_drafts=None,
            missing=None,
            report_compositions=None,
            missing_error=error or "config.yaml을 읽을 수 없습니다.",
        )

    status = _status_payload(cfg, None, state.config_path)
    diagnostics = _diagnostics_payload(diagnose_apis(cfg, live=False))
    week_start = datetime.combine(
        _week_start(date_cls.today()),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    weekly_drafts = list_drafts(cfg, period_start=week_start).__dict__
    missing: dict[str, Any] | None = None
    report_compositions: dict[str, Any] | None = None
    missing_error: str | None = None
    if _service_state(diagnostics, "Notion") != "blocked":
        try:
            rows = query_missing_homework(
                cfg,
                window_hours=window_hours,
                lesson_id=(lesson_id or "").strip() or None,
            )
            history = load_notification_history(cfg, limit=300)
            report_contexts = build_report_contexts(cfg, rows)
            missing = _missing_homework_payload(
                rows,
                history,
                report_contexts=report_contexts.contexts,
                data_context={
                    "summary": report_contexts.summary,
                    "needs_review_items": report_contexts.needs_review_items[:20],
                },
            )
        except Exception as exc:
            missing_error = _safe_exception(exc)
        try:
            repo = NotionRepo.from_config(cfg)
            report_compositions = _report_compositions_payload(
                cfg,
                repo,
                repo.list_active_students(),
                period_start=week_start,
                class_name=None,
            )
        except Exception as exc:
            report_compositions = {
                "ok": False,
                "error": _safe_exception(exc),
                "items": [],
                "summary": {},
            }
    return _ops_hub_payload(
        status=status,
        diagnostics=diagnostics,
        weekly_drafts=weekly_drafts,
        missing=missing,
        report_compositions=report_compositions,
        missing_error=missing_error,
    )


def _load_ops_report_payload(
    state: _ConfigState,
    *,
    window_hours: int,
    lesson_id: str | None,
) -> dict[str, Any]:
    _validate_window_hours(window_hours)
    hub = _load_ops_hub_payload(state, window_hours=window_hours, lesson_id=lesson_id)
    if state.demo:
        webhook_inbox = _demo_webhook_inbox_payload(limit=10)
        academy_contexts = _demo_academy_contexts_payload(class_name=None)
    else:
        cfg, _error = state.load()
        webhook_inbox = _webhook_inbox_payload(Path(cfg.webhook.dump_dir), limit=10) if cfg else {}
        academy_contexts: dict[str, Any] = {}
        if cfg:
            try:
                diagnostics = _diagnostics_payload(diagnose_apis(cfg, live=False))
                if _service_state(diagnostics, "Notion") != "blocked":
                    students = NotionRepo.from_config(cfg).list_active_students()
                    academy_contexts = _academy_contexts_payload(
                        cfg,
                        students,
                        class_name=None,
                    )
            except Exception as exc:
                academy_contexts = {
                    "ok": False,
                    "error": _safe_exception(exc),
                    "summary": {},
                    "items": [],
                    "needs_review_items": [],
                }
    return _ops_report_payload(
        hub=hub,
        webhook_inbox=webhook_inbox,
        academy_contexts=academy_contexts,
    )


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


def _required_payload_text(payload: dict[str, Any], key: str, label: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail=f"{label} 값이 필요합니다.")
    return value


def _validate_window_hours(window_hours: int) -> None:
    if window_hours < 1 or window_hours > 720:
        raise HTTPException(status_code=400, detail="window_hours는 1~720 사이여야 합니다.")


def _validate_webhook_limit(limit: int) -> None:
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit은 1~100 사이여야 합니다.")


def _validate_handoff_limit(limit: int) -> None:
    if limit < 1 or limit > 50:
        raise HTTPException(status_code=400, detail="limit은 1~50 사이여야 합니다.")


def _parse_window_hours(value: Any) -> int:
    try:
        window_hours = int(24 if value in (None, "") else value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="window_hours는 숫자여야 합니다.") from exc
    _validate_window_hours(window_hours)
    return window_hours


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        if text.isdigit():
            return datetime.fromtimestamp(int(text), tz=timezone.utc)
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="datetime 값은 ISO 형식 또는 epoch seconds 여야 합니다.",
        ) from exc


def _parse_sso_device_type(value: Any) -> int:
    try:
        device_type = int(1 if value in (None, "") else value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="device_type은 1, 2, 3 중 하나여야 합니다.") from exc
    if device_type not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="device_type은 1, 2, 3 중 하나여야 합니다.")
    return device_type


def _parse_sso_life_time(value: Any) -> int:
    try:
        life_time = int(86400 if value in (None, "") else value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="life_time은 숫자여야 합니다.") from exc
    if life_time < 60 or life_time > 604800:
        raise HTTPException(status_code=400, detail="life_time은 60~604800초 사이여야 합니다.")
    return life_time


def _sso_device_label(device_type: int) -> str:
    return {1: "PC/Mac", 2: "iOS", 3: "Android"}.get(device_type, "Unknown")


def _demo_sso_link(
    *,
    uid: str,
    course_id: str,
    class_id: str,
    device_type: int,
    life_time: int,
) -> str:
    return "https://demo.classin.local/open?" + urlencode(
        {
            "uid": uid,
            "courseId": course_id,
            "classId": class_id,
            "deviceType": device_type,
            "lifeTime": life_time,
        }
    )


def _mask_sso_link(link: str) -> str:
    if link.startswith("classin://"):
        return "classin://..."
    parsed = urlsplit(link)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/..."
    return "생성됨"


def _parse_week_start(value: str | None) -> datetime:
    text = (value or "").strip()
    if not text:
        return datetime.combine(_week_start(date_cls.today()), datetime.min.time(), tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="week는 YYYY-MM-DD 형식이어야 합니다.") from exc


def _ok(message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, **extra})


def _demo_ok(message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": True, "demo": True, "message": message, **extra})


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


def _demo_status_payload(config_path: Path) -> dict[str, Any]:
    missing_rows = _demo_missing_rows()
    history = _demo_notification_history()
    return {
        "ok": True,
        "mode": "demo",
        "config_path": str(config_path),
        "error": None,
        "academy": _DEMO_ACADEMY,
        "webhook": {
            "host": "127.0.0.1",
            "port": 8790,
            "dump_dir": "demo://incoming",
        },
        "output": {
            "daily_mode": "html",
            "daily_path": "demo://daily",
            "weekly_mode": "html",
            "weekly_path": "demo://weekly",
            "memo_mode": "off",
            "notify_mode": "dry_run",
        },
        "counts": {
            "incoming_json": 6,
            "daily_html": 1,
            "weekly_html": 5,
            "weekly_indexes": 1,
            "notification_history": len(history),
        },
        "demo": {
            "students": 5,
            "missing_rows": len(missing_rows),
            "external_writes": False,
        },
    }


def _demo_ops_hub_payload(config_path: Path) -> dict[str, Any]:
    diagnostics = {
        "ok": True,
        "ready": True,
        "live": False,
        "summary": {"ok": 4, "warn": 0, "missing": 0, "failed": 0, "skipped": 0},
        "items": [
            {"service": "ClassIn", "status": "ok"},
            {"service": "Notion", "status": "ok"},
            {"service": "Claude", "status": "ok"},
            {"service": "Aligo", "status": "ok"},
        ],
    }
    return _ops_hub_payload(
        status=_demo_status_payload(config_path),
        diagnostics=diagnostics,
        weekly_drafts=_demo_weekly_drafts_payload(
            datetime.combine(
                _week_start(date_cls.today()),
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
        ),
        missing=_demo_missing_homework_payload(),
        report_compositions=_demo_report_compositions_payload(
            datetime.combine(
                _week_start(date_cls.today()),
                datetime.min.time(),
                tzinfo=timezone.utc,
            ),
            class_name=None,
        ),
        missing_error=None,
    )


def _readiness_payload(report: ReadinessReport) -> dict[str, Any]:
    items = [
        {
            "stage": item.stage,
            "label": item.label,
            "status": item.status,
            "detail": item.detail,
            "fix": item.fix,
        }
        for item in report.items
    ]
    return _readiness_items_payload(
        mode=report.mode,
        items=items,
        ready=report.ready,
        demo=False,
    )


def _demo_readiness_payload(mode: str) -> dict[str, Any]:
    if mode not in ("local-demo", "classin-live", "kakao-live"):
        raise HTTPException(status_code=400, detail="mode must be one of: local-demo, classin-live, kakao-live")
    items = [
        _readiness_item("local-demo", "데모 데이터", "ok", "5명 페르소나와 샘플 이벤트 준비됨"),
        _readiness_item("local-demo", "Webhook 샘플", "ok", "Attendance/Homework/OMR 이벤트 표시 가능"),
        _readiness_item("local-demo", "알림 모드", "ok", "외부 발송 없이 검토"),
    ]
    if mode in ("classin-live", "kakao-live"):
        items.extend(
            [
                _readiness_item("classin-live", "ClassIn 실기관 키", "missing", "데모 모드라 실제 SID/secret 미사용", "config.yaml 에 실기관 값을 채우세요."),
                _readiness_item("classin-live", "Webhook 공개 URL 등록", "warn", "데모에서는 Cloudflare Tunnel 확인 생략", "운영 URL + /classin/webhook 을 ClassIn Datasub 에 등록하세요."),
                _readiness_item("classin-live", "Notion DB 권한", "missing", "데모 모드라 실제 Notion Integration 미사용", "setup-notion 후 DB ID를 config.yaml 에 넣으세요."),
            ]
        )
    if mode == "kakao-live":
        items.extend(
            [
                _readiness_item("kakao-live", "카톡 실발송 모드", "missing", "데모는 외부 발송 없는 검토 전용", "notify.mode 를 live 로 바꾸세요."),
                _readiness_item("kakao-live", "알리고 senderkey", "missing", "승인된 발신프로필 키 필요", "notify.aligo.sender_key 를 채우세요."),
                _readiness_item("kakao-live", "숙제 미제출 템플릿 코드", "missing", "알림톡 템플릿 심사 후 확보", "notify.aligo.template_code_missing_homework 를 채우세요."),
            ]
        )
    ready = not any(item["status"] in ("missing", "blocked") for item in items)
    return _readiness_items_payload(mode=mode, items=items, ready=ready, demo=True)


def _readiness_item(
    stage: str,
    label: str,
    status: str,
    detail: str,
    fix: str = "",
) -> dict[str, str]:
    return {
        "stage": stage,
        "label": label,
        "status": status,
        "detail": detail,
        "fix": fix,
    }


def _readiness_items_payload(
    *,
    mode: str,
    items: list[dict[str, Any]],
    ready: bool,
    demo: bool,
) -> dict[str, Any]:
    statuses = ("ok", "warn", "missing", "blocked")
    summary = {
        status: sum(1 for item in items if item.get("status") == status)
        for status in statuses
    }
    return {
        "ok": True,
        "demo": demo,
        "mode": mode,
        "ready": ready,
        "summary": {
            **summary,
            "total": len(items),
            "blockers": summary["missing"] + summary["blocked"],
            "warnings": summary["warn"],
        },
        "items": items,
    }


def _parse_schema_prefix(value: str) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return "ClassIn Toolkit"
    if len(text) > 80:
        raise HTTPException(status_code=400, detail="prefix는 80자 이하여야 합니다.")
    return text


def _notion_schema_payload(prefix: str) -> dict[str, Any]:
    schema = dry_run_schema(prefix)
    items = [
        {
            "index": index,
            "title": title,
            "kind": _notion_schema_kind(title),
            "property_count": len(properties),
            "properties": properties,
        }
        for index, (title, properties) in enumerate(schema, start=1)
    ]
    return {
        "ok": True,
        "prefix": prefix,
        "summary": {
            "databases": len(items),
            "properties": sum(item["property_count"] for item in items),
        },
        "items": items,
        "commands": {
            "dry_run": f'classin-toolkit setup-notion --parent-page-id <NOTION_PAGE_ID> --prefix "{prefix}" --dry-run',
            "write": f'classin-toolkit setup-notion --parent-page-id <NOTION_PAGE_ID> --prefix "{prefix}" --write',
        },
        "config_snippet": _notion_config_template(),
    }


def _notion_schema_kind(title: str) -> str:
    for label in ("학생 Master", "수업 기록", "리포트", "메모", "시험"):
        if label in title:
            return label
    return title


def _notion_config_template() -> str:
    return (
        "notion:\n"
        '  token: "secret_..."\n'
        "  databases:\n"
        '    students: "<STUDENTS_DB_ID>"\n'
        '    lessons: "<LESSONS_DB_ID>"\n'
        '    reports: "<REPORTS_DB_ID>"\n'
        '    memos: "<MEMOS_DB_ID>"\n'
        '    exams: "<EXAMS_DB_ID>"\n'
    )


def _pilot_brief_payload(
    cfg: AppConfig | None,
    *,
    demo: bool,
    config_error: str | None = None,
) -> dict[str, Any]:
    academy = cfg.academy.name if cfg else (_DEMO_ACADEMY if demo else "Academy")
    public_base = _pilot_public_base(cfg)
    datasub_url = f"{public_base}/classin/webhook" if public_base else "https://webhook.<academy-domain>/classin/webhook"
    health_url = f"{public_base}/health" if public_base else "https://webhook.<academy-domain>/health"
    local_url = f"http://127.0.0.1:{cfg.webhook.port if cfg else 8787}/classin/webhook"
    commands = _pilot_commands(academy=academy)
    email_body = _pilot_datasub_email(
        academy=academy,
        datasub_url=datasub_url,
        health_url=health_url,
    )
    checklist = _pilot_checklist(cfg, config_error=config_error, public_base=public_base)
    summary = _pilot_summary(checklist)
    markdown = _pilot_markdown(
        academy=academy,
        summary=summary,
        checklist=checklist,
        commands=commands,
        email_body=email_body,
        datasub_url=datasub_url,
        health_url=health_url,
        local_url=local_url,
    )
    return {
        "ok": True,
        "demo": demo,
        "academy": academy,
        "summary": summary,
        "endpoint": {
            "public_base": public_base,
            "datasub_url": datasub_url,
            "health_url": health_url,
            "local_url": local_url,
        },
        "checklist": checklist,
        "commands": commands,
        "datasub": {
            "recipients": ["an.vu@classin.com", "anh.nguyen@classin.com"],
            "subject": f"[DataSub 등록 요청] {academy} ClassIn Webhook 연동",
            "body": email_body,
        },
        "markdown": markdown,
    }


def _pilot_public_base(cfg: AppConfig | None) -> str:
    if not cfg:
        return ""
    candidates = [
        cfg.output.daily.public_url_base,
    ]
    for candidate in candidates:
        text = str(candidate or "").strip().rstrip("/")
        if not text:
            continue
        parsed = urlsplit(text)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _pilot_commands(*, academy: str) -> dict[str, str]:
    slug = _slugify_academy(academy)
    return {
        "serve_webhook": "scripts/serve-webhook.sh",
        "quick_tunnel": "scripts/tunnel-quick.sh",
        "named_tunnel_create": f"cloudflared tunnel create {slug}",
        "named_tunnel_route": f"cloudflared tunnel route dns {slug} webhook.<academy-domain>",
        "named_tunnel_run": f"cloudflared tunnel run {slug}",
        "windows_install": f".\\scripts\\install-windows-tasks.ps1 -TunnelName {slug}",
        "check_ready": "classin-toolkit check-ready --mode classin-live",
        "diagnose_live": "classin-toolkit diagnose-apis --live",
    }


def _pilot_checklist(
    cfg: AppConfig | None,
    *,
    config_error: str | None,
    public_base: str,
) -> list[dict[str, Any]]:
    notion_ready = bool(
        cfg
        and cfg.notion.token
        and cfg.notion.databases.students
        and cfg.notion.databases.lessons
        and cfg.notion.databases.reports
        and cfg.notion.databases.memos
        and cfg.notion.databases.exams
        and not any(
            _ui_value_missing(value)
            for value in (
                cfg.notion.token,
                cfg.notion.databases.students,
                cfg.notion.databases.lessons,
                cfg.notion.databases.reports,
                cfg.notion.databases.memos,
                cfg.notion.databases.exams,
            )
        )
    )
    classin_ready = bool(
        cfg
        and not _ui_value_missing(cfg.classin.school_id)
        and not _ui_value_missing(cfg.classin.secret_key)
        and not _ui_value_missing(cfg.classin.webhook_secret)
    )
    teacher_ready = bool(
        cfg
        and (
            cfg.classin.schedule_api != "lms"
            or cfg.classin.teacher_uids
            or not _ui_value_missing(cfg.classin.default_teacher_uid)
        )
    )
    llm_ready = bool(
        cfg
        and (
            (cfg.llm.provider == "gemini" and not _ui_value_missing(cfg.gemini.api_key))
            or (cfg.llm.provider == "anthropic" and not _ui_value_missing(cfg.anthropic.api_key))
        )
    )
    return [
        _pilot_step(
            "config",
            "config.yaml",
            "ok" if cfg else "missing",
            "설정 파일 로드됨" if cfg else (config_error or "config.yaml을 준비하세요."),
            "cp config.yaml.example config.yaml 후 학원별 값을 채우세요.",
        ),
        _pilot_step(
            "notion",
            "Notion DB 5종",
            "ok" if notion_ready else "missing",
            "학생·수업·리포트·메모·시험 DB ID 입력됨" if notion_ready else "DB ID 또는 token 누락",
            "설정 탭의 Notion DB 설계 미리보기 → setup-notion --write 후 config.yaml에 ID 5개 입력",
        ),
        _pilot_step(
            "classin",
            "ClassIn 실연동 키",
            "ok" if classin_ready else "missing",
            "SID, secret_key, webhook_secret 입력됨" if classin_ready else "ClassIn 실연동 키 누락",
            "ClassIn 대시보드에서 SID/signing key/SafeKey secret을 확정하세요.",
        ),
        _pilot_step(
            "teacher_uid",
            "교사 식별번호 매핑",
            "ok" if teacher_ready else "missing",
            "LMS 스케줄 생성 가능" if teacher_ready else "ClassIn 수업 생성용 교사 식별번호 누락",
            "classin.teacher_uids 또는 classin.default_teacher_uid를 채우세요.",
        ),
        _pilot_step(
            "llm",
            "LLM API",
            "ok" if llm_ready else "missing",
            f"{cfg.llm.provider} provider 준비됨" if llm_ready and cfg else "LLM API 키 누락",
            "anthropic.api_key 또는 gemini.api_key를 채우세요.",
        ),
        _pilot_step(
            "tunnel",
            "고정 Webhook URL",
            "review" if public_base else "warn",
            public_base or "아직 config에서 공개 URL을 확인할 수 없음",
            "Cloudflare named tunnel을 만들고 output.daily.public_url_base에 같은 origin을 기록하세요.",
        ),
        _pilot_step(
            "datasub",
            "ClassIn DataSub 등록",
            "review",
            "등록 메일 발송 후 ClassIn Cmd:Test 확인 필요",
            "아래 DataSub 신청 메일을 복사해 담당자에게 보내세요.",
        ),
        _pilot_step(
            "windows",
            "Windows 상시 구동",
            "review",
            "작업 스케줄러 등록 필요",
            "scripts/install-windows-tasks.ps1로 수신기와 Cloudflare Tunnel 작업을 등록하세요.",
        ),
    ]


def _pilot_step(
    step_id: str,
    title: str,
    status: str,
    detail: str,
    fix: str,
) -> dict[str, str]:
    return {
        "id": step_id,
        "title": title,
        "status": status,
        "detail": detail,
        "fix": fix,
    }


def _pilot_summary(checklist: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(checklist),
        "ok": sum(1 for item in checklist if item.get("status") == "ok"),
        "review": sum(1 for item in checklist if item.get("status") == "review"),
        "warn": sum(1 for item in checklist if item.get("status") == "warn"),
        "missing": sum(1 for item in checklist if item.get("status") == "missing"),
    }


def _pilot_datasub_email(*, academy: str, datasub_url: str, health_url: str) -> str:
    return (
        "안녕하세요. ClassIn DataSub 등록을 요청드립니다.\n\n"
        f"- 학원명: {academy}\n"
        "- School ID (SID): <config.yaml classin.school_id 값을 운영자가 직접 입력>\n"
        "- Datasub: AnswerSheetScore, ExamScore, EduDt, HomeworkSubmit, HomeworkScore, Attendance, End\n"
        f"- Endpoint URL: {datasub_url}\n"
        "- 에러 통지 이메일: <운영자 이메일>\n\n"
        "확인 부탁드립니다.\n"
        f"- Health check: {health_url}\n"
        "- Webhook path: /classin/webhook (FastAPI, /api 접두사 없음)\n"
        "- 참고: ClassIn은 1기관 1엔드포인트라 운영 고정 URL로 등록 예정입니다.\n"
    )


def _pilot_markdown(
    *,
    academy: str,
    summary: dict[str, int],
    checklist: list[dict[str, Any]],
    commands: dict[str, str],
    email_body: str,
    datasub_url: str,
    health_url: str,
    local_url: str,
) -> str:
    lines = [
        f"# {academy} 파일럿 브링업",
        "",
        f"- 준비: ok {summary['ok']} / review {summary['review']} / warn {summary['warn']} / missing {summary['missing']}",
        f"- 로컬 Webhook: {local_url}",
        f"- 공개 Health: {health_url}",
        f"- ClassIn 등록 Endpoint: {datasub_url}",
        "",
        "## 체크리스트",
    ]
    for index, item in enumerate(checklist, start=1):
        lines.append(
            f"{index}. [{_md_text(item.get('status'))}] {_md_text(item.get('title'))} - {_md_text(item.get('detail'))}"
        )
        lines.append(f"   - 다음: {_md_text(item.get('fix'))}")
    lines.extend(
        [
            "",
            "## 실행 명령",
            f"- Webhook 수신기: `{commands['serve_webhook']}`",
            f"- Quick Tunnel 테스트: `{commands['quick_tunnel']}`",
            f"- Named Tunnel 생성: `{commands['named_tunnel_create']}`",
            f"- DNS 라우팅: `{commands['named_tunnel_route']}`",
            f"- Tunnel 실행: `{commands['named_tunnel_run']}`",
            f"- Windows 작업 등록: `{commands['windows_install']}`",
            f"- 준비 점검: `{commands['check_ready']}`",
            f"- 비파괴 실 API 점검: `{commands['diagnose_live']}`",
            "",
            "## ClassIn DataSub 신청 메일",
            email_body.strip(),
            "",
            "## 보안 메모",
            "- 이 브리프는 secret_key, webhook_secret, 알리고 키, 전화번호를 자동 삽입하지 않는다.",
            "- SID도 메일 발송 직전에 운영자가 config.yaml에서 직접 붙여넣는다.",
            "- Datasub 등록 후 URL 변경 시 ClassIn 재등록이 필요하다.",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def _ui_value_missing(value: str | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    upper = text.upper()
    return any(marker in upper for marker in ("REPLACE_ME", "CHANGE_ME", "TODO", "YOUR_", "INSERT_"))


def _slugify_academy(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z]+", "-", value).strip("-").lower()
    if not text:
        return "classin-academy"
    return text[:48].strip("-") or "classin-academy"


def _count_files(path: Path, pattern: str) -> int:
    if not path.exists():
        return 0
    return sum(1 for p in path.glob(pattern) if p.is_file())


def _count_history_rows(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _webhook_inbox_payload(dump_dir: Path, *, limit: int) -> dict[str, Any]:
    files = []
    if dump_dir.exists():
        files = sorted(
            (path for path in dump_dir.glob("*.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:limit]
    items = [_webhook_event_item(path) for path in files]
    return {
        "ok": True,
        "demo": False,
        "dump_dir": str(dump_dir),
        "summary": _webhook_inbox_summary(items),
        "items": items,
    }


def _webhook_event_label(cmd: Any) -> str:
    return {
        "Attendance": "출결 기록",
        "End": "수업 종료",
        "HomeworkSubmit": "숙제 제출",
        "HomeworkScore": "숙제 점수",
        "AnswerSheetScore": "OMR 점수",
        "unreadable": "읽을 수 없음",
        "unknown": "알 수 없는 이벤트",
    }.get(str(cmd or "unknown"), str(cmd or "알 수 없는 이벤트"))


def _webhook_status_label(status: Any) -> str:
    return {
        "parsed": "정리 완료",
        "review": "확인 필요",
    }.get(str(status or "review"), "확인 필요")


def _webhook_course_label(course_id: Any, class_id: Any, class_name: Any) -> str:
    class_text = str(class_name or "").strip()
    if class_text:
        return class_text
    course_text = str(course_id or "").strip()
    class_id_text = str(class_id or "").strip()
    if course_text and class_id_text:
        return f"수업 {course_text} · {class_id_text}"
    if course_text:
        return f"강좌 {course_text}"
    if class_id_text:
        return f"수업 {class_id_text}"
    return "수업 정보 없음"


def _webhook_event_item(path: Path) -> dict[str, Any]:
    stat = path.stat()
    received_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "file_name": path.name,
            "received_at": received_at,
            "status": "review",
            "status_label": _webhook_status_label("review"),
            "cmd": "unreadable",
            "event_label": _webhook_event_label("unreadable"),
            "detail": _safe_exception(exc),
            "course_id": "",
            "class_id": "",
            "course_label": _webhook_course_label("", "", ""),
            "class_name": "",
            "activity": "",
            "student_count": 0,
            "students": [],
            "action_at": "",
            "source_label": "파일 확인 필요",
        }
    if not isinstance(raw, dict):
        return {
            "file_name": path.name,
            "received_at": received_at,
            "status": "review",
            "status_label": _webhook_status_label("review"),
            "cmd": "unknown",
            "event_label": _webhook_event_label("unknown"),
            "detail": "수신 파일 구조를 확인해야 합니다.",
            "course_id": "",
            "class_id": "",
            "course_label": _webhook_course_label("", "", ""),
            "class_name": "",
            "activity": "",
            "student_count": 0,
            "students": [],
            "action_at": "",
            "source_label": "파일 확인 필요",
        }

    data = raw.get("Data")
    students = _webhook_students(data)
    cmd = str(raw.get("Cmd") or raw.get("cmd") or "unknown")
    course_id = str(raw.get("CourseID") or raw.get("courseId") or "")
    class_id = str(raw.get("ClassID") or raw.get("ClassId") or "")
    class_name = str(raw.get("ClassName") or raw.get("CourseName") or "")
    return {
        "file_name": path.name,
        "received_at": received_at,
        "status": "parsed",
        "status_label": _webhook_status_label("parsed"),
        "cmd": cmd,
        "event_label": _webhook_event_label(cmd),
        "detail": "",
        "course_id": course_id,
        "class_id": class_id,
        "course_label": _webhook_course_label(course_id, class_id, class_name),
        "class_name": class_name,
        "activity": _webhook_activity(raw, data),
        "student_count": _webhook_student_count(data, students),
        "students": students[:4],
        "action_at": _epoch_iso(raw.get("ActionTime") or raw.get("TimeStamp")),
        "source_label": "보관됨",
    }


def _webhook_students(data: Any) -> list[str]:
    if isinstance(data, list):
        return [
            str(item.get("Name") or item.get("StudentName") or item.get("Uid") or "").strip()
            for item in data
            if isinstance(item, dict) and str(item.get("Name") or item.get("StudentName") or item.get("Uid") or "").strip()
        ]
    if not isinstance(data, dict):
        return []
    info = data.get("StudentInfo")
    if isinstance(info, dict):
        name = (
            info.get("Name")
            or info.get("StudentName")
            or info.get("Uid")
            or info.get("StudentUid")
            or ""
        )
        return [str(name).strip()] if str(name).strip() else []
    inout = data.get("inoutEnd")
    if isinstance(inout, list):
        return [str(item.get("Uid")) for item in inout if isinstance(item, dict) and item.get("Uid")]
    return []


def _webhook_student_count(data: Any, students: list[str]) -> int:
    if isinstance(data, list):
        return len(data)
    if students:
        return len(students)
    if isinstance(data, dict):
        for key in ("StudentTotal", "SubmitTotal"):
            value = data.get(key)
            if isinstance(value, int):
                return value
    return 0


def _webhook_activity(raw: dict[str, Any], data: Any) -> str:
    if isinstance(data, dict):
        for key in ("ActivityName", "UnitName"):
            if data.get(key):
                return str(data[key])
    return str(raw.get("ClassName") or raw.get("CourseName") or "")


def _epoch_iso(value: Any) -> str:
    try:
        if value in (None, ""):
            return ""
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _webhook_inbox_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    by_cmd: dict[str, int] = {}
    by_event_label: dict[str, int] = {}
    for item in items:
        cmd = str(item.get("cmd") or "unknown")
        by_cmd[cmd] = by_cmd.get(cmd, 0) + 1
        label = str(item.get("event_label") or _webhook_event_label(cmd))
        by_event_label[label] = by_event_label.get(label, 0) + 1
    return {
        "total": len(items),
        "parsed": sum(1 for item in items if item.get("status") == "parsed"),
        "review": sum(1 for item in items if item.get("status") != "parsed"),
        "student_events": sum(int(item.get("student_count") or 0) for item in items),
        "newest_received_at": items[0]["received_at"] if items else "",
        "by_cmd": by_cmd,
        "by_event_label": by_event_label,
    }


def _academy_contexts_payload(
    cfg: AppConfig,
    students: list[Any],
    *,
    class_name: str | None,
) -> dict[str, Any]:
    student_rows = [_student_context_seed(student) for student in students]
    context_result = build_report_contexts(cfg, student_rows)
    items = [
        _academy_context_item(student, context_result.contexts.get(student.classin_id))
        for student in students
    ]
    items.sort(
        key=lambda item: (
            not item["has_context"],
            item["class_name"],
            item["student_name"],
            item["student_classin_id"],
        )
    )
    classes = sorted({item["class_name"] for item in items if item["class_name"]})
    return {
        "ok": True,
        "demo": False,
        "class_name": class_name,
        "summary": _academy_context_summary(context_result.summary, items),
        "classes": classes,
        "needs_review_items": [
            _academy_context_review_item(item)
            for item in context_result.needs_review_items[:30]
        ],
        "items": items,
    }


def _student_context_seed(student: Any) -> dict[str, Any]:
    return {
        "student_classin_id": student.classin_id,
        "student_name": student.name,
        "student_class_name": student.class_name or "",
    }


def _academy_context_item(student: Any, context: dict[str, Any] | None) -> dict[str, Any]:
    context = context or _empty_report_context()
    sources = context.get("sources") or []
    weekly = context.get("weekly_report") or {}
    return {
        "student_classin_id": student.classin_id,
        "student_name": student.name,
        "class_name": student.class_name or "",
        "has_context": bool(context.get("has_context")),
        "summary": context.get("summary") or "",
        "badges": context.get("badges") or [],
        "weekly_report": {
            "status": weekly.get("status") or "",
            "approved": bool(weekly.get("approved")),
            "period_start": weekly.get("period_start") or "",
            "period_end": weekly.get("period_end") or "",
        },
        "counts": {
            "offline_attendance": int(context.get("offline_attendance") or 0),
            "offline_scores": int(context.get("offline_scores") or 0),
            "memos": int(context.get("memos") or 0),
            "sources": len(sources),
        },
        "sources": [_academy_context_source_item(source) for source in sources[:4]],
    }


def _academy_context_source_item(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": str(source.get("kind") or ""),
        "source_name": _source_name(source.get("source") or ""),
        "date": str(source.get("date") or ""),
        "detail": str(source.get("detail") or ""),
        "student": str(source.get("student") or ""),
    }


def _academy_context_review_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": str(item.get("kind") or ""),
        "student_name": str(item.get("student_name") or ""),
        "class_name": str(item.get("class_name") or ""),
        "date": str(item.get("date") or ""),
        "detail": str(item.get("detail") or ""),
        "source_name": _source_name(item.get("source") or ""),
        "reason": str(item.get("reason") or "학생 자동 매칭 필요"),
    }


def _source_name(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if "://" in text:
        return text
    return Path(text).name or text


def _academy_context_summary(
    source_summary: dict[str, int],
    items: list[dict[str, Any]],
) -> dict[str, int]:
    return {
        "total_students": len(items),
        "students_with_context": int(source_summary.get("students_with_context", 0)),
        "students_without_context": sum(1 for item in items if not item.get("has_context")),
        "weekly_reports": int(source_summary.get("weekly_reports", 0)),
        "offline_attendance": int(source_summary.get("offline_attendance", 0)),
        "offline_scores": int(source_summary.get("offline_scores", 0)),
        "memos": int(source_summary.get("memos", 0)),
        "needs_review": int(source_summary.get("needs_review", 0)),
    }


def _demo_webhook_inbox_payload(*, limit: int) -> dict[str, Any]:
    today = date_cls.today().isoformat()
    items = [
        {
            "file_name": f"{today}T190001_Attendance.json",
            "received_at": f"{today}T10:00:01+00:00",
            "status": "parsed",
            "cmd": "Attendance",
            "detail": "",
            "course_id": "132323",
            "class_id": "2362301",
            "class_name": "고2 수학 A반",
            "activity": "수1 - 지수함수 1",
            "student_count": 5,
            "students": ["박성실", "김지각", "이하락", "정활발"],
            "action_at": f"{today}T10:00:00+00:00",
        },
        {
            "file_name": f"{today}T190512_End.json",
            "received_at": f"{today}T10:05:12+00:00",
            "status": "parsed",
            "cmd": "End",
            "detail": "",
            "course_id": "132323",
            "class_id": "2362301",
            "class_name": "고2 수학 A반",
            "activity": "참여도 요약",
            "student_count": 5,
            "students": ["10001", "10002", "10003", "10004"],
            "action_at": f"{today}T10:05:00+00:00",
        },
        {
            "file_name": f"{today}T203100_HomeworkSubmit.json",
            "received_at": f"{today}T11:31:00+00:00",
            "status": "parsed",
            "cmd": "HomeworkSubmit",
            "detail": "",
            "course_id": "132323",
            "class_id": "2362301",
            "class_name": "",
            "activity": "워크북 p.42-48",
            "student_count": 1,
            "students": ["박성실"],
            "action_at": f"{today}T11:30:00+00:00",
        },
        {
            "file_name": f"{today}T204010_AnswerSheetScore.json",
            "received_at": f"{today}T11:40:10+00:00",
            "status": "parsed",
            "cmd": "AnswerSheetScore",
            "detail": "",
            "course_id": "132323",
            "class_id": "",
            "class_name": "고2-A",
            "activity": "6월 OMR 답안지",
            "student_count": 1,
            "students": ["박성실"],
            "action_at": f"{today}T11:40:00+00:00",
        },
    ][:limit]
    return {
        "ok": True,
        "demo": True,
        "dump_dir": "demo://incoming",
        "summary": _webhook_inbox_summary(items),
        "items": items,
    }


def _demo_academy_contexts_payload(*, class_name: str | None) -> dict[str, Any]:
    students = [student for student in DEMO_STUDENTS if not class_name or student.class_name == class_name]
    contexts = _demo_report_contexts()
    items = [_academy_context_item(student, contexts.get(student.classin_id)) for student in students]
    items.sort(
        key=lambda item: (
            not item["has_context"],
            item["class_name"],
            item["student_name"],
            item["student_classin_id"],
        )
    )
    source_summary = {
        "students_with_context": sum(1 for item in items if item["has_context"]),
        "weekly_reports": sum(1 for item in items if item["weekly_report"]["status"]),
        "offline_attendance": sum(item["counts"]["offline_attendance"] for item in items),
        "offline_scores": sum(item["counts"]["offline_scores"] for item in items),
        "memos": sum(item["counts"]["memos"] for item in items),
        "needs_review": 1,
    }
    review_items = [
        {
            "kind": "offline_score",
            "student_name": "동명이인",
            "class_name": "고2-A",
            "date": "2026-04-24",
            "detail": "월말평가 71점",
            "source_name": "demo://local_data/inbox/scores/april.xlsx",
            "reason": "학생 자동 매칭 필요",
        }
    ]
    return {
        "ok": True,
        "demo": True,
        "class_name": class_name,
        "summary": _academy_context_summary(source_summary, items),
        "classes": sorted({student.class_name for student in DEMO_STUDENTS if student.class_name}),
        "needs_review_items": review_items,
        "items": items,
    }


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


def _missing_homework_payload(
    rows: list[dict],
    history: list[dict[str, Any]],
    *,
    report_contexts: dict[str, dict[str, Any]] | None = None,
    data_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    latest_by_student = _latest_notification_by_student(history)
    missing_counts = _missing_count_by_student(rows)
    report_contexts = report_contexts or {}
    items = []
    for row in rows:
        student_id = row.get("student_classin_id") or ""
        latest = latest_by_student.get(student_id)
        has_parent_phone = bool(row.get("parent_phone"))
        notification_status = latest.get("status") if latest else "pending"
        items.append(
            {
                "student_classin_id": student_id,
                "selection_key": missing_homework_selection_key(row),
                "student_name": row.get("student_name") or "미등록",
                "class_name": row.get("student_class_name") or "",
                "parent_phone": row.get("parent_phone") or "",
                "has_parent_phone": has_parent_phone,
                "lesson_classin_id": row.get("lesson_classin_id") or "",
                "course_classin_id": row.get("course_classin_id") or "",
                "date": row.get("date") or "",
                "attendance": row.get("attendance") or "",
                "homework_late": row.get("homework_late"),
                "homework_score": row.get("homework_score"),
                "notification_status": notification_status,
                "notification_at": latest.get("created_at") if latest else "",
                "notification_provider": latest.get("provider") if latest else "",
                "notification_quality_status": latest.get("quality_status") if latest else "",
                "notification_quality_score": latest.get("quality_score") if latest else 0,
                "notification_quality_warnings": latest.get("quality_warnings") if latest else [],
                "missing_count": missing_counts.get(student_id, 1),
                "is_repeat": missing_counts.get(student_id, 1) > 1,
                "action_required": _missing_action(has_parent_phone, notification_status),
                "report_context": report_contexts.get(student_id, _empty_report_context()),
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
        "needs_phone": sum(1 for item in items if item["action_required"] == "needs_phone"),
        "needs_message": sum(1 for item in items if item["action_required"] == "needs_message"),
        "needs_review": sum(1 for item in items if item["action_required"] == "needs_review"),
        "needs_retry": sum(1 for item in items if item["action_required"] == "needs_retry"),
        "repeat_students": len({item["student_classin_id"] for item in items if item["is_repeat"]}),
        "quality_ready": sum(1 for item in items if item["notification_quality_status"] == "ready"),
        "quality_review": sum(1 for item in items if item["notification_quality_status"] == "review"),
        "quality_blocked": sum(1 for item in items if item["notification_quality_status"] == "blocked"),
    }
    return {
        "ok": True,
        "summary": summary,
        "items": items,
        "data_context": data_context or {"summary": {}, "needs_review_items": []},
    }


def _missing_homework_preview_payload(
    messages: list[OutgoingMessage],
    *,
    notify_mode: str,
    demo: bool,
) -> dict[str, Any]:
    items = [_missing_homework_preview_item(message, notify_mode=notify_mode) for message in messages]
    summary = {
        "total": len(items),
        "dispatchable": sum(1 for item in items if item["dispatchable"]),
        "ready": sum(1 for item in items if item["quality_status"] == "ready"),
        "review": sum(1 for item in items if item["quality_status"] == "review"),
        "blocked": sum(1 for item in items if item["quality_status"] == "blocked"),
        "no_parent_phone": sum(1 for item in items if not item["has_parent_phone"]),
        "live_blocked": sum(1 for item in items if notify_mode == "live" and not item["dispatchable"]),
    }
    return {
        "ok": True,
        "demo": demo,
        "notify_mode": notify_mode,
        "summary": summary,
        "items": items,
    }


def _missing_homework_preview_item(
    message: OutgoingMessage,
    *,
    notify_mode: str,
) -> dict[str, Any]:
    has_parent_phone = bool(str(message.parent_phone or "").strip())
    dispatchable = (
        message.quality_status == "ready" and has_parent_phone
        if notify_mode == "live"
        else message.quality_status != "blocked"
    )
    return {
        "student_classin_id": message.student_classin_id,
        "student_name": message.student_name,
        "parent_phone_masked": _mask_phone(message.parent_phone),
        "has_parent_phone": has_parent_phone,
        "message": message.message,
        "quality_status": message.quality_status,
        "quality_score": message.quality_score,
        "quality_warnings": message.quality_warnings,
        "dispatchable": dispatchable,
        "block_reason": _missing_preview_block_reason(
            message,
            notify_mode=notify_mode,
            has_parent_phone=has_parent_phone,
        ),
    }


def _missing_preview_block_reason(
    message: OutgoingMessage,
    *,
    notify_mode: str,
    has_parent_phone: bool,
) -> str:
    if not has_parent_phone:
        return "보호자 연락처가 없습니다."
    if message.quality_status == "blocked":
        return "문구 품질이 발송 제외 상태입니다."
    if notify_mode == "live" and message.quality_status != "ready":
        return "실제 발송은 품질 검토를 통과한 문구만 허용됩니다."
    return ""


def _demo_missing_homework_preview_payload(
    *,
    selection_keys: list[str] | None,
) -> dict[str, Any]:
    rows = _demo_missing_rows()
    selected = set(selection_keys or [])
    if selected:
        rows = [row for row in rows if missing_homework_selection_key(row) in selected]
    student_by_id = {student.classin_id: student for student in DEMO_STUDENTS}
    messages: list[OutgoingMessage] = []
    for row in rows[:5]:
        student_id = str(row.get("student_classin_id") or "")
        student = student_by_id.get(student_id)
        name = student.name if student else row.get("student_name") or "미등록"
        parent_phone = student.parent_phone if student else row.get("parent_phone")
        quality_status = "ready" if parent_phone else "blocked"
        messages.append(
            OutgoingMessage(
                student_classin_id=student_id,
                student_name=name,
                parent_phone=parent_phone,
                message=f"{name} 학생의 숙제 제출 확인이 필요합니다. 오늘 중 제출할 수 있도록 가정에서도 한 번만 확인 부탁드립니다.",
                quality_status=quality_status,
                quality_score=92 if quality_status == "ready" else 35,
                quality_warnings=[] if quality_status == "ready" else ["보호자 연락처가 없습니다."],
            )
        )
    return _missing_homework_preview_payload(messages, notify_mode="dry_run", demo=True)


def _mask_phone(value: str | None) -> str:
    digits = re.sub(r"\D+", "", value or "")
    if len(digits) < 7:
        return ""
    return f"{digits[:3]}****{digits[-4:]}"


def _empty_report_context() -> dict[str, Any]:
    return {
        "has_context": False,
        "weekly_report": {
            "status": "",
            "period_start": "",
            "period_end": "",
            "html_path": "",
            "public_url": "",
            "approved": False,
        },
        "offline_attendance": 0,
        "offline_scores": 0,
        "memos": 0,
        "badges": [],
        "summary": "",
        "sources": [],
    }


def _missing_count_by_student(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        student_id = row.get("student_classin_id") or ""
        if not student_id:
            continue
        counts[student_id] = counts.get(student_id, 0) + 1
    return counts


def _missing_action(has_parent_phone: bool, notification_status: str) -> str:
    if not has_parent_phone:
        return "needs_phone"
    if notification_status == "failed":
        return "needs_retry"
    if notification_status == "pending":
        return "needs_message"
    return "needs_review"


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


def _report_compositions_payload(
    cfg: AppConfig,
    repo: NotionRepo,
    students: list[Any],
    *,
    period_start: datetime,
    class_name: str | None,
) -> dict[str, Any]:
    period_end = period_start + timedelta(days=6, hours=23, minutes=59)
    context_result = build_report_contexts(
        cfg,
        [
            {
                "student_classin_id": student.classin_id,
                "student_name": student.name,
                "student_class_name": student.class_name,
            }
            for student in students
        ],
    )
    latest_notifications = _latest_notification_by_student(load_notification_history(cfg, limit=300))
    items: list[dict[str, Any]] = []
    for student in students:
        lessons = repo.weekly_student_stats(
            student_page_id=student.page_id,
            since=period_start,
            until=period_end,
        )
        exams = repo.student_exam_results(
            student_page_id=student.page_id,
            since=period_start,
            until=period_end,
        )
        item = compose_individual_report(
            student=student,
            lessons=lessons,
            exam_results=exams,
            academy_context=context_result.contexts.get(student.classin_id),
            latest_notification=latest_notifications.get(student.classin_id),
        ).as_dict()
        items.append(item)

    items.sort(
        key=lambda item: (
            _readiness_priority(item["readiness_status"]),
            item["class_name"],
            item["student_name"],
            item["student_classin_id"],
        )
    )
    classes = sorted({item["class_name"] for item in items if item["class_name"]})
    return {
        "ok": True,
        "class_name": class_name,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "summary": _report_composition_summary(items),
        "classes": classes,
        "data_context": {
            "summary": context_result.summary,
            "needs_review_items": context_result.needs_review_items[:20],
        },
        "items": items,
    }


def _report_composition_summary(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(items),
        "ready": sum(1 for item in items if item.get("readiness_status") == "ready"),
        "review": sum(1 for item in items if item.get("readiness_status") == "review"),
        "blocked": sum(1 for item in items if item.get("readiness_status") == "blocked"),
        "with_classin_lessons": sum(
            1 for item in items if (item.get("source_counts") or {}).get("classin_lessons", 0)
        ),
        "with_exam_signal": sum(
            1
            for item in items
            if (item.get("source_counts") or {}).get("exam_results", 0)
            or (item.get("source_counts") or {}).get("offline_scores", 0)
        ),
        "with_memo_context": sum(
            1 for item in items if (item.get("source_counts") or {}).get("memos", 0)
        ),
    }


def _student_report_pack_payload(
    compositions: dict[str, Any],
    *,
    student_classin_id: str,
) -> dict[str, Any]:
    item = next(
        (
            row
            for row in compositions.get("items") or []
            if str(row.get("student_classin_id") or "") == student_classin_id
        ),
        None,
    )
    if not item:
        raise HTTPException(status_code=404, detail="해당 학생의 리포트 구성을 찾을 수 없습니다.")
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    parent_message = _student_parent_message(item)
    teacher_checklist = _student_teacher_checklist(item)
    markdown = _student_report_pack_markdown(
        item,
        period_start=_md_text(compositions.get("period_start")),
        period_end=_md_text(compositions.get("period_end")),
        generated_at=generated_at,
        parent_message=parent_message,
        teacher_checklist=teacher_checklist,
    )
    sections = item.get("sections") or []
    return {
        "ok": True,
        "student_classin_id": item.get("student_classin_id"),
        "student_name": item.get("student_name"),
        "class_name": item.get("class_name"),
        "period_start": compositions.get("period_start"),
        "period_end": compositions.get("period_end"),
        "generated_at": generated_at,
        "readiness_status": item.get("readiness_status"),
        "readiness_score": item.get("readiness_score"),
        "focus": item.get("focus"),
        "summary": {
            "sections": len(sections),
            "ready_sections": sum(1 for section in sections if section.get("status") == "ready"),
            "review_sections": sum(1 for section in sections if section.get("status") == "review"),
            "missing_sections": sum(1 for section in sections if section.get("status") == "missing"),
            "warnings": len(item.get("readiness_warnings") or []),
            "markdown_chars": len(markdown),
        },
        "parent_message": parent_message,
        "teacher_checklist": teacher_checklist,
        "markdown": markdown,
    }


def _student_report_pack_markdown(
    item: dict[str, Any],
    *,
    period_start: str,
    period_end: str,
    generated_at: str,
    parent_message: str,
    teacher_checklist: list[str],
) -> str:
    counts = item.get("source_counts") or {}
    sections = item.get("sections") or []
    section_by_id = {section.get("id"): section for section in sections}
    warnings = item.get("readiness_warnings") or []
    lines = [
        f"# {_md_text(item.get('student_name') or '미등록')} 개별 리포트 초안",
        "",
        f"- 반: {_md_text(item.get('class_name') or '-')}",
        f"- 학생 ID: {_md_text(item.get('student_classin_id') or '-')}",
        f"- 기간: {_md_text(period_start)} ~ {_md_text(period_end)}",
        f"- 생성: {_md_text(generated_at)}",
        f"- 준비도: {_md_text(item.get('readiness_status') or '-')} / {int(item.get('readiness_score') or 0)}/100",
        "",
        "## 1. 핵심 요약",
        f"- 한 줄 판단: {_md_text(item.get('focus') or '-')}",
        f"- 통합 맥락: {_md_text(item.get('context_summary') or '-')}",
        f"- ClassIn 수업 {int(counts.get('classin_lessons') or 0)}건 / 시험 {int(counts.get('exam_results') or 0) + int(counts.get('offline_scores') or 0)}건 / 메모 {int(counts.get('memos') or 0)}건",
        "",
        "## 2. ClassIn 근거",
    ]
    for section_id in ("attendance_routine", "homework_attitude", "exam_signal"):
        lines.extend(_student_report_section_lines(section_by_id.get(section_id)))

    lines.extend(
        [
            "",
            "## 3. 학원 데이터 맥락",
            f"- 오프라인 출결: {int(counts.get('offline_attendance') or 0)}건",
            f"- 오프라인 성적: {int(counts.get('offline_scores') or 0)}건",
            f"- 상담/메모: {int(counts.get('memos') or 0)}건",
            f"- 기존 리포트: {int(counts.get('weekly_reports') or 0)}건",
        ]
    )
    memo_section = section_by_id.get("memo_context")
    if memo_section:
        lines.extend(_student_report_section_lines(memo_section))

    lines.extend(["", "## 4. 리포트 구성"])
    for section in sections:
        title = _md_text(section.get("title") or "-")
        status = _md_text(section.get("status") or "-")
        summary = _md_text(section.get("summary") or "-")
        lines.append(f"- {title}: {status} - {summary}")

    lines.extend(
        [
            "",
            "## 5. 학부모 전달 문안 초안",
            parent_message,
            "",
            "## 6. 교사 확인 체크리스트",
        ]
    )
    if warnings:
        lines.extend(f"- [ ] {_md_text(warning)}" for warning in warnings[:8])
    for action in teacher_checklist[:8]:
        lines.append(f"- [ ] {_md_text(action)}")
    if not warnings and not teacher_checklist:
        lines.append("- [ ] 승인 전 학생명, 기간, 근거를 최종 확인")
    return "\n".join(lines).strip() + "\n"


def _student_report_section_lines(section: dict[str, Any] | None) -> list[str]:
    if not section:
        return ["- 해당 섹션 근거 없음"]
    lines = [
        "- "
        f"{_md_text(section.get('title') or '-')}: "
        f"{_md_text(section.get('summary') or '-')}"
    ]
    for evidence in (section.get("evidence") or [])[:3]:
        lines.append(f"  - 근거: {_md_text(evidence)}")
    next_action = _md_text(section.get("next_action"))
    if next_action:
        lines.append(f"  - 보강: {next_action}")
    return lines


def _student_parent_message(item: dict[str, Any]) -> str:
    section_by_id = {section.get("id"): section for section in item.get("sections") or []}
    next_action = _md_text((section_by_id.get("next_action") or {}).get("summary"))
    homework = _md_text((section_by_id.get("homework_attitude") or {}).get("summary"))
    exam = _md_text((section_by_id.get("exam_signal") or {}).get("summary"))
    focus = _md_text(item.get("focus") or "이번 주 학습 흐름 점검")
    student = _md_text(item.get("student_name") or "학생")
    status = item.get("readiness_status")
    if status == "blocked":
        return (
            f"{student} 학생 리포트는 현재 근거 보강이 먼저 필요합니다. "
            "ClassIn 수업 기록과 학원 메모를 확인한 뒤 안전하게 안내드리겠습니다."
        )
    evidence = " / ".join(part for part in (homework, exam) if part)
    if not evidence:
        evidence = _md_text(item.get("context_summary") or "수업 기록 확인 중")
    action_text = next_action or "다음 수업 전 변화 신호를 확인하겠습니다."
    return (
        f"{student} 학생은 이번 주 {focus} 흐름입니다. "
        f"확인 근거는 {evidence}입니다. {action_text}"
    )


def _student_teacher_checklist(item: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    for section in item.get("sections") or []:
        action = _md_text(section.get("next_action"))
        if action:
            actions.append(f"{_md_text(section.get('title') or '섹션')}: {action}")
    seen: set[str] = set()
    unique: list[str] = []
    for action in actions:
        if action in seen:
            continue
        seen.add(action)
        unique.append(action)
    return unique


def _readiness_priority(status: str) -> int:
    return {"blocked": 0, "review": 1, "ready": 2}.get(status, 3)


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


def _ops_hub_payload(
    *,
    status: dict[str, Any],
    diagnostics: dict[str, Any] | None,
    weekly_drafts: dict[str, Any] | None,
    missing: dict[str, Any] | None,
    report_compositions: dict[str, Any] | None,
    missing_error: str | None,
) -> dict[str, Any]:
    counts = status.get("counts") or {}
    missing_summary = (missing or {}).get("summary") or {}
    data_context = (missing or {}).get("data_context") or {"summary": {}, "needs_review_items": []}
    data_summary = data_context.get("summary") or {}
    draft_summary = (weekly_drafts or {}).get("summary") or {}
    composition_summary = (report_compositions or {}).get("summary") or {}
    readiness = _hub_readiness(diagnostics)
    summary = {
        "incoming_json": counts.get("incoming_json", 0),
        "weekly_html": counts.get("weekly_html", 0),
        "weekly_indexes": counts.get("weekly_indexes", 0),
        "notification_history": counts.get("notification_history", 0),
        "total_missing": missing_summary.get("total_missing", 0),
        "needs_message": missing_summary.get("needs_message", 0),
        "needs_retry": missing_summary.get("needs_retry", 0),
        "needs_phone": missing_summary.get("needs_phone", 0),
        "repeat_students": missing_summary.get("repeat_students", 0),
        "students_with_context": data_summary.get("students_with_context", 0),
        "offline_attendance": data_summary.get("offline_attendance", 0),
        "offline_scores": data_summary.get("offline_scores", 0),
        "memos": data_summary.get("memos", 0),
        "data_needs_review": data_summary.get("needs_review", 0),
        "report_total": draft_summary.get("total", 0),
        "report_pending": draft_summary.get("pending", 0),
        "report_ready": draft_summary.get("ready_unapproved", 0),
        "report_review": draft_summary.get("review_unapproved", 0),
        "report_blocked": draft_summary.get("blocked_unapproved", 0),
        "composition_review": composition_summary.get("review", 0),
        "composition_blocked": composition_summary.get("blocked", 0),
    }
    lanes = _hub_lanes(status, diagnostics, summary, missing_error)
    focus = _hub_focus_items(status, readiness, summary, missing_error)
    work_queue = _hub_work_queue(
        (missing or {}).get("items") or [],
        data_context.get("needs_review_items") or [],
        (weekly_drafts or {}).get("items") or [],
        (report_compositions or {}).get("items") or [],
    )
    return {
        "ok": True,
        "config_ok": bool(status.get("ok")),
        "academy": status.get("academy"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "readiness": readiness,
        "lanes": lanes,
        "focus": focus,
        "ops_brief": _hub_ops_brief(
            status=status,
            readiness=readiness,
            summary=summary,
            lanes=lanes,
            missing_error=missing_error,
        ),
        "work_queue": work_queue,
        "weekly_drafts": weekly_drafts or {},
        "report_compositions": report_compositions or {},
        "data_context": data_context,
        "missing_error": missing_error,
    }


def _ops_report_payload(
    *,
    hub: dict[str, Any],
    webhook_inbox: dict[str, Any],
    academy_contexts: dict[str, Any],
) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    markdown = _ops_report_markdown(
        hub=hub,
        webhook_inbox=webhook_inbox or {},
        academy_contexts=academy_contexts or {},
        generated_at=generated_at,
    )
    return {
        "ok": True,
        "academy": hub.get("academy"),
        "generated_at": generated_at,
        "summary": {
            "lanes": len(hub.get("lanes") or []),
            "brief_items": len(hub.get("ops_brief") or []),
            "work_queue": len(hub.get("work_queue") or []),
            "markdown_chars": len(markdown),
        },
        "markdown": markdown,
    }


def _ops_handoff_dir(cfg: AppConfig) -> Path:
    return Path(cfg.reports.output_dir) / "ops"


def _ops_handoff_index_path(cfg: AppConfig) -> Path:
    return _ops_handoff_dir(cfg) / _OPS_HANDOFF_INDEX


def _save_ops_handoff(cfg: AppConfig, report: dict[str, Any]) -> dict[str, Any]:
    base_dir = _ops_handoff_dir(cfg)
    base_dir.mkdir(parents=True, exist_ok=True)
    filename = _ops_handoff_filename(report.get("generated_at"))
    path = _unique_handoff_path(base_dir / filename)
    markdown = str(report.get("markdown") or "")
    path.write_text(markdown, encoding="utf-8")

    item = _ops_handoff_item(report, path=path, demo=False)
    existing = [
        record
        for record in _load_ops_handoffs(cfg, limit=_MAX_OPS_HANDOFFS)
        if record.get("filename") != item["filename"]
    ]
    _write_ops_handoffs(cfg, [item, *existing])
    return item


def _load_ops_handoffs(cfg: AppConfig, *, limit: int) -> list[dict[str, Any]]:
    index_path = _ops_handoff_index_path(cfg)
    if not index_path.exists():
        return []
    try:
        raw = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    items = [_normalize_ops_handoff_item(item) for item in raw if isinstance(item, dict)]
    items.sort(
        key=lambda item: str(item.get("saved_at") or item.get("generated_at") or ""),
        reverse=True,
    )
    return items[:limit]


def _write_ops_handoffs(cfg: AppConfig, items: list[dict[str, Any]]) -> None:
    index_path = _ops_handoff_index_path(cfg)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(items[:_MAX_OPS_HANDOFFS], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _demo_ops_handoff_item(report: dict[str, Any]) -> dict[str, Any]:
    return _ops_handoff_item(report, path=None, demo=True)


def _ops_handoff_item(
    report: dict[str, Any],
    *,
    path: Path | None,
    demo: bool,
) -> dict[str, Any]:
    filename = path.name if path else _ops_handoff_filename(report.get("generated_at"))
    summary = report.get("summary") or {}
    generated_at = str(report.get("generated_at") or "")
    saved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    markdown = str(report.get("markdown") or "")
    return _normalize_ops_handoff_item(
        {
            "academy": str(report.get("academy") or ""),
            "generated_at": generated_at,
            "saved_at": saved_at,
            "filename": filename,
            "path": f"demo://ops/{filename}" if demo else str(path),
            "preview_url": "" if demo else f"/reports/ops/{filename}",
            "lanes": int(summary.get("lanes") or 0),
            "brief_items": int(summary.get("brief_items") or 0),
            "work_queue": int(summary.get("work_queue") or 0),
            "markdown_chars": int(summary.get("markdown_chars") or len(markdown)),
        }
    )


def _normalize_ops_handoff_item(item: dict[str, Any]) -> dict[str, Any]:
    filename = str(item.get("filename") or "")
    preview_url = (
        str(item.get("preview_url") or "")
        if "preview_url" in item
        else (f"/reports/ops/{filename}" if filename else "")
    )
    return {
        "academy": str(item.get("academy") or ""),
        "generated_at": str(item.get("generated_at") or ""),
        "saved_at": str(item.get("saved_at") or ""),
        "filename": filename,
        "path": str(item.get("path") or ""),
        "preview_url": preview_url,
        "lanes": int(item.get("lanes") or 0),
        "brief_items": int(item.get("brief_items") or 0),
        "work_queue": int(item.get("work_queue") or 0),
        "markdown_chars": int(item.get("markdown_chars") or 0),
    }


def _ops_handoff_filename(generated_at: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(generated_at or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        parsed = datetime.now(timezone.utc)
    stamp = parsed.astimezone(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    return f"{_safe_filename(stamp)}_ops.md"


def _safe_filename(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z가-힣_.-]+", "_", value).strip("._")
    return text or "ops"


def _unique_handoff_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise HTTPException(status_code=500, detail="운영 리포트 파일명을 만들 수 없습니다.")


def _ops_playbook_payload(*, hub: dict[str, Any]) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    steps = _ops_playbook_steps(hub)
    markdown = _ops_playbook_markdown(hub=hub, steps=steps, generated_at=generated_at)
    return {
        "ok": True,
        "academy": hub.get("academy"),
        "generated_at": generated_at,
        "summary": {
            "total_steps": len(steps),
            "ready": sum(1 for step in steps if step["status"] == "ready"),
            "review": sum(1 for step in steps if step["status"] == "review"),
            "blocked": sum(1 for step in steps if step["status"] == "blocked"),
            "dry_run": sum(1 for step in steps if step["risk"] == "dry_run"),
            "live_guarded": sum(1 for step in steps if step["risk"] == "live_guard"),
            "estimated_minutes": sum(int(step.get("duration_min") or 0) for step in steps),
            "markdown_chars": len(markdown),
        },
        "steps": steps,
        "markdown": markdown,
    }


def _ops_playbook_steps(hub: dict[str, Any]) -> list[dict[str, Any]]:
    summary = hub.get("summary") or {}
    readiness = hub.get("readiness") or {}
    notify_mode = ((hub.get("status") or {}).get("output") or {}).get("notify_mode")
    steps = [
        _playbook_step(
            "settings",
            "점검",
            "설정·모드 확인",
            "ClassIn/Notion/LLM/알림 설정과 현재 발송 모드를 먼저 확인합니다.",
            "원장",
            "blocked" if not hub.get("config_ok") or readiness.get("blockers") else "ready",
            "review",
            "설정 점검",
            "dashboard",
            "refreshDiagnostics",
            "classin-toolkit check-ready --mode local-demo",
            f"막힘 {int(readiness.get('blockers') or 0)}건, 확인 {int(readiness.get('warnings') or 0)}건, 발송 방식 {_notify_mode_text(notify_mode)}",
            3,
            notes=["실 API 점검과 실제 생성은 원장 확인 후 실행"],
        )
    ]

    if int(summary.get("incoming_json") or 0) or int(summary.get("total_missing") or 0):
        steps.append(
            _playbook_step(
                "data_subscription",
                "데이터",
                "ClassIn 수신 데이터 확인",
                "ClassIn 수신 기록에서 출결·숙제·OMR 이벤트가 정상적으로 들어왔는지 확인합니다.",
                "교사",
                "review" if int(summary.get("data_needs_review") or 0) else "ready",
                "safe",
                "수신함 보기",
                "data",
                "refreshWebhookInbox",
                "classin-toolkit replay-webhook <sample.json>",
                f"수신 원본 {int(summary.get('incoming_json') or 0)}건, 매칭 확인 {int(summary.get('data_needs_review') or 0)}건",
                4,
                notes=["원본 파일은 개인정보가 있을 수 있어 외부 공유 금지"],
            )
        )

    if int(summary.get("total_missing") or 0):
        retry = int(summary.get("needs_retry") or 0)
        needs_phone = int(summary.get("needs_phone") or 0)
        steps.append(
            _playbook_step(
                "missing_homework",
                "알림",
                "미제출 알림 큐 처리",
                "연락처·실패 이력·문구 품질을 확인한 뒤 발송 전 검토 기록을 만듭니다.",
                "교사",
                "review" if retry or needs_phone else "ready",
                "dry_run",
                "미제출 처리",
                "missing",
                "focusMissingCore",
                "classin-toolkit sweep-missing-homework",
                f"미제출 {int(summary.get('total_missing') or 0)}명, 재시도 {retry}명, 연락처 보완 {needs_phone}명",
                8,
                notes=["실제 발송 모드라면 발송 전 다시 확인"],
            )
        )

    if int(summary.get("students_with_context") or 0) or int(summary.get("data_needs_review") or 0):
        steps.append(
            _playbook_step(
                "academy_context",
                "데이터",
                "학원 데이터 매칭 보강",
                "오프라인 출결·성적·상담 메모가 학생에게 올바르게 붙었는지 확인합니다.",
                "교사",
                "review" if int(summary.get("data_needs_review") or 0) else "ready",
                "review",
                "데이터 확인",
                "data",
                "refreshAcademyContexts",
                "local_data/inbox 확인",
                f"학생 맥락 {int(summary.get('students_with_context') or 0)}명, 확인 필요 {int(summary.get('data_needs_review') or 0)}건",
                6,
                notes=["동명이인·반 누락 자료는 리포트에 자동 반영하지 않음"],
            )
        )

    report_risk = (
        int(summary.get("report_blocked") or 0)
        + int(summary.get("composition_blocked") or 0)
    )
    report_review = (
        int(summary.get("report_review") or 0)
        + int(summary.get("composition_review") or 0)
    )
    if int(summary.get("report_total") or 0) or report_risk or report_review:
        steps.append(
            _playbook_step(
                "report_quality",
                "리포트",
                "개별 리포트 품질·구성 확인",
                "차단·확인 필요 드래프트와 섹션 구성 점검을 먼저 보강합니다.",
                "교사",
                "blocked" if report_risk else ("review" if report_review else "ready"),
                "review",
                "리포트 검토",
                "reports",
                "refreshReportCompositions",
                "classin-toolkit weekly-reports",
                f"차단 {report_risk}건, 확인 필요 {report_review}건, 승인 가능 {int(summary.get('report_ready') or 0)}건",
                10,
                notes=["차단 리포트는 기본 승인 대상에서 제외"],
            )
        )

    if int(summary.get("report_ready") or 0):
        steps.append(
            _playbook_step(
                "report_approval",
                "리포트",
                "통과 리포트 승인",
                "품질 점검을 통과한 주간 드래프트만 승인 대상으로 검토합니다.",
                "원장",
                "review",
                "live_guard",
                "승인 대기 확인",
                "reports",
                "refreshWeeklyDrafts",
                "classin-toolkit approve-weekly --week YYYY-MM-DD",
                f"승인 가능 {int(summary.get('report_ready') or 0)}건, 승인 대기 {int(summary.get('report_pending') or 0)}건",
                5,
                notes=["Notion 아카이브 쓰기 전 승인 범위 확인"],
            )
        )

    steps.append(
        _playbook_step(
            "handoff",
            "마감",
            "운영 리포트 생성·공유",
            "오늘 처리한 큐와 남은 보강 항목을 Markdown으로 남깁니다.",
            "원장",
            "ready",
            "safe",
            "운영 리포트 만들기",
            "dashboard",
            "generateOpsReport",
            "classin-toolkit ui --demo",
            f"브리핑 {len(hub.get('ops_brief') or [])}건, 학생 큐 {len(hub.get('work_queue') or [])}명",
            3,
            notes=["학부모 연락처·접속 링크는 운영 리포트에 포함하지 않음"],
        )
    )
    return steps


def _playbook_step(
    step_id: str,
    phase: str,
    title: str,
    detail: str,
    owner: str,
    status: str,
    risk: str,
    action_label: str,
    tab: str,
    ui_action: str,
    command: str,
    evidence: str,
    duration_min: int,
    *,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": step_id,
        "phase": phase,
        "title": title,
        "detail": detail,
        "owner": owner,
        "status": status,
        "risk": risk,
        "action_label": action_label,
        "tab": tab,
        "ui_action": ui_action,
        "command": command,
        "evidence": evidence,
        "duration_min": duration_min,
        "notes": notes or [],
    }


def _ops_playbook_markdown(
    *,
    hub: dict[str, Any],
    steps: list[dict[str, Any]],
    generated_at: str,
) -> str:
    academy = _md_text(hub.get("academy") or "운영 허브")
    summary = hub.get("summary") or {}
    lines = [
        f"# {academy} 자동화 실행계획",
        "",
        f"- 생성: {_md_text(generated_at)}",
        f"- 예상 소요: {sum(int(step.get('duration_min') or 0) for step in steps)}분",
        f"- 오늘 큐: 미제출 {int(summary.get('total_missing') or 0)}명, 데이터 확인 {int(summary.get('data_needs_review') or 0)}건, 리포트 차단 {int(summary.get('report_blocked') or 0) + int(summary.get('composition_blocked') or 0)}건",
        "",
        "## 실행 순서",
    ]
    for index, step in enumerate(steps, start=1):
        lines.extend(
            [
                f"{index}. {_md_text(step.get('phase'))} - {_md_text(step.get('title'))}",
                f"   - 상태: {_ops_status_text(step.get('status'))} / 위험도: {_ops_risk_text(step.get('risk'))} / 담당: {_md_text(step.get('owner'))}",
                f"   - 실행: {_md_text(step.get('action_label'))} ({_ops_tab_text(step.get('tab'))})",
                f"   - 근거: {_md_text(step.get('evidence'))}",
            ]
        )
        for note in step.get("notes") or []:
            lines.append(f"   - 주의: {_md_text(note)}")
    lines.extend(
        [
            "",
            "## 안전 게이트",
            "- ClassIn 실제 생성·게시·Notion 아카이브는 설정 점검과 원장 확인 후 실행",
            "- 보호자 발송은 검토 문구·연락처·품질 상태를 먼저 확인",
            "- 매칭 애매한 학원 데이터는 리포트 문장에 자동 반영하지 않음",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def _ops_report_markdown(
    *,
    hub: dict[str, Any],
    webhook_inbox: dict[str, Any],
    academy_contexts: dict[str, Any],
    generated_at: str,
) -> str:
    academy = _md_text(hub.get("academy") or "운영 허브")
    summary = hub.get("summary") or {}
    readiness = hub.get("readiness") or {}
    webhook_summary = webhook_inbox.get("summary") or {}
    context_summary = academy_contexts.get("summary") or {}
    weekly_summary = ((hub.get("weekly_drafts") or {}).get("summary") or {})
    composition_summary = ((hub.get("report_compositions") or {}).get("summary") or {})

    lines = [
        f"# {academy} 운영 리포트",
        "",
        f"- 생성: {_md_text(generated_at)}",
        f"- 준비 상태: {'준비 완료' if readiness.get('ready') else '확인 필요'} / 막힘 {int(readiness.get('blockers') or 0)}건 / 확인 {int(readiness.get('warnings') or 0)}건",
        f"- 핵심 큐: 미제출 {int(summary.get('total_missing') or 0)}명, 데이터 확인 {int(summary.get('data_needs_review') or 0)}건, 리포트 차단 {int(summary.get('report_blocked') or 0)}건",
        "",
        "## 1. 오늘 우선순위",
    ]
    brief = hub.get("ops_brief") or []
    if brief:
        for item in brief[:8]:
            lines.append(
                "- "
                f"{_md_text(item.get('title') or '-')} "
                f"({_md_text(item.get('lane') or '운영 허브')}): "
                f"{_md_text(item.get('detail') or '')} "
                f"-> {_md_text(item.get('action_label') or '확인')}"
            )
    else:
        lines.append("- 오늘 실행할 운영 브리핑이 없습니다.")

    lines.extend(["", "## 2. 오늘 처리할 학생"])
    queue = hub.get("work_queue") or []
    if queue:
        for item in queue[:10]:
            badges = ", ".join(_md_text(badge) for badge in (item.get("badges") or [])[:4])
            badge_text = f" [{badges}]" if badges else ""
            lines.append(
                "- "
                f"{_md_text(item.get('student_name') or '미등록')} / {_md_text(item.get('class_name') or '-')} - "
                f"{_md_text(item.get('reason') or '')} "
                f"-> {_md_text(item.get('next_action') or item.get('action_label') or '확인')}"
                f"{badge_text}"
            )
            if item.get("operator_note"):
                lines.append(f"  - 상태: {_md_text(item.get('operator_note'))}")
            if item.get("safety_gate"):
                lines.append(f"  - 게이트: {_md_text(item.get('safety_gate'))}")
            if item.get("completion_check"):
                lines.append(f"  - 완료 기준: {_md_text(item.get('completion_check'))}")
    else:
        lines.append("- 바로 처리할 학생 큐가 없습니다.")

    lines.extend(
        [
            "",
            "## 3. ClassIn 데이터 상태",
            f"- 수신 원본: {int(summary.get('incoming_json') or 0)}건",
            f"- 수신함 파싱: {int(webhook_summary.get('parsed') or 0)}건 / 확인 필요 {int(webhook_summary.get('review') or 0)}건",
            f"- 이벤트 종류: {_cmd_summary_text(webhook_summary.get('by_event_label') or webhook_summary.get('by_cmd') or {})}",
            f"- 숙제 알림: 발송 필요 {int(summary.get('needs_message') or 0)}건, 재시도 {int(summary.get('needs_retry') or 0)}건, 연락처 보완 {int(summary.get('needs_phone') or 0)}건",
            "",
            "## 4. 학원 데이터 융합",
            f"- 학생 맥락: {int(context_summary.get('students_with_context') or summary.get('students_with_context') or 0)}명",
            f"- 오프라인 출결 {int(context_summary.get('offline_attendance') or summary.get('offline_attendance') or 0)}건 / 성적 {int(context_summary.get('offline_scores') or summary.get('offline_scores') or 0)}건 / 메모 {int(context_summary.get('memos') or summary.get('memos') or 0)}건",
            f"- 자동 매칭 확인 필요: {int(context_summary.get('needs_review') or summary.get('data_needs_review') or 0)}건",
        ]
    )
    review_items = academy_contexts.get("needs_review_items") or (hub.get("data_context") or {}).get("needs_review_items") or []
    for item in review_items[:5]:
        lines.append(
            "- 확인: "
            f"{_md_text(item.get('student_name') or '미매칭')} / {_md_text(item.get('class_name') or '-')} - "
            f"{_md_text(item.get('detail') or item.get('reason') or '-')}"
        )

    lines.extend(
        [
            "",
            "## 5. 개별 리포트 품질",
            f"- 드래프트: 전체 {int(weekly_summary.get('total') or summary.get('report_total') or 0)}건 / 승인 대기 {int(weekly_summary.get('pending') or summary.get('report_pending') or 0)}건",
            f"- 품질: 통과 {int(weekly_summary.get('ready_unapproved') or summary.get('report_ready') or 0)}건 / 확인 필요 {int(weekly_summary.get('review_unapproved') or summary.get('report_review') or 0)}건 / 차단 {int(weekly_summary.get('blocked_unapproved') or summary.get('report_blocked') or 0)}건",
            f"- 구성 점검: 확인 필요 {int(composition_summary.get('review') or summary.get('composition_review') or 0)}건 / 차단 {int(composition_summary.get('blocked') or summary.get('composition_blocked') or 0)}건",
            "",
            "## 6. 다음 액션 체크리스트",
        ]
    )
    actions = _ops_report_actions(hub)
    if actions:
        lines.extend(f"- [ ] {_md_text(action)}" for action in actions)
    else:
        lines.append("- [ ] 특이 큐 없음. 다음 수업 전 Webhook/알림 상태만 재확인")
    return "\n".join(lines).strip() + "\n"


def _ops_report_actions(hub: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    for item in hub.get("ops_brief") or []:
        label = item.get("action_label") or item.get("title")
        if label:
            actions.append(str(label))
    for item in hub.get("work_queue") or []:
        label = item.get("next_action") or item.get("action_label")
        student = item.get("student_name") or "미등록"
        if label:
            actions.append(f"{student}: {label}")
    seen: set[str] = set()
    unique: list[str] = []
    for action in actions:
        if action in seen:
            continue
        seen.add(action)
        unique.append(action)
    return unique[:12]


def _cmd_summary_text(by_cmd: dict[str, Any]) -> str:
    if not by_cmd:
        return "-"
    return ", ".join(f"{_md_text(cmd)} {int(count or 0)}" for cmd, count in sorted(by_cmd.items()))


def _notify_mode_text(mode: Any) -> str:
    return {
        "dry_run": "외부 발송 없음",
        "live": "실제 발송",
    }.get(str(mode or "dry_run"), _md_text(mode or "외부 발송 없음"))


def _ops_status_text(status: Any) -> str:
    return {
        "ready": "준비 완료",
        "review": "확인 필요",
        "blocked": "차단",
    }.get(str(status or ""), _md_text(status or "-"))


def _ops_risk_text(risk: Any) -> str:
    return {
        "safe": "안전",
        "review": "검토 필요",
        "dry_run": "외부 발송 없음",
        "live_guard": "실행 전 승인 필요",
    }.get(str(risk or ""), _md_text(risk or "-"))


def _ops_tab_text(tab: Any) -> str:
    return {
        "dashboard": "대시보드",
        "missing": "미제출",
        "data": "데이터",
        "reports": "리포트",
        "settings": "설정",
    }.get(str(tab or ""), _md_text(tab or "-"))


def _md_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def _hub_readiness(diagnostics: dict[str, Any] | None) -> dict[str, Any]:
    if not diagnostics:
        return {
            "ready": False,
            "blockers": 1,
            "warnings": 0,
            "services": {},
        }
    services = {
        service: _service_state(diagnostics, service)
        for service in ("ClassIn", "Notion", "Claude", "Gemini", "Aligo")
        if any(item.get("service") == service for item in diagnostics.get("items", []))
    }
    summary = diagnostics.get("summary") or {}
    return {
        "ready": bool(diagnostics.get("ready")),
        "blockers": int(summary.get("missing", 0)) + int(summary.get("failed", 0)),
        "warnings": int(summary.get("warn", 0)),
        "services": services,
    }


def _service_state(diagnostics: dict[str, Any] | None, service: str) -> str:
    if not diagnostics:
        return "unknown"
    items = [
        item
        for item in diagnostics.get("items", [])
        if item.get("service") == service
    ]
    if not items:
        return "unknown"
    if any(item.get("status") in ("missing", "failed") for item in items):
        return "blocked"
    if any(item.get("status") == "warn" for item in items):
        return "warn"
    return "ready"


def _hub_lanes(
    status: dict[str, Any],
    diagnostics: dict[str, Any] | None,
    summary: dict[str, int],
    missing_error: str | None,
) -> list[dict[str, Any]]:
    counts = status.get("counts") or {}
    classin_state = _service_state(diagnostics, "ClassIn")
    notion_state = _service_state(diagnostics, "Notion")
    report_state = "blocked" if notion_state == "blocked" else "ready"
    if summary["report_blocked"] or summary["composition_blocked"]:
        report_state = "blocked"
    elif (
        summary["report_pending"]
        or summary["composition_review"]
        or (summary["weekly_indexes"] and summary["weekly_html"])
    ):
        report_state = "warn"
    return [
        {
            "id": "api_push",
            "title": "ClassIn 생성 작업",
            "label": "수업·숙제·OMR 생성",
            "state": classin_state,
            "metric": "준비" if classin_state != "blocked" else "설정 필요",
            "count": 0,
            "next_action": "스케줄 검토",
        },
        {
            "id": "data_subscription",
            "title": "ClassIn 수신 데이터",
            "label": "출결·숙제·시험 수신",
            "state": "warn" if missing_error else ("ready" if notion_state != "blocked" else "blocked"),
            "metric": f"원본 {counts.get('incoming_json', 0)}건",
            "count": summary["total_missing"],
            "next_action": "미제출 확인",
        },
        {
            "id": "academy_data",
            "title": "학원 데이터 융합",
            "label": "로컬 출결·성적·상담 메모",
            "state": "warn" if summary["data_needs_review"] else ("ready" if notion_state != "blocked" else "blocked"),
            "metric": f"맥락 {summary['students_with_context']}명",
            "count": summary["data_needs_review"],
            "next_action": "확인 필요",
        },
        {
            "id": "individual_reports",
            "title": "개별 리포트",
            "label": "학생별 주간 드래프트·승인",
            "state": report_state,
            "metric": f"대기 {summary['report_pending']}건",
            "count": (
                summary["report_blocked"]
                or summary["composition_blocked"]
                or summary["report_review"]
                or summary["composition_review"]
                or summary["report_ready"]
            ),
            "next_action": "품질 검토",
        },
    ]


def _hub_focus_items(
    status: dict[str, Any],
    readiness: dict[str, Any],
    summary: dict[str, int],
    missing_error: str | None,
) -> list[dict[str, Any]]:
    focus: list[dict[str, Any]] = []
    if not status.get("ok"):
        focus.append(
            {
                "id": "config",
                "label": "설정 필요",
                "count": 1,
                "tone": "danger",
                "detail": "config.yaml",
                "tab": "settings",
            }
        )
    elif readiness["blockers"]:
        focus.append(
            {
                "id": "diagnostics",
                "label": "연결 점검",
                "count": readiness["blockers"],
                "tone": "danger",
                "detail": "외부 서비스",
                "tab": "dashboard",
            }
        )
    if missing_error:
        focus.append(
            {
                "id": "missing_error",
                "label": "조회 실패",
                "count": 1,
                "tone": "danger",
                "detail": missing_error,
                "tab": "missing",
            }
        )
    for item in (
        ("needs_message", "연락 필요", "warn", "학부모 알림", "missing"),
        ("needs_retry", "재발송 필요", "danger", "발송 실패", "missing"),
        ("needs_phone", "연락처 보완", "danger", "학생 정보", "missing"),
        ("report_blocked", "리포트 수정", "danger", "품질 차단", "reports"),
        ("composition_blocked", "구성 보강", "danger", "근거 부족", "reports"),
        ("report_review", "리포트 검토", "warn", "AI 품질", "reports"),
        ("data_needs_review", "데이터 확인", "warn", "자동 매칭", "data"),
        ("composition_review", "구성 보강", "warn", "리포트 점검", "reports"),
        ("report_ready", "승인 가능", "info", "주간 리포트", "reports"),
        ("weekly_indexes", "드래프트 있음", "info", "주간 리포트", "reports"),
    ):
        key, label, tone, detail, tab = item
        count = int(summary.get(key, 0))
        if count:
            focus.append(
                {
                    "id": key,
                    "label": label,
                    "count": count,
                    "tone": tone,
                    "detail": detail,
                    "tab": tab,
                }
            )
    if not focus:
        focus.append(
            {
                "id": "steady",
                "label": "운영 안정",
                "count": 0,
                "tone": "ok",
                "detail": "처리 큐 없음",
                "tab": "dashboard",
            }
        )
    return focus[:6]


def _hub_ops_brief(
    *,
    status: dict[str, Any],
    readiness: dict[str, Any],
    summary: dict[str, int],
    lanes: list[dict[str, Any]],
    missing_error: str | None,
) -> list[dict[str, Any]]:
    brief: list[dict[str, Any]] = []
    if not status.get("ok"):
        brief.append(
            _brief_item(
                "config",
                "danger",
                "설정부터 완료",
                "config.yaml을 읽지 못해 자동화 판단을 멈췄습니다.",
                "설정 열기",
                "settings",
                "운영 허브",
                "config.yaml",
                1,
            )
        )
    elif readiness["blockers"]:
        brief.append(
            _brief_item(
                "readiness",
                "danger",
                "외부 연결 점검",
                f"ClassIn/Notion/LLM/알림 설정 중 {readiness['blockers']}개가 막혀 있습니다.",
                "설정 점검",
                "dashboard",
                "운영 허브",
                "비파괴 API 진단",
                1,
            )
        )
    if missing_error:
        brief.append(
            _brief_item(
                "missing_error",
                "danger",
                "데이터 구독 조회 실패",
                missing_error,
                "미제출 확인",
                "data",
                "수신 데이터",
                "미제출 조회",
                2,
            )
        )
    for key, title, detail, action, tab, lane, priority, tone in (
        (
            "needs_retry",
            "발송 실패 재시도",
            "알림 전송 실패 학생이 있습니다. 연락 전 로그와 번호를 확인하세요.",
            "미제출 확인",
            "missing",
                "수신 데이터",
            3,
            "danger",
        ),
        (
            "needs_phone",
            "보호자 연락처 보완",
            "연락처가 없어 자동 알림을 진행할 수 없는 학생이 있습니다.",
            "학생 큐 확인",
            "missing",
            "학원 데이터 융합",
            4,
            "danger",
        ),
        (
            "report_blocked",
            "차단 리포트 수정",
            "AI 품질 점검에서 막힌 드래프트가 있습니다. 근거·표현·다음 액션을 보강하세요.",
            "리포트 검토",
            "reports",
            "개별 리포트",
            5,
            "danger",
        ),
        (
            "report_review",
            "확인 필요 리포트 검토",
            "교사 확인이 필요한 리포트 드래프트가 있습니다.",
            "리포트 검토",
            "reports",
            "개별 리포트",
            6,
            "warn",
        ),
        (
            "composition_blocked",
            "리포트 구성 근거 보강",
            "드래프트 생성 전 근거가 부족한 학생이 있습니다. 수업·시험·메모 데이터를 먼저 보강하세요.",
            "구성 확인",
            "reports",
            "개별 리포트",
            6,
            "danger",
        ),
        (
            "composition_review",
            "리포트 구성 확인",
            "출결·숙제·시험·메모 섹션 중 교사 확인이 필요한 학생이 있습니다.",
            "구성 확인",
            "reports",
            "개별 리포트",
            7,
            "warn",
        ),
        (
            "needs_message",
            "미제출 알림 발송",
            "보호자 연락처가 있는 미제출 학생에게 발송 전 검토 문구를 만들 수 있습니다.",
            "문자 발송",
            "missing",
            "수신 데이터",
            8,
            "warn",
        ),
        (
            "data_needs_review",
            "학원 데이터 매칭 확인",
            "오프라인 성적·출결·메모 중 자동 매칭이 애매한 자료가 있습니다.",
            "데이터 확인",
            "data",
            "학원 데이터 융합",
            6,
            "warn",
        ),
        (
            "report_ready",
            "통과 리포트 승인",
            "품질 점검을 통과한 주간 드래프트를 승인 대기 중입니다.",
            "승인하기",
            "reports",
            "개별 리포트",
            10,
            "info",
        ),
    ):
        count = int(summary.get(key, 0))
        if count:
            brief.append(
                _brief_item(
                    key,
                    tone,
                    title,
                    f"{detail} 대상 {count}건.",
                    action,
                    tab,
                    lane,
                    f"대상 {count}건",
                    priority,
                    count=count,
                )
            )

    if not brief:
        brief.append(
            _brief_item(
                "steady",
                "ok",
                "오늘 자동화 큐 안정",
                "즉시 처리할 알림·데이터·리포트 위험 신호가 없습니다.",
                "허브 보기",
                "dashboard",
                "운영 허브",
                "운영 축 정상",
                99,
            )
        )
    brief.sort(key=lambda item: (item["priority"], item["title"]))
    return brief[:5]


def _brief_item(
    item_id: str,
    tone: str,
    title: str,
    detail: str,
    action_label: str,
    tab: str,
    lane: str,
    evidence: str,
    priority: int,
    *,
    count: int = 0,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "tone": tone,
        "title": title,
        "detail": detail,
        "action_label": action_label,
        "tab": tab,
        "lane": lane,
        "evidence": evidence,
        "priority": priority,
        "count": count,
    }


def _hub_work_queue(
    missing_items: list[dict[str, Any]],
    review_items: list[dict[str, Any]],
    weekly_draft_items: list[dict[str, Any]],
    report_composition_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return build_teacher_action_queue(
        missing_items=missing_items,
        data_review_items=review_items,
        weekly_draft_items=weekly_draft_items,
        report_composition_items=report_composition_items,
        limit=12,
    )


def _demo_missing_homework_payload() -> dict[str, Any]:
    contexts = _demo_report_contexts()
    return _missing_homework_payload(
        _demo_missing_rows(),
        _demo_notification_history(),
        report_contexts=contexts,
        data_context={
            "summary": {
                "students_with_context": sum(
                    1 for context in contexts.values() if context["has_context"]
                ),
                "weekly_reports": sum(
                    1 for context in contexts.values() if context["weekly_report"]["status"]
                ),
                "offline_attendance": sum(
                    context["offline_attendance"] for context in contexts.values()
                ),
                "offline_scores": sum(context["offline_scores"] for context in contexts.values()),
                "memos": sum(context["memos"] for context in contexts.values()),
                "needs_review": 1,
            },
            "needs_review_items": [
                {
                    "kind": "offline_score",
                    "student_name": "동명이인",
                    "class_name": "고2-A",
                    "source": "demo://local_data/inbox/scores/april.xlsx",
                    "reason": "학생 자동 매칭 필요",
                }
            ],
        },
    )


def _demo_missing_rows() -> list[dict[str, Any]]:
    return build_demo_missing_homework_rows(base_date=date_cls.today(), weeks=3)


def _demo_lesson_rows(*, start_date: date_cls, days: int) -> list[dict[str, Any]]:
    rows = build_demo_lesson_records(base_date=start_date, weeks=3)
    end_date = start_date + timedelta(days=days)
    return [
        row
        for row in rows
        if start_date <= datetime.fromisoformat(row["date"]).date() < end_date
    ]


def _demo_report_target_items() -> list[dict[str, Any]]:
    return [
        {
            "student_classin_id": student.classin_id,
            "student_name": student.name,
            "class_name": student.class_name,
            "has_parent_phone": bool(student.parent_phone),
        }
        for student in DEMO_STUDENTS
    ]


def _demo_weekly_drafts_payload(period_start: datetime) -> dict[str, Any]:
    period_end = period_start + timedelta(days=6, hours=23, minutes=59)
    profiles = [
        ("10001", "ready", 92, [], False, "출석·숙제 루틴 안정"),
        ("10002", "review", 73, ["지각 반복 원인 확인 필요"], False, "지각 2회 · 상담 메모 1건"),
        ("10003", "blocked", 41, ["다음 액션 부족", "성적 하락 근거 보강 필요"], False, "성적 하락 · 상담 메모 2건"),
        ("10004", "ready", 88, [], False, "심화 과제 추천"),
        ("10005", "ready", 90, [], True, "결석 상담 후 승인 완료"),
    ]
    student_by_id = {student.classin_id: student for student in DEMO_STUDENTS}
    items: list[dict[str, Any]] = []
    for student_id, status, score, warnings, approved, context in profiles:
        student = student_by_id[student_id]
        filename = f"{period_start.date()}_{student.name}.html"
        items.append(
            {
                "student_classin_id": student.classin_id,
                "student_name": student.name,
                "html_path": f"demo://weekly/{filename}",
                "public_url": "",
                "preview_url": None,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "summary_markdown": "",
                "parent_message": "",
                "report_context_summary": context,
                "quality_status": status,
                "quality_score": score,
                "quality_warnings": warnings,
                "approved": approved,
                "notion_page_id": "demo-page-5" if approved else None,
            }
        )
    items.sort(
        key=lambda item: (
            item["approved"],
            {"blocked": 0, "review": 1, "ready": 2}.get(item["quality_status"], 3),
            item["student_name"],
        )
    )
    return {
        "ok": True,
        "demo": True,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "index_path": f"demo://weekly/{period_start.date()}_drafts.json",
        "exists": True,
        "summary": _weekly_draft_summary_from_items(items),
        "items": items,
    }


def _demo_report_compositions_payload(
    period_start: datetime,
    *,
    class_name: str | None,
) -> dict[str, Any]:
    period_end = period_start + timedelta(days=6, hours=23, minutes=59)
    students = [student for student in DEMO_STUDENTS if not class_name or student.class_name == class_name]
    contexts = _demo_report_contexts()
    notifications = _latest_notification_by_student(_demo_notification_history())
    lessons_by_student: dict[str, list[dict[str, Any]]] = {}
    for row in build_demo_lesson_records(base_date=period_start.date(), weeks=1):
        if not (period_start.date() <= datetime.fromisoformat(row["date"]).date() <= period_end.date()):
            continue
        lessons_by_student.setdefault(str(row["student_classin_id"]), []).append(row)
    exams_by_student: dict[str, list[dict[str, Any]]] = {
        "10001": [{"exam_name": "단원평가", "subject": "수학", "score": 92, "max_score": 100}],
        "10002": [{"exam_name": "단원평가", "subject": "수학", "score": 68, "max_score": 100}],
        "10003": [{"exam_name": "내신 대비", "subject": "수학", "score": 62, "max_score": 100}],
    }
    items = [
        compose_individual_report(
            student=student,
            lessons=lessons_by_student.get(student.classin_id, []),
            exam_results=exams_by_student.get(student.classin_id, []),
            academy_context=contexts.get(student.classin_id),
            latest_notification=notifications.get(student.classin_id),
        ).as_dict()
        for student in students
    ]
    items.sort(
        key=lambda item: (
            _readiness_priority(item["readiness_status"]),
            item["class_name"],
            item["student_name"],
            item["student_classin_id"],
        )
    )
    classes = sorted({student.class_name for student in DEMO_STUDENTS if student.class_name})
    return {
        "ok": True,
        "demo": True,
        "class_name": class_name,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "summary": _report_composition_summary(items),
        "classes": classes,
        "data_context": {
            "summary": {
                "students_with_context": sum(
                    1 for context in contexts.values() if context["has_context"]
                ),
                "weekly_reports": sum(
                    1 for context in contexts.values() if context["weekly_report"]["status"]
                ),
                "offline_attendance": sum(
                    context["offline_attendance"] for context in contexts.values()
                ),
                "offline_scores": sum(context["offline_scores"] for context in contexts.values()),
                "memos": sum(context["memos"] for context in contexts.values()),
                "needs_review": 1,
            },
            "needs_review_items": [
                {
                    "kind": "offline_score",
                    "student_name": "동명이인",
                    "class_name": "고2-A",
                    "source": "demo://local_data/inbox/scores/april.xlsx",
                    "reason": "학생 자동 매칭 필요",
                }
            ],
        },
        "items": items,
    }


def _weekly_draft_summary_from_items(items: list[dict[str, Any]]) -> dict[str, int]:
    total = len(items)
    approved = sum(1 for item in items if item.get("approved"))
    return {
        "total": total,
        "approved": approved,
        "pending": total - approved,
        "ready": sum(1 for item in items if item.get("quality_status") == "ready"),
        "review": sum(1 for item in items if item.get("quality_status") == "review"),
        "blocked": sum(1 for item in items if item.get("quality_status") == "blocked"),
        "ready_unapproved": sum(
            1
            for item in items
            if not item.get("approved") and item.get("quality_status") == "ready"
        ),
        "review_unapproved": sum(
            1
            for item in items
            if not item.get("approved") and item.get("quality_status") == "review"
        ),
        "blocked_unapproved": sum(
            1
            for item in items
            if not item.get("approved") and item.get("quality_status") == "blocked"
        ),
        "with_context": sum(1 for item in items if item.get("report_context_summary")),
        "with_public_url": sum(1 for item in items if item.get("public_url")),
    }


def _demo_notification_history() -> list[dict[str, Any]]:
    return build_demo_notification_history(_demo_missing_rows())


def _demo_report_contexts() -> dict[str, dict[str, Any]]:
    contexts = {
        "10002": _empty_report_context(),
        "10003": _empty_report_context(),
        "10005": _empty_report_context(),
    }
    contexts["10002"].update(
        {
            "has_context": True,
            "weekly_report": {
                "status": "draft_ready",
                "period_start": "2026-04-20T00:00:00+09:00",
                "period_end": "2026-04-26T23:59:00+09:00",
                "html_path": "demo://weekly/김지각.html",
                "public_url": "",
                "approved": False,
            },
            "offline_attendance": 1,
            "offline_scores": 1,
            "memos": 1,
            "badges": ["리포트 초안", "오프라인 시험 1건", "상담 메모 1건"],
            "summary": "리포트 초안 · 오프라인 시험 1건 · 상담 메모 1건",
            "sources": [
                {"kind": "weekly_report", "source": "demo://weekly", "detail": "초안"},
                {"kind": "offline_score", "source": "demo://scores", "detail": "단원평가 68점"},
                {"kind": "memo", "source": "demo://memos", "detail": "등원 시간 상담"},
            ],
        }
    )
    contexts["10003"].update(
        {
            "has_context": True,
            "weekly_report": {
                "status": "draft_ready",
                "period_start": "2026-04-20T00:00:00+09:00",
                "period_end": "2026-04-26T23:59:00+09:00",
                "html_path": "demo://weekly/이하락.html",
                "public_url": "",
                "approved": False,
            },
            "offline_attendance": 0,
            "offline_scores": 1,
            "memos": 2,
            "badges": ["리포트 초안", "오프라인 시험 1건", "상담 메모 2건"],
            "summary": "리포트 초안 · 오프라인 시험 1건 · 상담 메모 2건",
            "sources": [
                {"kind": "weekly_report", "source": "demo://weekly", "detail": "초안"},
                {"kind": "offline_score", "source": "demo://scores", "detail": "내신 대비 62점"},
                {"kind": "memo", "source": "demo://memos", "detail": "집중도 하락 상담"},
            ],
        }
    )
    contexts["10005"].update(
        {
            "has_context": True,
            "weekly_report": {
                "status": "approved",
                "period_start": "2026-04-20T00:00:00+09:00",
                "period_end": "2026-04-26T23:59:00+09:00",
                "html_path": "demo://weekly/최결석.html",
                "public_url": "",
                "approved": True,
            },
            "offline_attendance": 1,
            "offline_scores": 0,
            "memos": 1,
            "badges": ["리포트 승인됨", "오프라인 출결 1건", "상담 메모 1건"],
            "summary": "리포트 승인됨 · 오프라인 출결 1건 · 상담 메모 1건",
            "sources": [
                {"kind": "weekly_report", "source": "demo://weekly", "detail": "승인됨"},
                {"kind": "offline_attendance", "source": "demo://attendance", "detail": "보강 결석"},
                {"kind": "memo", "source": "demo://memos", "detail": "보호자 연락처 확인 필요"},
            ],
        }
    )
    return contexts


def _render_shell(status: dict[str, Any]) -> str:
    status_json = _json_for_script(status)
    today = date_cls.today().isoformat()
    week_start = _week_start(date_cls.today()).isoformat()
    academy_name = status.get("academy") or "ClassIn Toolkit"
    title = html.escape(academy_name)
    brand_initial = html.escape((academy_name.strip() or "C")[:1])
    notify_mode_raw = (status.get("output") or {}).get("notify_mode") or "dry_run"
    notify_mode = html.escape(notify_mode_raw)
    notify_mode_class = "sent" if notify_mode_raw == "live" else "dry_run"
    return f"""<!doctype html>
<html lang="ko" data-theme="light">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ClassIn 운영 콘솔 · {title}</title>
  <link rel="icon" href="data:,">
  <link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable.min.css">
  <style>
    :root {{
      color-scheme: light;
      --primary: #4f8f56;
      --primary-d: #326f3c;
      --primary-soft: #e8f5e6;
      --primary-softer: #f5fbf3;

      --bg: #f7faf4;
      --panel: #ffffff;
      --surface-2: #fbfdf8;
      --line: #e4ecdf;
      --line-strong: #cfdcc8;
      --text: #1b251d;
      --muted: #59665b;
      --text-3: #8a9788;

      --ok: #1f8a5b;
      --ok-soft: #e6f4ed;
      --accent: #c2790c;
      --accent-soft: #fdf3e2;
      --danger: #d23f3f;
      --danger-soft: #fbeaea;
      --info: #477f8a;
      --info-soft: #e7f3f5;

      --radius: 8px;
      --radius-sm: 7px;
      --shadow-sm: 0 1px 2px rgba(20,30,50,.05), 0 1px 1px rgba(20,30,50,.04);
      --shadow-md: 0 4px 16px rgba(20,30,50,.08), 0 1px 3px rgba(20,30,50,.05);
      --shadow-lg: 0 20px 50px rgba(15,25,45,.22), 0 6px 16px rgba(15,25,45,.12);

      --sidebar-w: 202px;
      --font: "Pretendard Variable", Pretendard, -apple-system, "Apple SD Gothic Neo", system-ui, sans-serif;
      --mono: "SF Mono", ui-monospace, Menlo, Consolas, monospace;
    }}
    [data-theme="dark"] {{
      color-scheme: dark;
      --bg: #10170f;
      --panel: #182018;
      --surface-2: #131b13;
      --line: #2a3828;
      --line-strong: #3a4d37;
      --text: #edf4ea;
      --muted: #aab7a7;
      --text-3: #74806f;
      --primary: #8bcf7d;
      --primary-d: #6eaf64;
      --primary-soft: #20351f;
      --primary-softer: #172617;
      --ok-soft: #142a22;
      --accent-soft: #2e2414;
      --danger-soft: #2e1a1c;
      --info-soft: #173036;
      --shadow-sm: 0 1px 2px rgba(0,0,0,.3);
      --shadow-md: 0 4px 16px rgba(0,0,0,.4);
      --shadow-lg: 0 24px 60px rgba(0,0,0,.6);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-width: 0;
      background: var(--bg);
      color: var(--text);
      font-family: var(--font);
      font-size: 14px;
      line-height: 1.5;
      letter-spacing: 0;
      -webkit-font-smoothing: antialiased;
    }}
    h1, h2 {{ letter-spacing: 0; }}
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
      gap: 9px;
      height: 56px;
      padding: 0 14px;
      border-bottom: 1px solid var(--line);
      flex-shrink: 0;
    }}
    .brand-mark {{
      display: grid;
      place-items: center;
      width: 28px;
      height: 28px;
      border-radius: 8px;
      background: linear-gradient(135deg, var(--primary), var(--primary-d));
      color: #fff;
      font-weight: 800;
      font-size: 14px;
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
      font-size: 13px;
      font-weight: 700;
    }}
    .brand span {{
      display: block;
      margin-top: 1px;
      color: var(--text-3);
      font-size: 10.5px;
      white-space: nowrap;
    }}
    .sidebar .tabs {{
      display: flex;
      flex-direction: column;
      gap: 0;
      flex: 1;
      margin: 0;
      padding: 7px;
      overflow-y: auto;
      border: 0;
      background: transparent;
      box-shadow: none;
    }}
    .nav-sep {{
      padding: 9px 9px 3px;
      color: var(--text-3);
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0;
    }}
    .sidebar .tab-button {{
      display: grid;
      grid-template-columns: 20px minmax(0, 1fr);
      align-items: center;
      justify-content: stretch;
      justify-items: start;
      column-gap: 8px;
      width: 100%;
      min-height: 0;
      padding: 7px 9px;
      border: none;
      border-radius: var(--radius-sm);
      background: none;
      color: var(--muted);
      font-size: 12.8px;
      font-weight: 550;
      text-align: left;
      white-space: nowrap;
    }}
    .sidebar .tab-button .ic {{ width: 18px; height: 18px; justify-self: center; flex-shrink: 0; }}
    .sidebar .tab-button .label {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; }}
    .sidebar .tab-button:hover {{ background: var(--surface-2); color: var(--text); }}
    .sidebar .tab-button.active {{
      background: var(--primary-soft);
      color: var(--primary);
      font-weight: 650;
      box-shadow: inset 3px 0 0 var(--primary);
    }}
    [data-theme="dark"] .sidebar .tab-button.active {{ color: var(--primary); }}
    .sidebar-footer {{
      flex-shrink: 0;
      margin: 0;
      padding: 8px;
      border-top: 1px solid var(--line);
    }}
    .server-pill {{
      display: flex;
      align-items: center;
      gap: 7px;
      padding: 6px 8px;
      border-radius: var(--radius-sm);
      background: var(--surface-2);
      color: var(--muted);
      font-size: 11.5px;
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
      height: 56px;
      padding: 0 20px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
    }}
    .topbar-title {{ display: flex; align-items: baseline; gap: 10px; min-width: 0; }}
    .topbar-title h2 {{ margin: 0; font-size: 16px; font-weight: 700; }}
    .topbar-title span {{ color: var(--text-3); font-size: 13px; }}
    .topbar .inline-actions {{ margin-left: auto; min-width: 0; }}
    .quick-run-button svg {{ width: 15px; height: 15px; }}
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
    .topbar-user {{
      display: flex;
      align-items: center;
      gap: 7px;
      min-height: 34px;
      padding: 2px 9px 2px 3px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--surface-1);
      color: var(--text);
      white-space: nowrap;
    }}
    .topbar-user .avatar {{
      display: grid;
      place-items: center;
      width: 28px;
      height: 28px;
      border-radius: 7px;
      background: var(--primary);
      color: #fff;
      font-weight: 700;
      font-size: 12px;
    }}
    .topbar-user .rl {{ font-size: 12.5px; font-weight: 650; }}
    .workspace main {{
      flex: 1;
      width: auto;
      margin: 0;
      padding: 20px 22px 28px;
    }}

    /* ── Metrics ─────────────────────────── */
    .status-strip {{
      display: flex;
      gap: 0;
      margin: -20px -22px 18px;
      padding: 0 20px;
      height: 58px;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      overflow-x: auto;
    }}
    .metric {{
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 2px;
      min-width: 108px;
      padding: 0 18px;
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
      font-size: 19px;
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

    .quick-run-overlay {{
      position: fixed;
      inset: 0;
      z-index: 120;
      display: grid;
      place-items: start center;
      padding: 72px 18px 18px;
      background: rgba(15, 23, 42, .36);
      backdrop-filter: blur(8px);
    }}
    .quick-run-overlay[hidden] {{ display: none !important; }}
    .quick-run-panel {{
      width: min(720px, 100%);
      max-height: min(720px, calc(100vh - 104px));
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--panel);
      box-shadow: var(--shadow-lg);
    }}
    .quick-run-head {{
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 14px;
      border-bottom: 1px solid var(--line);
    }}
    .quick-run-head h2 {{
      flex-shrink: 0;
      margin: 0;
      font-size: 15px;
      font-weight: 760;
    }}
    .quick-run-search {{
      flex: 1 1 auto;
      min-width: 0;
      width: 100%;
      height: 42px;
      padding: 0 14px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface-2);
      color: var(--text);
      font: inherit;
      font-weight: 620;
    }}
    .quick-run-list {{
      display: grid;
      gap: 7px;
      max-height: calc(100vh - 230px);
      overflow: auto;
      padding: 10px;
    }}
    .quick-command-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
      width: 100%;
      min-height: 62px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface);
      color: var(--text);
      text-align: left;
    }}
    .quick-command-row:hover,
    .quick-command-row.active {{
      border-color: rgba(79,143,86,.45);
      background: var(--primary-softer);
    }}
    .quick-command-row strong {{
      display: block;
      font-size: 13.5px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }}
    .quick-command-row span:not(.badge) {{
      display: block;
      margin-top: 3px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .quick-command-row .badge {{
      align-self: center;
      white-space: nowrap;
    }}
    .student-brief-overlay {{
      position: fixed;
      inset: 0;
      z-index: 110;
      display: grid;
      justify-content: end;
      background: rgba(15, 23, 42, .28);
      backdrop-filter: blur(7px);
    }}
    .student-brief-overlay[hidden] {{ display: none !important; }}
    .student-brief-panel {{
      width: min(560px, 100vw);
      height: 100vh;
      overflow: auto;
      border-left: 1px solid var(--line);
      background: var(--panel);
      box-shadow: var(--shadow-lg);
    }}
    .student-brief-head {{
      position: sticky;
      top: 0;
      z-index: 1;
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      padding: 18px;
      border-bottom: 1px solid var(--line);
      background: color-mix(in srgb, var(--panel) 94%, transparent);
      backdrop-filter: blur(12px);
    }}
    .student-brief-head h2 {{
      margin: 0;
      font-size: 18px;
      line-height: 1.25;
    }}
    .student-brief-head span {{
      display: block;
      margin-top: 4px;
      color: var(--muted);
      font-size: 12.5px;
    }}
    .student-brief-body {{
      display: grid;
      gap: 12px;
      padding: 14px 18px 20px;
    }}
    .student-brief-hero {{
      padding: 14px;
      border: 1px solid var(--line);
      border-left: 4px solid var(--primary);
      border-radius: var(--radius);
      background: var(--surface);
    }}
    .student-brief-hero strong {{
      display: block;
      font-size: 15px;
      line-height: 1.3;
    }}
    .student-brief-hero p {{
      margin: 7px 0 0;
      color: var(--muted);
      font-size: 12.5px;
      line-height: 1.45;
    }}
    .student-brief-actions,
    .student-brief-meta,
    .student-brief-badges {{
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
      margin-top: 10px;
    }}
    .student-brief-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .student-brief-card {{
      min-width: 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface-2);
    }}
    .student-brief-card span {{
      display: block;
      color: var(--text-3);
      font-size: 11px;
      font-weight: 700;
    }}
    .student-brief-card strong {{
      display: block;
      margin-top: 5px;
      font-size: 18px;
      line-height: 1.2;
    }}
    .student-brief-section {{
      padding: 13px 14px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--panel);
    }}
    .student-brief-section h3 {{
      margin: 0 0 9px;
      font-size: 13.5px;
      line-height: 1.3;
    }}
    .student-brief-list {{
      display: grid;
      gap: 8px;
      margin: 0;
      padding: 0;
      list-style: none;
    }}
    .student-brief-list li {{
      padding: 9px 10px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface);
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }}
    .student-brief-list strong {{
      display: block;
      color: var(--text);
      font-size: 12.5px;
      line-height: 1.35;
    }}
    .metric.warn strong, .metric.alert strong, .metric.danger strong,
    .metric.info strong, .metric.ok strong {{ color: var(--text); }}

    /* ── Tabs / panels ─────────────────────────── */
    .tab-panel {{ display: none; }}
    .tab-panel.active {{ display: block; }}

    .panel {{
      min-width: 0;
      padding: var(--pad, 18px);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow-sm);
    }}
    .panel + .panel {{ margin-top: 14px; }}
    h2 {{ margin: 0 0 12px; font-size: 14.5px; font-weight: 700; line-height: 1.2; }}
    .panel-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }}
    .panel-head h2 {{ margin: 0; }}
    .grid {{
      display: grid;
      grid-template-columns: 1.2fr .8fr;
      gap: 14px;
      align-items: start;
    }}
    .grid > *,
    .stack > *,
    .action-layout > *,
    .action-controls > * {{
      min-width: 0;
    }}

    /* ── Buttons ─────────────────────────── */
    button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
      min-height: 36px;
      padding: 0 14px;
      border: 1px solid transparent;
      border-radius: var(--radius-sm);
      background: var(--primary);
      color: #fff;
      font: inherit;
      font-size: 13px;
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
    .action-layout {{ display: grid; gap: 14px; align-items: start; }}
    .core-panel {{ padding: var(--pad, 18px); }}
    .core-panel .panel-head {{ margin-bottom: 12px; }}
    .core-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }}
    .core-button {{
      display: grid;
      grid-template-columns: 38px minmax(0, 1fr);
      grid-template-rows: auto auto;
      gap: 3px 11px;
      align-content: start;
      min-height: 84px;
      padding: 15px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--panel);
      color: var(--text);
      text-align: left;
      white-space: normal;
      box-shadow: var(--shadow-sm);
      transition: border-color .14s, box-shadow .14s, transform .14s;
    }}
    .core-button:hover {{ background: var(--panel); filter: none; border-color: var(--primary); box-shadow: var(--shadow-md); transform: translateY(-2px); }}
    .core-button.active {{
      border-color: rgba(79,143,86,.55);
      background: var(--primary-softer);
      box-shadow: inset 0 0 0 1px rgba(79,143,86,.12), var(--shadow-sm);
    }}
    .core-button strong {{ display: block; min-width: 0; overflow-wrap: anywhere; font-size: 15px; font-weight: 700; line-height: 1.3; }}
    .core-button span {{ display: block; min-width: 0; color: var(--muted); font-size: 12.5px; line-height: 1.45; font-weight: 500; overflow-wrap: anywhere; }}
    .core-button .core-number {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      grid-row: 1 / span 2;
      width: 38px;
      height: 38px;
      border-radius: 8px;
      background: var(--primary);
      color: #fff;
      font-size: 15px;
      font-weight: 800;
      line-height: 1;
    }}

    .action-command {{
      display: grid;
      grid-template-columns: minmax(0, 1.08fr) minmax(300px, .92fr);
      gap: 11px;
      padding: 12px 13px;
      border: 1px solid var(--line);
      border-left: 4px solid var(--primary);
      border-radius: var(--radius);
      background: var(--panel);
      box-shadow: var(--shadow-sm);
    }}
    .action-command-main,
    .action-command-side {{
      min-width: 0;
    }}
    .action-command strong {{
      display: block;
      color: var(--text);
      font-size: 15px;
      font-weight: 780;
      line-height: 1.22;
      overflow-wrap: anywhere;
    }}
    .action-command p {{
      margin: 3px 0 0;
      color: var(--muted);
      font-size: 12.5px;
      line-height: 1.34;
      overflow-wrap: anywhere;
    }}
    .action-command-main > p {{
      display: -webkit-box;
      overflow: hidden;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 1;
    }}
    .action-command .command-meta {{
      flex-wrap: nowrap;
      gap: 4px;
      margin-top: 8px;
      overflow-x: auto;
      padding-bottom: 2px;
      scrollbar-width: none;
    }}
    .action-command .command-meta::-webkit-scrollbar {{
      display: none;
    }}
    .action-command .command-meta .badge {{
      flex: 0 0 auto;
    }}
    .action-command-actions {{
      display: flex;
      gap: 6px;
      flex-wrap: nowrap;
      margin-top: 7px;
      overflow-x: auto;
      padding-bottom: 2px;
      scrollbar-width: none;
    }}
    .action-command-actions::-webkit-scrollbar {{
      display: none;
    }}
    .action-command-actions button {{
      flex: 0 0 auto;
      min-height: 30px;
      padding-inline: 10px;
      font-size: 12px;
    }}
    .workflow-steps {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(116px, 1fr));
      gap: 6px;
    }}
    .workflow-step {{
      min-width: 0;
      padding: 6px 7px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface-2);
    }}
    .workflow-step span {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 19px;
      height: 19px;
      margin-bottom: 4px;
      border-radius: 6px;
      background: var(--panel);
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 10.5px;
      font-weight: 750;
    }}
    .workflow-step strong {{
      font-size: 12px;
      font-weight: 720;
      line-height: 1.22;
    }}
    .workflow-step p {{
      display: -webkit-box;
      margin-top: 2px;
      overflow: hidden;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 1;
      font-size: 11.2px;
      line-height: 1.25;
    }}
    .workflow-step.active {{
      border-color: rgba(79,143,86,.42);
      background: var(--primary-softer);
    }}
    .workflow-step.active span {{
      background: var(--primary);
      border-color: var(--primary);
      color: #fff;
    }}
    .action-command .command-gate {{
      gap: 6px;
      margin-top: 7px;
      padding-top: 7px;
    }}
    .action-command .command-gate p {{
      display: -webkit-box;
      overflow: hidden;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 1;
      line-height: 1.35;
    }}
    .missing-command,
    .schedule-command,
    .report-command,
    .data-command,
    .agent-command,
    .settings-command,
    .exam-command {{
      margin-bottom: 16px;
      border-left-color: var(--ok);
    }}
    .missing-command.warn,
    .schedule-command.warn,
    .report-command.warn,
    .data-command.warn,
    .agent-command.warn,
    .settings-command.warn,
    .exam-command.warn {{
      border-left-color: var(--accent);
    }}
    .missing-command.danger,
    .schedule-command.danger,
    .report-command.danger,
    .data-command.danger,
    .agent-command.danger,
    .settings-command.danger,
    .exam-command.danger {{
      border-left-color: var(--danger);
    }}
    .missing-command .workflow-steps,
    .schedule-command .workflow-steps,
    .report-command .workflow-steps,
    .data-command .workflow-steps,
    .agent-command .workflow-steps,
    .settings-command .workflow-steps,
    .exam-command .workflow-steps {{
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }}
    .agent-prompt-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }}
    .agent-prompt-grid button {{
      justify-content: flex-start;
      min-height: 46px;
      white-space: normal;
      text-align: left;
      line-height: 1.3;
    }}

    .action-controls {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 16px;
      align-items: start;
    }}
    .action-controls .panel {{ margin-top: 0; }}
    .missing-action-panel {{
      --pad: 16px;
      grid-column: span 2;
    }}
    .missing-action-panel .stack {{
      gap: 8px;
      grid-template-columns: minmax(0, 1fr) minmax(250px, .9fr);
    }}
    .missing-action-panel .range-picker {{
      gap: 7px;
    }}
    .missing-action-panel input,
    .missing-action-panel select {{
      min-height: 32px;
      padding: 7px 10px;
      font-size: 12.5px;
    }}
    .missing-action-panel .segmented[aria-label="미제출 조회 범위"] {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      width: 100%;
    }}
    .missing-action-panel .segmented[aria-label="미제출 조회 범위"] button {{
      min-height: 30px;
      padding-inline: 8px;
      font-size: 12px;
    }}
    .missing-action-panel .mini-grid {{
      gap: 8px;
    }}
    .missing-action-panel .inline-actions {{
      grid-column: 1 / -1;
      flex-wrap: nowrap;
      overflow-x: auto;
      padding-bottom: 2px;
      scrollbar-width: none;
    }}
    .missing-action-panel .inline-actions::-webkit-scrollbar {{
      display: none;
    }}
    .missing-action-panel .inline-actions button {{
      flex: 0 0 auto;
      min-height: 32px;
      padding-inline: 10px;
      font-size: 12px;
    }}
    .missing-action-panel .missing-filter {{
      grid-column: 1 / -1;
      margin-bottom: 0;
    }}
    .missing-action-panel .selection-summary {{
      grid-column: 1 / -1;
      align-items: flex-start;
      min-height: 34px;
      padding: 7px 10px;
      font-size: 12px;
    }}
    .missing-action-panel .selection-summary span {{
      min-width: 0;
      text-align: right;
      overflow-wrap: anywhere;
    }}
    .missing-action-panel .missing-action-list {{
      max-height: 204px;
      overflow: auto;
      padding: 6px;
      scrollbar-width: thin;
    }}
    .missing-action-panel .missing-action-list .pager {{
      position: sticky;
      bottom: 0;
      margin: 6px -6px -6px;
      padding: 7px 6px 6px;
      border-top: 1px solid var(--line);
      background: color-mix(in srgb, var(--surface-2) 94%, white);
    }}
    .missing-action-panel .selectable-row {{
      gap: 8px;
      padding: 8px;
      line-height: 1.32;
    }}
    .missing-action-panel .selectable-row strong {{
      font-size: 12.5px;
    }}
    .missing-action-panel .selectable-row span {{
      font-size: 12px;
      line-height: 1.35;
    }}
    .missing-action-panel .action-preview {{
      height: 100%;
      min-height: 66px;
      max-height: 204px;
      overflow: auto;
      line-height: 1.5;
    }}
    .schedule-action-panel {{
      --pad: 16px;
    }}
    .schedule-action-panel .stack {{
      gap: 10px;
    }}
    .schedule-action-panel .inline-actions {{
      flex-wrap: nowrap;
      gap: 6px;
      overflow-x: auto;
      padding-bottom: 2px;
      scrollbar-width: none;
    }}
    .schedule-action-panel .inline-actions::-webkit-scrollbar {{
      display: none;
    }}
    .schedule-action-panel .inline-actions button {{
      flex: 0 0 auto;
      min-height: 32px;
      padding-inline: 10px;
      font-size: 12px;
    }}
    .schedule-action-panel .action-preview {{
      min-height: 74px;
      max-height: 88px;
      overflow: auto;
      padding: 9px 10px;
      line-height: 1.5;
    }}
    .sso-action-panel {{
      --pad: 16px;
    }}
    .sso-action-panel .stack {{
      gap: 10px;
    }}
    .sso-action-panel .sso-fields {{
      gap: 8px;
    }}
    .sso-action-panel input,
    .sso-action-panel select {{
      min-height: 32px;
      padding: 7px 10px;
      font-size: 12.5px;
    }}
    .sso-action-panel .inline-actions {{
      gap: 6px;
      flex-wrap: nowrap;
      overflow-x: auto;
      padding-bottom: 2px;
      scrollbar-width: none;
    }}
    .sso-action-panel .inline-actions::-webkit-scrollbar {{
      display: none;
    }}
    .sso-action-panel .inline-actions button {{
      flex: 0 0 auto;
      min-height: 32px;
      padding-inline: 10px;
      font-size: 12px;
    }}
    .sso-action-panel .action-preview {{
      min-height: 64px;
      max-height: 86px;
      overflow: auto;
      padding: 9px 10px;
      line-height: 1.5;
    }}
    .report-action-panel {{
      --pad: 16px;
      grid-column: span 2;
    }}
    .report-action-panel .stack {{
      gap: 10px;
      grid-template-columns: minmax(0, 1fr) minmax(230px, .8fr);
    }}
    .report-action-panel .mini-grid,
    .report-action-panel .inline-actions,
    .report-action-panel .selection-summary {{
      grid-column: 1 / -1;
    }}
    .report-action-panel .inline-actions {{
      flex-wrap: nowrap;
      overflow-x: auto;
      padding-bottom: 2px;
      scrollbar-width: none;
    }}
    .report-action-panel .inline-actions::-webkit-scrollbar {{
      display: none;
    }}
    .report-action-panel .inline-actions button {{
      flex: 0 0 auto;
      min-height: 32px;
      padding-inline: 10px;
      font-size: 12px;
    }}
    .report-action-panel #reportTargetList {{
      max-height: 238px;
      overflow: auto;
      scrollbar-width: thin;
    }}
    .report-action-panel #reportPreview {{
      height: 100%;
      min-height: 86px;
      max-height: 238px;
      overflow: auto;
    }}
    #tab-actions .action-layout {{
      gap: 12px;
    }}
    #tab-actions .core-panel {{
      --pad: 14px;
    }}
    #tab-actions .core-grid {{
      gap: 10px;
    }}
    #tab-actions .core-button {{
      grid-template-columns: 34px minmax(0, 1fr);
      min-height: 76px;
      padding: 12px;
    }}
    #tab-actions .core-button .core-number {{
      width: 34px;
      height: 34px;
      font-size: 14px;
    }}
    #tab-actions .action-controls {{
      gap: 12px;
    }}
    #tab-actions .missing-action-panel .missing-action-list,
    #tab-actions .missing-action-panel .action-preview {{
      max-height: 180px;
    }}
    #tab-actions .schedule-action-panel textarea.large {{
      min-height: 126px;
    }}
    #tab-actions .schedule-action-panel .action-preview,
    #tab-actions .sso-action-panel .action-preview {{
      min-height: 56px;
      max-height: 74px;
    }}
    #tab-actions .report-action-panel #reportTargetList,
    #tab-actions .report-action-panel #reportPreview {{
      max-height: 188px;
    }}
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
    label {{ display: grid; min-width: 0; gap: 6px; color: var(--muted); font-size: 12.5px; font-weight: 600; line-height: 1.3; }}
    .checkbox-field {{
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      min-height: 48px;
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      color: var(--text);
    }}
    input, select, textarea {{
      width: 100%;
      min-width: 0;
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
    .settings-panel {{
      --pad: 16px;
    }}
    .settings-panel .panel-head {{
      margin-bottom: 10px;
    }}
    .settings-panel .inline-actions {{
      gap: 6px;
      flex-wrap: nowrap;
      overflow-x: auto;
      padding-bottom: 2px;
      scrollbar-width: none;
    }}
    .settings-panel .inline-actions::-webkit-scrollbar {{
      display: none;
    }}
    .settings-panel .inline-actions > * {{
      flex: 0 0 auto;
    }}
    .settings-panel button {{
      min-height: 32px;
      padding: 0 11px;
      font-size: 12.5px;
    }}
    .settings-panel label {{
      margin: 0;
    }}
    .settings-panel input {{
      min-height: 32px;
      padding: 7px 10px;
      font-size: 12.5px;
    }}
    #tab-settings.active {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
      align-items: start;
    }}
    #tab-settings > .action-command {{
      grid-column: 1 / -1;
      margin-bottom: 0;
    }}
    #tab-settings > .settings-panel {{
      margin-top: 0;
      min-width: 0;
    }}
    #tab-settings .settings-panel-strip {{
      display: contents;
    }}
    #tab-settings .settings-panel {{
      margin-top: 0;
      min-width: 0;
    }}
    #tab-settings .settings-panel .readiness-list,
    #tab-settings .settings-panel .schema-list,
    #tab-settings #settings {{
      overflow: auto;
      padding-right: 2px;
      scrollbar-width: thin;
    }}
    #tab-settings .schedule-summary {{
      gap: 6px;
      margin: 9px 0 10px;
    }}
    #tab-settings #readinessSummary,
    #tab-settings #pilotBriefSummary {{
      grid-template-columns: repeat(5, minmax(0, 1fr));
    }}
    #tab-settings #notionSchemaSummary {{
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }}
    #tab-settings .diagnostic-count {{
      min-height: 50px;
      padding: 8px 9px;
    }}
    #tab-settings .diagnostic-count strong {{
      margin-top: 3px;
      font-size: 17px;
    }}
    #tab-settings #readinessList {{
      max-height: 220px;
    }}
    #tab-settings #notionSchemaList,
    #tab-settings #pilotBriefList {{
      max-height: 204px;
    }}
    #tab-settings .schema-command.is-collapsed {{
      min-height: 72px;
      max-height: 76px !important;
      padding: 9px 10px;
      overflow: hidden;
    }}
    #tab-settings #settings {{
      max-height: 204px;
    }}
    #tab-data.active {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
      align-items: start;
    }}
    #tab-data > .action-command {{
      grid-column: 1 / -1;
      margin-bottom: 0;
    }}
    #tab-data > .panel {{
      margin-top: 0;
      min-width: 0;
    }}
    #tab-data .data-panel-strip {{
      display: contents;
    }}
    #tab-data .data-panel-strip > .panel {{
      margin-top: 0;
      min-width: 0;
    }}
    .data-inbox-panel,
    .data-context-panel {{
      --pad: 16px;
    }}
    .data-inbox-panel .schedule-summary,
    .data-context-panel .schedule-summary {{
      grid-template-columns: repeat(auto-fit, minmax(112px, 1fr));
      gap: 7px;
      margin: 10px 0;
    }}
    .data-inbox-panel .diagnostic-count,
    .data-context-panel .diagnostic-count {{
      min-height: 52px;
      padding: 9px 10px;
    }}
    .data-inbox-panel .diagnostic-count strong,
    .data-context-panel .diagnostic-count strong {{
      margin-top: 3px;
      font-size: 17px;
    }}
    .data-inbox-panel .table-wrap,
    .data-context-panel .table-wrap {{
      max-height: min(470px, calc(100vh - 250px));
    }}

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
    .message-preview-list {{
      margin: 10px 0 0;
      padding-left: 20px;
      white-space: normal;
    }}
    .message-preview-list li {{ margin: 7px 0; }}
    .message-preview-card {{
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--panel);
      color: var(--muted);
    }}
    .message-preview-card summary {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      min-height: 38px;
      padding: 8px 10px;
      color: var(--text);
      cursor: pointer;
    }}
    .message-preview-card summary strong {{
      display: inline;
      margin: 0;
      font-size: 13px;
    }}
    .message-preview-meta {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .message-preview-body {{
      margin: 0;
      padding: 0 10px 10px;
      white-space: pre-wrap;
    }}
    .message-preview-body p {{ margin: 6px 0; }}
    .action-preview a {{
      color: var(--primary);
      font-weight: 700;
      text-decoration: none;
    }}
    .action-preview a:hover {{ text-decoration: underline; }}
    .ops-report-preview {{
      max-height: 360px;
      overflow: auto;
      font-family: var(--mono);
      font-size: 12.5px;
    }}
    .student-report-preview {{
      max-height: 420px;
      overflow: auto;
      font-family: var(--mono);
      font-size: 12.5px;
    }}
    .action-preview.is-collapsed {{
      max-height: 148px !important;
      overflow: hidden;
      position: relative;
    }}
    .action-preview.is-expanded {{
      max-height: 520px;
      overflow: auto;
    }}
    .handoff-list {{
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }}
    .dashboard-playbook-panel .inline-actions,
    .dashboard-report-panel .inline-actions {{
      flex-wrap: nowrap;
      overflow-x: auto;
      padding-bottom: 2px;
      scrollbar-width: none;
    }}
    .dashboard-playbook-panel .inline-actions::-webkit-scrollbar,
    .dashboard-report-panel .inline-actions::-webkit-scrollbar {{
      display: none;
    }}
    .dashboard-playbook-panel .inline-actions button,
    .dashboard-report-panel .inline-actions button {{
      flex: 0 0 auto;
    }}
    .dashboard-log-panel .log {{
      min-height: 160px;
      max-height: 180px;
      overflow: auto;
    }}
    .handoff-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface-2);
    }}
    .handoff-row strong {{
      display: block;
      min-width: 0;
      color: var(--text);
      font-size: 13px;
      line-height: 1.3;
      overflow-wrap: anywhere;
    }}
    .handoff-row span {{
      display: block;
      margin-top: 3px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .handoff-link {{
      color: var(--primary);
      font-size: 12.5px;
      font-weight: 700;
      text-decoration: none;
      white-space: nowrap;
    }}
    .handoff-link:hover {{ text-decoration: underline; }}
    .readiness-list {{
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }}
    .settings-panel .readiness-list,
    .settings-panel .schema-list {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
      align-items: start;
      gap: 7px;
      margin-top: 8px;
    }}
    .readiness-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface-2);
    }}
    .settings-panel .readiness-row {{
      padding: 9px 10px;
    }}
    .settings-panel .readiness-fix {{
      display: -webkit-box;
      overflow: hidden;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 2;
    }}
    .readiness-row strong {{
      display: block;
      min-width: 0;
      margin: 4px 0 2px;
      color: var(--text);
      font-size: 13px;
      line-height: 1.3;
      overflow-wrap: anywhere;
    }}
    .readiness-row span:not(.status-pill) {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .readiness-row .status-pill {{
      display: inline-flex;
      width: fit-content;
      max-width: 100%;
    }}
    .readiness-fix {{
      min-width: 120px;
      max-width: 260px;
      color: var(--text-3);
      font-size: 12px;
      font-weight: 650;
      line-height: 1.35;
      text-align: right;
      overflow-wrap: anywhere;
    }}
    .schema-list {{
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }}
    .schema-row {{
      display: grid;
      gap: 8px;
      padding: 11px 12px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface-2);
    }}
    .settings-panel .schema-row {{
      gap: 7px;
      padding: 9px 10px;
    }}
    .schema-row strong {{
      color: var(--text);
      font-size: 13px;
      line-height: 1.3;
      overflow-wrap: anywhere;
    }}
    .schema-meta {{
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }}
    .schema-details {{
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--panel);
    }}
    .schema-details summary {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      min-height: 36px;
      padding: 8px 10px;
      color: var(--muted);
      font-size: 12.5px;
      font-weight: 700;
      cursor: pointer;
      list-style: none;
    }}
    .schema-details summary::-webkit-details-marker {{ display: none; }}
    .schema-details summary::after {{
      content: "펼치기";
      color: var(--primary);
      font-size: 12px;
      white-space: nowrap;
    }}
    .schema-details[open] summary::after {{ content: "접기"; }}
    .schema-props {{
      display: flex;
      gap: 5px;
      flex-wrap: wrap;
      padding: 0 10px 10px;
    }}
    .schema-command {{
      margin-top: 10px;
      font-family: var(--mono);
      font-size: 12px;
    }}
    .settings-panel .schema-command {{
      margin-top: 8px;
    }}
    .settings-panel .schema-command.is-collapsed {{
      min-height: 64px;
      max-height: 96px !important;
    }}
    .playbook-list {{ display: grid; gap: 10px; margin-top: 10px; }}
    .playbook-step {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface-2);
    }}
    .playbook-step strong {{ display: block; margin-bottom: 4px; color: var(--text); font-size: 14px; }}
    .playbook-step span {{ display: block; color: var(--muted); font-size: 12px; line-height: 1.45; }}
    .playbook-step .step-meta {{ display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }}
    .pager {{
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
      justify-content: flex-end;
      margin-top: 10px;
    }}
    .pager button {{
      min-height: 30px;
      padding: 6px 10px;
      font-size: 12px;
    }}
    .pager button.active {{
      background: var(--primary-soft);
      border-color: rgba(37,99,235,.35);
      color: var(--primary);
    }}
    .pager-count {{ color: var(--muted); font-size: 12px; }}
    .sso-fields {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}

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
      max-height: none;
      overflow: visible;
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

    .mobile-card-list {{
      display: none;
    }}
    .missing-card {{
      display: grid;
      gap: 9px;
      padding: 11px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--panel);
      box-shadow: var(--shadow-sm);
    }}
    .missing-card.is-disabled {{
      background: var(--surface-2);
      opacity: .72;
    }}
    .missing-card-head {{
      display: grid;
      grid-template-columns: 22px minmax(0, 1fr) auto;
      gap: 9px;
      align-items: start;
    }}
    .missing-card-head input[type="checkbox"] {{
      width: 18px;
      min-height: 18px;
      margin: 2px 0 0;
      padding: 0;
      accent-color: var(--primary);
    }}
    .missing-card-title {{
      min-width: 0;
    }}
    .missing-card-title strong {{
      display: block;
      color: var(--text);
      font-size: 13.5px;
      font-weight: 720;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }}
    .missing-card-title span {{
      display: block;
      margin-top: 3px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .missing-card-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      min-width: 0;
    }}
    .missing-card-row {{
      display: grid;
      grid-template-columns: minmax(0, .9fr) minmax(0, 1.1fr);
      gap: 8px;
      min-width: 0;
    }}
    .missing-card-field {{
      min-width: 0;
      padding: 8px 9px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface-2);
      color: var(--text);
      font-size: 12.5px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .missing-card-field > span:first-child {{
      display: block;
      margin-bottom: 3px;
      color: var(--text-3);
      font-size: 11px;
      font-weight: 650;
    }}
    .missing-card-field .badge,
    .missing-card-field .status-pill {{
      max-width: 100%;
      height: auto;
      min-height: 20px;
      align-items: flex-start;
      white-space: normal;
      text-align: left;
      overflow-wrap: anywhere;
    }}
    .missing-card-message {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }}

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
    .table-wrap.is-compact {{
      max-height: min(560px, calc(100vh - 250px));
    }}
    table {{ width: 100%; border-collapse: collapse; min-width: 760px; font-size: 13px; }}
    th, td {{ padding: 11px 14px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{
      background: var(--surface-2);
      color: var(--text-3);
      font-size: 11.5px;
      font-weight: 650;
      text-transform: uppercase;
      letter-spacing: 0;
      white-space: nowrap;
    }}
    .compact-table th {{
      position: sticky;
      top: 0;
      z-index: 1;
    }}
    .compact-table th,
    .compact-table td {{
      padding: 9px 11px;
    }}
    .compact-table td {{
      font-size: 12.5px;
      line-height: 1.42;
    }}
    .compact-table .inline-actions {{
      gap: 5px;
      flex-wrap: nowrap;
    }}
    .compact-table .inline-actions button {{
      min-height: 30px;
      padding: 0 9px;
      font-size: 12px;
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
    .empty.compact {{ padding: 16px 12px; font-size: 12.5px; }}

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
    .badge.warn {{ color: var(--accent); background: var(--accent-soft); border-color: transparent; }}
    .badge.error {{ color: var(--danger); background: var(--danger-soft); border-color: transparent; }}
    .status-pill.pending {{ color: var(--accent); background: var(--accent-soft); }}
    .status-pill.dry_run, .status-pill.info {{ color: var(--info); background: var(--info-soft); }}
    .status-pill.sent, .status-pill.ok {{ color: var(--ok); background: var(--ok-soft); }}
    .status-pill.failed, .status-pill.missing {{ color: var(--danger); background: var(--danger-soft); }}
    .status-pill.warn {{ color: var(--accent); background: var(--accent-soft); }}
    .status-pill.skipped {{ color: var(--muted); background: var(--surface-2); border-color: var(--line); }}
    .status-pill.ready, .status-pill.approved {{ color: var(--ok); background: var(--ok-soft); }}
    .status-pill.review {{ color: var(--accent); background: var(--accent-soft); }}
    .status-pill.blocked {{ color: var(--danger); background: var(--danger-soft); }}

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
    .diagnostic-card-list {{ display: none; }}
    .diagnostic-card {{
      display: grid;
      gap: 8px;
      padding: 11px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--panel);
    }}
    .diagnostic-card-head {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: start;
    }}
    .diagnostic-card strong {{
      display: block;
      min-width: 0;
      font-size: 13.3px;
      font-weight: 740;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }}
    .diagnostic-card span:not(.status-pill) {{
      display: block;
      margin-top: 2px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .diagnostic-next {{
      padding: 7px 8px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface-2);
      color: var(--text-3);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .schedule-summary {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin: 14px 0; }}
    .schedule-summary .diagnostic-count strong {{ color: var(--text); }}
    .report-review-panel .schedule-summary {{
      grid-template-columns: repeat(auto-fit, minmax(112px, 1fr));
      gap: 7px;
      margin: 10px 0;
    }}
    .report-review-panel .diagnostic-count {{
      min-height: 52px;
      padding: 9px 10px;
    }}
    .report-review-panel .diagnostic-count strong {{
      margin-top: 3px;
      font-size: 17px;
    }}
    #tab-reports.active {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
      align-items: start;
    }}
    #tab-reports > .action-command {{
      grid-column: 1 / -1;
      margin-bottom: 0;
      padding: 10px 12px;
    }}
    #tab-reports > .report-command,
    #tab-reports > .exam-command {{
      gap: 10px;
    }}
    #tab-reports > .action-command .command-label {{
      margin-bottom: 4px;
    }}
    #tab-reports > .action-command strong {{
      font-size: 15px;
      line-height: 1.2;
    }}
    #tab-reports > .action-command-main > p {{
      display: -webkit-box;
      margin-top: 3px;
      overflow: hidden;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 1;
      line-height: 1.32;
    }}
    #tab-reports > .action-command .command-meta {{
      gap: 4px;
      margin-top: 8px;
    }}
    #tab-reports > .action-command .action-command-actions {{
      gap: 6px;
      margin-top: 7px;
    }}
    #tab-reports > .action-command .action-command-actions button {{
      min-height: 30px;
      padding-inline: 10px;
      font-size: 12px;
    }}
    #tab-reports > .action-command .command-gate {{
      margin-top: 7px;
      padding-top: 7px;
      gap: 6px;
    }}
    #tab-reports > .action-command .command-gate p {{
      display: -webkit-box;
      overflow: hidden;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 1;
      line-height: 1.35;
    }}
    #tab-reports > .exam-command {{
      grid-template-columns: minmax(0, 1.35fr) minmax(320px, .65fr);
    }}
    #tab-reports > .exam-command .command-meta,
    #tab-reports > .exam-command .action-command-actions {{
      flex-wrap: nowrap;
      overflow-x: auto;
      padding-bottom: 2px;
      scrollbar-width: none;
    }}
    #tab-reports > .exam-command .command-meta::-webkit-scrollbar,
    #tab-reports > .exam-command .action-command-actions::-webkit-scrollbar {{
      display: none;
    }}
    #tab-reports > .exam-command .command-meta .badge,
    #tab-reports > .exam-command .action-command-actions button {{
      flex: 0 0 auto;
    }}
    #tab-reports > .exam-command .workflow-step {{
      padding: 6px 7px;
    }}
    #tab-reports > .report-command .workflow-step p,
    #tab-reports > .exam-command .workflow-step p {{
      -webkit-line-clamp: 1;
    }}
    #tab-reports > .exam-command .workflow-step span {{
      width: 18px;
      height: 18px;
      margin-bottom: 3px;
    }}
    #tab-reports > .panel {{
      margin-top: 0;
    }}
    #tab-reports .report-review-strip {{
      display: contents;
    }}
    #tab-reports .exam-form-strip {{
      display: contents;
    }}
    .report-overview-panel,
    .student-report-panel {{
      --pad: 16px;
    }}
    .report-overview-panel .inline-actions,
    .student-report-panel .inline-actions,
    .weekly-draft-panel .inline-actions {{
      gap: 8px;
    }}
    #tab-reports .report-overview-panel .inline-actions {{
      flex-wrap: nowrap;
      overflow-x: auto;
      padding-bottom: 2px;
      scrollbar-width: none;
    }}
    #tab-reports .report-overview-panel .inline-actions::-webkit-scrollbar {{
      display: none;
    }}
    #tab-reports .report-overview-panel .inline-actions button {{
      flex: 0 0 auto;
    }}
    .report-overview-panel label {{
      min-width: 136px;
    }}
    .student-report-panel .action-preview {{
      min-height: 120px;
      max-height: 220px;
      overflow: auto;
    }}
    .schedule-panel .schedule-summary {{
      grid-template-columns: repeat(auto-fit, minmax(112px, 1fr));
      gap: 7px;
      margin: 10px 0;
    }}
    .schedule-panel .diagnostic-count {{
      min-height: 52px;
      padding: 9px 10px;
    }}
    .schedule-panel .diagnostic-count strong {{
      margin-top: 3px;
      font-size: 17px;
    }}
    .neis-exam-panel {{
      --pad: 16px;
    }}
    .neis-exam-panel .panel-head p {{
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 12.5px;
      line-height: 1.45;
    }}
    .neis-exam-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 10px 0;
    }}
    .neis-exam-table {{
      min-width: 980px;
      font-size: 12.5px;
    }}
    .neis-exam-table th {{
      text-transform: none;
      font-size: 11.5px;
    }}
    .neis-exam-table td {{
      line-height: 1.48;
    }}
    .neis-exam-table strong {{
      display: block;
      margin-bottom: 4px;
      font-size: 13.5px;
      line-height: 1.2;
    }}
    .neis-exam-table .school-address {{
      display: block;
      color: var(--muted);
      font-size: 11.5px;
      line-height: 1.35;
    }}
    .exam-lines {{
      display: grid;
      gap: 5px;
    }}
    .exam-line {{
      display: flex;
      gap: 7px;
      align-items: center;
      min-width: 0;
    }}
    .exam-label {{
      flex: 0 0 auto;
      min-width: 66px;
      padding: 3px 7px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface-2);
      color: var(--text-3);
      font-size: 11px;
      font-weight: 750;
      text-align: center;
    }}
    .exam-label.warn {{
      background: rgba(245, 158, 11, .12);
      border-color: rgba(245, 158, 11, .28);
      color: var(--accent);
    }}
    .exam-label.danger {{
      background: rgba(239, 68, 68, .1);
      border-color: rgba(239, 68, 68, .25);
      color: var(--danger);
    }}
    .exam-label.info {{
      background: rgba(14, 116, 144, .1);
      border-color: rgba(14, 116, 144, .22);
      color: var(--info);
    }}
    .exam-date {{
      font-weight: 720;
      color: var(--text);
      white-space: nowrap;
    }}
    .exam-form-panel .stack {{
      gap: 8px;
    }}
    .exam-form-panel {{
      --pad: 16px;
    }}
    .exam-form-panel .panel-head {{
      margin-bottom: 10px;
    }}
    .exam-form-panel label,
    .exam-form-panel input,
    .exam-form-panel textarea {{
      min-width: 0;
    }}
    .exam-form-panel label {{
      gap: 5px;
      margin: 0;
      font-size: 12px;
    }}
    .exam-form-panel input {{
      min-height: 32px;
      padding: 7px 10px;
      font-size: 12.5px;
    }}
    .exam-form-panel input[type="file"] {{
      min-height: 32px;
      padding: 6px 10px;
      font-size: 12px;
    }}
    .exam-form-panel .checkbox-field {{
      min-height: 34px;
      padding: 7px 9px;
      font-size: 12px;
    }}
    .exam-form-panel .checkbox-field input[type="checkbox"] {{
      width: 17px;
      min-height: 17px;
    }}
    .exam-form-panel textarea.large {{
      min-height: 96px;
      max-height: 188px;
      line-height: 1.45;
    }}
    .exam-form-panel .action-preview {{
      min-height: 52px;
      max-height: 124px;
      overflow: auto;
      line-height: 1.5;
    }}
    .exam-form-panel .inline-actions {{
      flex-wrap: nowrap;
      overflow-x: auto;
      padding-bottom: 2px;
      scrollbar-width: none;
    }}
    .exam-form-panel .inline-actions::-webkit-scrollbar {{
      display: none;
    }}
    .exam-form-panel .inline-actions button {{
      flex: 0 0 auto;
      min-height: 32px;
      padding-inline: 10px;
      font-size: 12px;
    }}
    .exam-date-grid,
    .exam-toggle-grid {{
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .exam-csv-input {{
      gap: 5px;
    }}
    .badge-strip {{
      display: flex;
      gap: 4px;
      flex-wrap: nowrap;
      max-width: 360px;
      overflow-x: auto;
      scrollbar-width: none;
    }}
    .badge-strip::-webkit-scrollbar {{ display: none; }}
    .badge-strip .badge {{
      flex: 0 0 auto;
      max-width: 230px;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .settings-panel .schedule-summary {{
      grid-template-columns: repeat(auto-fit, minmax(108px, 1fr));
      gap: 7px;
      margin: 9px 0;
    }}
    .settings-panel .diagnostic-count {{
      min-height: 52px;
      padding: 9px 10px;
    }}
    .settings-panel .diagnostic-count strong {{
      margin-top: 3px;
      font-size: 17px;
    }}
    .student-list {{ max-width: 280px; color: var(--muted); line-height: 1.55; }}
    .hub-focus {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }}
    .ops-command {{
      display: grid;
      grid-template-columns: minmax(0, 1.25fr) minmax(230px, .75fr);
      gap: 12px;
      margin-bottom: 14px;
      padding: 14px;
      border: 1px solid var(--line);
      border-left: 4px solid var(--primary);
      border-radius: var(--radius-sm);
      background: var(--surface-2);
    }}
    .ops-command.danger {{ border-left-color: var(--danger); }}
    .ops-command.warn {{ border-left-color: var(--accent); }}
    .ops-command.ok {{ border-left-color: var(--ok); }}
    .ops-command-main,
    .ops-command-side {{
      min-width: 0;
    }}
    .command-label {{
      display: block;
      margin-bottom: 5px;
      color: var(--text-3);
      font-size: 11.5px;
      font-weight: 700;
    }}
    .ops-command strong {{
      display: block;
      color: var(--text);
      font-size: 16px;
      font-weight: 780;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }}
    .ops-command p {{
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 12.5px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }}
    .command-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      margin-top: 10px;
    }}
    .command-action {{
      margin-top: 10px;
      width: 100%;
    }}
    .command-gate {{
      grid-column: 1 / -1;
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: 8px;
      align-items: start;
      padding-top: 11px;
      border-top: 1px solid var(--line);
    }}
    .command-gate span {{
      color: var(--text-3);
      font-size: 11.5px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .command-gate p {{
      margin: 0;
      font-size: 12px;
    }}
    .focus-card, .lane-card, .queue-row, .brief-row {{
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface-2);
    }}
    .focus-card {{
      display: grid;
      gap: 5px;
      min-height: 74px;
      padding: 12px 14px;
      cursor: pointer;
    }}
    .focus-card:hover {{ border-color: var(--primary); background: var(--panel); }}
    .focus-card span {{ color: var(--text-3); font-size: 11.5px; font-weight: 650; }}
    .focus-card strong {{ font-size: 24px; font-weight: 780; line-height: 1; }}
    .focus-card.ok strong {{ color: var(--ok); }}
    .focus-card.info strong {{ color: var(--info); }}
    .focus-card.warn strong {{ color: var(--accent); }}
    .focus-card.danger strong {{ color: var(--danger); }}
    .hub-lanes {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .lane-card {{
      display: grid;
      gap: 8px;
      min-height: 138px;
      padding: 13px;
    }}
    .lane-card h3 {{
      margin: 0;
      font-size: 13.5px;
      font-weight: 760;
      line-height: 1.25;
    }}
    .lane-card p {{
      margin: 0;
      color: var(--muted);
      font-size: 12.2px;
      line-height: 1.4;
    }}
    .lane-card .lane-foot {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-top: auto;
      color: var(--text-3);
      font-size: 12px;
      font-weight: 650;
    }}
    .dashboard-hub-panel .ops-command {{
      gap: 10px;
      margin-bottom: 10px;
      padding: 12px;
    }}
    .dashboard-hub-panel .hub-focus {{
      display: flex;
      gap: 7px;
      margin: 0 -2px 9px;
      overflow-x: auto;
      padding: 0 2px 2px;
      scroll-snap-type: x proximity;
      scrollbar-width: none;
    }}
    .dashboard-hub-panel .hub-focus::-webkit-scrollbar {{
      display: none;
    }}
    .dashboard-hub-panel .focus-card {{
      flex: 0 0 116px;
      min-height: 60px;
      padding: 9px 10px;
      gap: 3px;
      scroll-snap-align: start;
    }}
    .dashboard-hub-panel .focus-card strong {{
      font-size: 20px;
    }}
    .dashboard-hub-panel .focus-card span {{
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .dashboard-hub-panel .hub-lanes {{
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
    }}
    .dashboard-hub-panel .lane-card {{
      min-height: 118px;
      padding: 10px;
      gap: 6px;
    }}
    .dashboard-hub-panel .lane-card h3,
    .dashboard-hub-panel .lane-card p {{
      display: -webkit-box;
      overflow: hidden;
      -webkit-box-orient: vertical;
    }}
    .dashboard-hub-panel .lane-card h3 {{
      font-size: 12.8px;
      -webkit-line-clamp: 2;
    }}
    .dashboard-hub-panel .lane-card p {{
      -webkit-line-clamp: 2;
    }}
    .dashboard-hub-panel .lane-card .lane-foot {{
      display: grid;
      gap: 3px;
      font-size: 11.5px;
    }}
    .dashboard-hub-panel .lane-card .lane-foot span {{
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .dashboard-work-strip {{
      display: contents;
    }}
    .queue-panel {{
      display: flex;
      flex-direction: column;
      max-height: min(760px, calc(100vh - 150px));
      overflow: hidden;
    }}
    .queue-panel .panel-head,
    .queue-panel .queue-filter {{
      flex-shrink: 0;
    }}
    .queue-list {{
      display: grid;
      gap: 8px;
      min-height: 0;
      overflow-y: auto;
      padding-right: 4px;
      scrollbar-width: thin;
      scrollbar-color: var(--line-strong) transparent;
    }}
    .queue-list::-webkit-scrollbar {{ width: 6px; }}
    .queue-list::-webkit-scrollbar-thumb {{
      background: var(--line-strong);
      border-radius: 999px;
    }}
    .queue-card-strip {{
      display: grid;
      gap: 8px;
    }}
    .dashboard-queue-panel .queue-card-strip {{
      gap: 6px;
    }}
    .dashboard-queue-panel .queue-row {{
      gap: 8px;
      padding: 8px 9px;
    }}
    .dashboard-queue-panel .queue-row strong,
    .dashboard-queue-panel .queue-row > div:first-child > span:not(.status-pill) {{
      display: -webkit-box;
      overflow: hidden;
      -webkit-box-orient: vertical;
    }}
    .dashboard-queue-panel .queue-row strong {{
      -webkit-line-clamp: 1;
    }}
    .dashboard-queue-panel .queue-row > div:first-child > span:not(.status-pill) {{
      -webkit-line-clamp: 1;
    }}
    .dashboard-queue-panel .queue-details {{
      display: none;
    }}
    .dashboard-queue-panel .queue-badges {{
      margin-top: 5px;
    }}
    .dashboard-queue-panel .queue-actions {{
      gap: 5px;
    }}
    .dashboard-queue-panel .queue-actions button {{
      min-height: 29px;
      padding-inline: 9px;
      font-size: 11.8px;
    }}
    .dashboard-queue-panel .queue-action {{
      min-width: 72px;
      max-width: 104px;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .brief-list {{ display: grid; gap: 8px; }}
    .dashboard-brief-panel .brief-list {{
      display: flex;
      gap: 8px;
      margin: 0 -2px;
      overflow-x: auto;
      padding: 0 2px 2px;
      scroll-snap-type: x proximity;
      scrollbar-width: none;
    }}
    .dashboard-brief-panel .brief-list::-webkit-scrollbar {{
      display: none;
    }}
    .brief-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: center;
      min-height: 82px;
      padding: 12px 13px;
      background: var(--panel);
      cursor: pointer;
    }}
    .dashboard-brief-panel .brief-row {{
      flex: 0 0 188px;
      grid-template-columns: 1fr;
      gap: 8px;
      align-content: start;
      min-height: 158px;
      padding: 10px;
      scroll-snap-align: start;
    }}
    .brief-row:hover {{ border-color: var(--primary); background: var(--surface-2); }}
    .brief-row strong {{
      display: block;
      min-width: 0;
      margin: 4px 0 2px;
      font-size: 13.5px;
      font-weight: 730;
      line-height: 1.3;
      overflow-wrap: anywhere;
    }}
    .dashboard-brief-panel .brief-row strong {{
      display: -webkit-box;
      margin: 5px 0 3px;
      overflow: hidden;
      font-size: 13px;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 2;
    }}
    .brief-row span:not(.status-pill) {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .dashboard-brief-panel .brief-row span:not(.status-pill) {{
      display: -webkit-box;
      overflow: hidden;
      font-size: 11.5px;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 2;
    }}
    .brief-row .status-pill {{
      display: inline-flex;
      width: fit-content;
      max-width: 100%;
    }}
    .brief-row .brief-action {{
      color: var(--primary);
      font-size: 12.5px;
      font-weight: 700;
      text-align: right;
      white-space: nowrap;
    }}
    .dashboard-brief-panel .brief-row .brief-action {{
      align-self: end;
      justify-self: start;
      max-width: 100%;
      padding: 4px 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--primary-soft);
      font-size: 11.5px;
      text-align: left;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .queue-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 9px;
      align-items: start;
      padding: 10px 11px;
      background: var(--panel);
    }}
    .queue-row > div {{
      min-width: 0;
    }}
    .queue-row strong {{
      display: block;
      min-width: 0;
      font-size: 13.2px;
      font-weight: 730;
      line-height: 1.3;
      overflow-wrap: anywhere;
    }}
    .queue-row span {{
      display: block;
      margin-top: 2px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.32;
      overflow-wrap: anywhere;
    }}
    .queue-row .status-pill {{
      display: inline-flex;
      width: fit-content;
      max-width: 100%;
      margin-top: 0;
    }}
    .queue-badges {{
      display: flex;
      flex-wrap: nowrap;
      gap: 4px;
      margin-top: 6px;
      overflow-x: auto;
      scrollbar-width: none;
    }}
    .queue-badges::-webkit-scrollbar {{ display: none; }}
    .queue-badges .badge {{
      display: inline-block;
      flex: 0 0 auto;
      max-width: min(240px, 100%);
      margin-top: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .queue-details {{
      margin-top: 6px;
      color: var(--text-3);
      font-size: 12px;
    }}
    .queue-details summary {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      color: var(--primary);
      font-weight: 700;
      cursor: pointer;
      list-style: none;
    }}
    .queue-details summary::-webkit-details-marker {{ display: none; }}
    .queue-details summary::after {{
      content: "+";
      font-weight: 800;
    }}
    .queue-details[open] summary::after {{ content: "-"; }}
    .queue-note {{
      margin-top: 5px !important;
      color: var(--text-3) !important;
    }}
    .queue-action {{
      min-width: 86px;
      color: var(--primary);
      font-size: 12.5px;
      font-weight: 700;
      text-align: right;
      white-space: nowrap;
    }}
    .queue-actions {{
      display: grid;
      justify-items: end;
      gap: 7px;
    }}
    .queue-actions button {{
      min-height: 32px;
      padding: 0 10px;
      font-size: 12px;
    }}
    .queue-filter,
    .missing-filter {{
      display: flex;
      max-width: 100%;
      min-width: 0;
      overflow-x: auto;
      margin: 0 0 12px;
      scrollbar-width: none;
    }}
    .queue-filter::-webkit-scrollbar,
    .missing-filter::-webkit-scrollbar {{ display: none; }}
    .queue-filter button,
    .missing-filter button {{
      flex: 0 0 auto;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}

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
      border-radius: 8px;
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
      .sidebar {{
        position: sticky;
        z-index: 10;
        height: 56px;
        display: grid;
        grid-template-columns: 54px minmax(0, 1fr);
        align-items: stretch;
        overflow: hidden;
      }}
      .brand {{
        height: 56px;
        justify-content: center;
        padding: 0;
        border-right: 1px solid var(--line);
        border-bottom: 0;
        gap: 0;
      }}
      .brand-mark {{
        width: 28px;
        height: 28px;
      }}
      .brand > div:not(.brand-mark) {{
        display: none;
      }}
      .sidebar .tabs {{
        flex-direction: row;
        flex-wrap: nowrap;
        gap: 6px;
        align-items: center;
        height: 56px;
        padding: 8px 10px;
        overflow-x: auto;
        overflow-y: hidden;
        scrollbar-width: none;
      }}
      .sidebar .tabs::-webkit-scrollbar {{ display: none; }}
      .sidebar .tab-button {{
        width: auto;
        flex: 0 0 auto;
        grid-template-columns: 16px auto;
        column-gap: 7px;
        min-height: 36px;
        padding: 0 8px;
        font-size: 12.5px;
      }}
      .sidebar .tab-button .ic {{
        width: 16px;
        height: 16px;
      }}
      .sidebar .tab-button .label {{
        font-size: 0;
      }}
      .sidebar .tab-button .label::after {{
        content: attr(data-short);
        font-size: 12.5px;
      }}
      .sidebar .tab-button.active {{
        box-shadow: inset 0 -3px 0 var(--primary);
      }}
      .nav-sep {{ display: none; }}
      .sidebar-footer {{ display: none; }}
      .topbar {{
        height: auto;
        flex-wrap: wrap;
        align-items: flex-start;
        gap: 9px;
        padding: 10px 14px;
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
        min-height: 34px;
        padding-inline: 12px;
        font-size: 12.5px;
      }}
      .topbar-user {{
        display: none;
      }}
      .status-strip {{
        flex-wrap: nowrap;
        height: 54px;
        scrollbar-width: none;
      }}
      .status-strip::-webkit-scrollbar {{ display: none; }}
      .metric {{
        min-width: 98px;
        padding: 0 14px;
      }}
      .metric strong {{
        font-size: 18px;
      }}
      .grid, .actions, .actions.compact, .action-layout, .action-controls, .mini-grid,
      .diagnostic-summary, .schedule-summary, .hub-lanes, .sso-fields {{ grid-template-columns: 1fr; }}
      #tab-reports.active {{
        display: block;
      }}
      #tab-reports > .action-command {{
        margin-bottom: 12px;
      }}
      #tab-reports > .exam-command {{
        grid-template-columns: 1fr;
      }}
      #tab-reports > .panel + .panel {{
        margin-top: 12px;
      }}
      #tab-reports > .action-command {{
        gap: 7px;
        padding: 10px;
      }}
      #tab-reports > .action-command .action-command-main > p {{
        -webkit-line-clamp: 1;
      }}
      #tab-reports > .action-command .command-meta,
      #tab-reports > .action-command .action-command-actions {{
        margin-top: 6px;
      }}
      #tab-reports > .action-command .command-gate {{
        margin-top: 6px;
        padding: 6px 7px;
      }}
      #tab-reports > .action-command .workflow-step {{
        flex-basis: min(112px, 46vw);
        min-height: 48px;
        padding: 6px 7px;
      }}
      #tab-reports > .action-command .workflow-step p {{
        display: none;
      }}
      #tab-reports .report-overview-panel,
      #tab-reports .report-review-panel,
      #tab-reports .student-report-panel,
      #tab-reports .exam-form-panel {{
        --pad: 14px;
      }}
      #tab-reports .student-report-panel .action-preview {{
        min-height: 84px;
        max-height: 120px;
      }}
      #tab-reports .report-review-strip {{
        display: flex;
        gap: 8px;
        margin: 0 0 12px;
        overflow-x: auto;
        overflow-y: hidden;
        padding: 0 2px 3px;
        scroll-snap-type: x proximity;
        scrollbar-width: none;
      }}
      #tab-reports .report-review-strip::-webkit-scrollbar {{
        display: none;
      }}
      #tab-reports .report-review-strip > .panel {{
        flex: 0 0 min(286px, 82vw);
        max-height: 238px;
        margin-top: 0;
        overflow: auto;
        scroll-snap-align: start;
      }}
      #tab-reports .report-review-strip > .report-overview-panel {{
        flex-basis: min(300px, 84vw);
      }}
      #tab-reports .report-review-strip > .student-report-panel {{
        flex-basis: min(268px, 78vw);
      }}
      #tab-reports .report-review-strip .panel-head {{
        gap: 8px;
        margin-bottom: 8px;
      }}
      #tab-reports .report-review-strip .panel-head h2 {{
        font-size: 15px;
      }}
      #tab-reports .report-review-strip .inline-actions {{
        width: 100%;
        flex-wrap: nowrap;
        overflow-x: auto;
        padding-bottom: 2px;
        scrollbar-width: none;
      }}
      #tab-reports .report-review-strip .inline-actions::-webkit-scrollbar {{
        display: none;
      }}
      #tab-reports .report-review-strip .inline-actions > * {{
        flex: 0 0 auto;
      }}
      #tab-reports .report-review-strip .inline-actions button,
      #tab-reports .report-review-strip .inline-actions label {{
        min-height: 30px;
        font-size: 12px;
      }}
      #tab-reports .report-review-strip .empty {{
        padding: 16px 12px;
        font-size: 12px;
      }}
      #tab-reports .exam-form-panel textarea.large {{
        min-height: 68px;
        max-height: 88px;
      }}
      #tab-reports .exam-form-panel .action-preview {{
        min-height: 42px;
        max-height: 58px;
      }}
      #tab-reports .exam-form-panel {{
        --pad: 12px;
      }}
      #tab-reports .exam-form-strip {{
        display: flex;
        gap: 8px;
        margin: 0 -2px;
        overflow-x: auto;
        overflow-y: hidden;
        padding: 0 2px 3px;
        scroll-snap-type: x proximity;
        scrollbar-width: none;
      }}
      #tab-reports .exam-form-strip::-webkit-scrollbar {{
        display: none;
      }}
      #tab-reports .exam-form-strip > .exam-form-panel {{
        flex: 0 0 min(326px, 88vw);
        max-height: 456px;
        margin-top: 0;
        overflow: auto;
        scroll-snap-align: start;
      }}
      #tab-reports .exam-form-panel .panel-head {{
        margin-bottom: 8px;
      }}
      #tab-reports .exam-form-panel .stack {{
        gap: 7px;
      }}
      #tab-reports .exam-form-panel label {{
        gap: 4px;
        font-size: 11.5px;
      }}
      #tab-reports .exam-form-panel input {{
        min-height: 30px;
        padding: 6px 8px;
      }}
      #tab-reports .exam-form-panel input[type="file"] {{
        min-height: 30px;
        padding: 5px 8px;
      }}
      #tab-reports .exam-form-panel .checkbox-field {{
        min-height: 32px;
        padding: 6px 8px;
      }}
      #tab-settings.active {{
        display: block;
      }}
      #tab-settings > .action-command {{
        gap: 7px;
        margin-bottom: 12px;
        padding: 10px;
      }}
      #tab-settings > .action-command .action-command-main > p {{
        -webkit-line-clamp: 1;
      }}
      #tab-settings > .action-command .command-meta,
      #tab-settings > .action-command .action-command-actions {{
        margin-top: 6px;
      }}
      #tab-settings > .action-command .command-gate {{
        margin-top: 6px;
        padding: 6px 7px;
      }}
      #tab-settings > .action-command .workflow-step {{
        flex-basis: min(112px, 46vw);
        min-height: 48px;
        padding: 6px 7px;
      }}
      #tab-settings > .action-command .workflow-step p {{
        display: none;
      }}
      #tab-settings > .settings-panel + .settings-panel {{
        margin-top: 12px;
      }}
      #tab-settings > .settings-panel {{
        padding: 14px;
      }}
      #tab-settings .settings-panel-strip {{
        display: flex;
        gap: 8px;
        margin: 0 -2px;
        overflow-x: auto;
        overflow-y: hidden;
        padding: 0 2px 3px;
        scroll-snap-type: x proximity;
        scrollbar-width: none;
      }}
      #tab-settings .settings-panel-strip::-webkit-scrollbar {{
        display: none;
      }}
      #tab-settings .settings-panel-strip > .settings-panel {{
        flex: 0 0 min(320px, 86vw);
        max-height: 504px;
        margin-top: 0;
        overflow: auto;
        padding: 14px;
        scroll-snap-align: start;
      }}
      #tab-settings .settings-panel .panel-head {{
        gap: 8px;
        margin-bottom: 8px;
      }}
      #tab-settings .settings-panel .inline-actions {{
        width: 100%;
      }}
      #tab-settings .settings-panel .inline-actions button {{
        min-height: 30px;
        padding-inline: 9px;
        font-size: 12px;
      }}
      #tab-settings .settings-panel .inline-actions label {{
        min-width: 132px;
      }}
      #tab-settings #readinessSummary,
      #tab-settings #pilotBriefSummary,
      #tab-settings #notionSchemaSummary {{
        display: flex;
        gap: 7px;
        margin: 8px -2px 8px;
        overflow-x: auto;
        padding: 0 2px 2px;
        scrollbar-width: none;
      }}
      #tab-settings #readinessSummary::-webkit-scrollbar,
      #tab-settings #pilotBriefSummary::-webkit-scrollbar,
      #tab-settings #notionSchemaSummary::-webkit-scrollbar {{
        display: none;
      }}
      #tab-settings #readinessSummary .diagnostic-count,
      #tab-settings #pilotBriefSummary .diagnostic-count,
      #tab-settings #notionSchemaSummary .diagnostic-count {{
        flex: 0 0 96px;
        min-height: 48px;
        padding: 8px 9px;
      }}
      #tab-settings #readinessList {{
        max-height: 210px;
      }}
      #tab-settings #notionSchemaList,
      #tab-settings #pilotBriefList,
      #tab-settings #settings {{
        max-height: 176px;
      }}
      #tab-settings .schema-command.is-collapsed {{
        min-height: 60px;
        max-height: 64px !important;
      }}
      #tab-data.active {{
        display: block;
      }}
      #tab-data > .action-command {{
        margin-bottom: 12px;
      }}
      #tab-data > .panel + .panel {{
        margin-top: 12px;
      }}
      #tab-data .data-panel-strip {{
        display: flex;
        gap: 8px;
        margin: 0 -2px;
        overflow-x: auto;
        overflow-y: hidden;
        padding: 0 2px 3px;
        scroll-snap-type: x proximity;
        scrollbar-width: none;
      }}
      #tab-data .data-panel-strip::-webkit-scrollbar {{
        display: none;
      }}
      #tab-data .data-panel-strip > .panel {{
        flex: 0 0 min(326px, 88vw);
        max-height: 620px;
        margin-top: 0;
        overflow: auto;
        scroll-snap-align: start;
      }}
      #tab-missing > .action-command {{
        gap: 7px;
        padding: 10px;
      }}
      #tab-missing > .action-command .action-command-main > p {{
        -webkit-line-clamp: 1;
      }}
      #tab-missing > .action-command .command-meta,
      #tab-missing > .action-command .action-command-actions {{
        margin-top: 6px;
      }}
      #tab-missing > .action-command .command-gate {{
        margin-top: 6px;
        padding: 6px 7px;
      }}
      #tab-missing > .action-command .workflow-step {{
        flex-basis: min(112px, 46vw);
        min-height: 48px;
        padding: 6px 7px;
      }}
      #tab-missing > .action-command .workflow-step p {{
        display: none;
      }}
      .action-command {{
        gap: 9px;
        margin-bottom: 12px;
        padding: 12px;
      }}
      .action-command strong {{
        font-size: 15px;
      }}
      .action-command p {{
        display: -webkit-box;
        margin-top: 4px;
        overflow: hidden;
        font-size: 12px;
        line-height: 1.35;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 2;
      }}
      .action-command-actions {{
        flex-wrap: nowrap;
        gap: 6px;
        margin-top: 9px;
        overflow-x: auto;
        padding-bottom: 2px;
        scrollbar-width: none;
      }}
      .action-command-actions::-webkit-scrollbar {{
        display: none;
      }}
      .action-command-actions button {{
        flex: 0 0 auto;
        min-height: 30px;
        padding-inline: 10px;
        font-size: 12px;
      }}
      .action-command .command-gate {{
        grid-template-columns: auto minmax(0, 1fr);
        gap: 6px;
        align-items: start;
        margin-top: 7px;
        padding: 6px 8px;
        border: 1px solid var(--line);
        border-radius: var(--radius-sm);
        background: var(--surface-2);
      }}
      .action-command .command-gate span {{
        font-size: 11px;
        white-space: nowrap;
      }}
      .action-command .command-gate p {{
        margin: 0;
        font-size: 11.5px;
        -webkit-line-clamp: 1;
      }}
      .action-command-side {{
        overflow-x: hidden;
        padding-top: 6px;
        border-top: 1px solid var(--line);
      }}
      .action-command-side .workflow-steps {{
        display: flex;
        gap: 7px;
        margin: 0;
        overflow-x: auto;
        padding: 0 0 2px;
        scroll-snap-type: x proximity;
        scrollbar-width: none;
      }}
      .action-command-side .workflow-steps::-webkit-scrollbar {{
        display: none;
      }}
      .action-command-side .workflow-step {{
        flex: 0 0 min(138px, 60vw);
        min-height: 70px;
        padding: 7px 8px;
        scroll-snap-align: start;
      }}
      .action-command-side .workflow-step span {{
        width: 18px;
        height: 18px;
        margin-bottom: 4px;
      }}
      .action-command-side .workflow-step strong {{
        font-size: 12px;
      }}
      .action-command-side .workflow-step p {{
        display: -webkit-box;
        margin-top: 2px;
        overflow: hidden;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 2;
        min-height: 0;
        font-size: 11px;
        line-height: 1.25;
      }}
      #tab-reports > .action-command .action-command-side {{
        overflow-x: hidden;
      }}
      #tab-reports > .action-command .workflow-steps {{
        margin-inline: 0;
        padding-inline: 0;
      }}
      .schedule-panel .schedule-summary {{
        display: flex;
        gap: 7px;
        margin: 8px -2px 8px;
        overflow-x: auto;
        padding: 0 2px 2px;
        scrollbar-width: none;
      }}
      .schedule-panel .schedule-summary::-webkit-scrollbar {{
        display: none;
      }}
      .schedule-panel .schedule-summary .diagnostic-count {{
        flex: 0 0 104px;
      }}
      .exam-form-panel .actions.compact {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 8px;
      }}
      .exam-form-panel .actions.compact.exam-date-grid,
      .exam-form-panel .actions.compact.exam-toggle-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .exam-form-panel input[type="datetime-local"] {{
        padding-inline: 8px;
        font-size: 12px;
      }}
      .exam-form-panel textarea.large {{
        min-height: 96px;
      }}
      .dashboard-hub-panel {{
        padding: 14px;
      }}
      .dashboard-hub-panel .panel-head {{
        gap: 9px;
        margin-bottom: 10px;
      }}
      .dashboard-hub-panel .ops-command {{
        gap: 7px;
        margin-bottom: 10px;
        padding: 10px;
      }}
      .dashboard-hub-panel .ops-command strong {{
        font-size: 15px;
      }}
      .dashboard-hub-panel .ops-command p {{
        display: -webkit-box;
        margin-top: 4px;
        overflow: hidden;
        color: var(--muted);
        font-size: 12px;
        line-height: 1.35;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 2;
      }}
      .dashboard-hub-panel .ops-command-main p {{
        -webkit-line-clamp: 1;
      }}
      .dashboard-hub-panel .command-meta {{
        flex-wrap: nowrap;
        gap: 4px;
        margin-top: 7px;
        overflow-x: auto;
        padding-bottom: 2px;
        scrollbar-width: none;
      }}
      .dashboard-hub-panel .command-meta::-webkit-scrollbar {{
        display: none;
      }}
      .dashboard-hub-panel .command-meta > * {{
        flex: 0 0 auto;
      }}
      .dashboard-hub-panel .ops-command-side {{
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        gap: 3px 8px;
        align-items: end;
        padding: 7px 8px;
        border: 1px solid var(--line);
        border-radius: var(--radius-sm);
        background: var(--panel);
      }}
      .dashboard-hub-panel .ops-command-side .command-label {{
        display: none;
      }}
      .dashboard-hub-panel .ops-command-side strong,
      .dashboard-hub-panel .ops-command-side p {{
        grid-column: 1;
      }}
      .dashboard-hub-panel .ops-command-side strong {{
        font-size: 13px;
      }}
      .dashboard-hub-panel .ops-command-side p {{
        -webkit-line-clamp: 1;
      }}
      .dashboard-hub-panel .ops-command-side .command-action {{
        grid-column: 2;
        grid-row: 1 / 3;
        width: auto;
        min-height: 30px;
        margin: 0;
        padding-inline: 9px;
        font-size: 11.5px;
      }}
      .dashboard-hub-panel .command-gate {{
        grid-template-columns: auto minmax(0, 1fr);
        gap: 6px;
        align-items: center;
        padding: 6px 8px;
        border: 1px solid var(--line);
        border-radius: var(--radius-sm);
        background: var(--panel);
      }}
      .dashboard-hub-panel .command-gate span {{
        font-size: 11px;
      }}
      .dashboard-hub-panel .command-gate p {{
        font-size: 11.5px;
        -webkit-line-clamp: 1;
      }}
      .dashboard-hub-panel .hub-focus {{
        gap: 7px;
        margin: 0 -2px 9px;
        padding: 0 2px 2px;
      }}
      .dashboard-hub-panel .focus-card {{
        flex: 0 0 136px;
        min-height: 66px;
        padding: 10px 11px;
        gap: 4px;
      }}
      .dashboard-hub-panel .focus-card strong {{
        font-size: 20px;
      }}
      .dashboard-hub-panel .focus-card span {{
        font-size: 11px;
      }}
      .dashboard-hub-panel .hub-lanes {{
        display: flex;
        gap: 8px;
        margin: 0 -2px;
        overflow-x: auto;
        padding: 0 2px 2px;
        scroll-snap-type: x proximity;
        scrollbar-width: none;
      }}
      .dashboard-hub-panel .hub-lanes::-webkit-scrollbar {{
        display: none;
      }}
      .dashboard-hub-panel .lane-card {{
        flex: 0 0 min(178px, 70vw);
        min-height: 118px;
        padding: 10px;
        gap: 6px;
        scroll-snap-align: start;
      }}
      .dashboard-hub-panel .lane-card h3 {{
        font-size: 12.8px;
      }}
      .dashboard-hub-panel .lane-card p {{
        display: -webkit-box;
        overflow: hidden;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 2;
      }}
      .dashboard-hub-panel .lane-card .lane-foot {{
        display: grid;
        gap: 3px;
        font-size: 11.5px;
      }}
      .dashboard-brief-panel {{
        padding: 14px;
      }}
      .dashboard-brief-panel .panel-head {{
        gap: 8px;
        margin-bottom: 10px;
      }}
      .dashboard-brief-panel .panel-head .inline-actions {{
        width: 100%;
        flex-wrap: nowrap;
        overflow-x: auto;
        padding-bottom: 2px;
        scrollbar-width: none;
      }}
      .dashboard-brief-panel .panel-head .inline-actions::-webkit-scrollbar {{
        display: none;
      }}
      .dashboard-brief-panel .panel-head .inline-actions > * {{
        flex: 0 0 auto;
      }}
      .dashboard-brief-panel .panel-head .inline-actions button {{
        min-height: 32px;
        padding-inline: 10px;
        font-size: 12px;
      }}
      .dashboard-brief-panel .brief-list {{
        display: flex;
        gap: 8px;
        margin: 0 -2px;
        overflow-x: auto;
        padding: 0 2px 2px;
        scroll-snap-type: x proximity;
        scrollbar-width: none;
      }}
      .dashboard-brief-panel .brief-list::-webkit-scrollbar {{
        display: none;
      }}
      .dashboard-brief-panel .brief-row {{
        flex: 0 0 min(216px, 78vw);
        grid-template-columns: 1fr;
        gap: 8px;
        align-content: start;
        min-height: 164px;
        padding: 10px;
        scroll-snap-align: start;
      }}
      .dashboard-brief-panel .brief-row strong {{
        display: -webkit-box;
        margin: 5px 0 3px;
        overflow: hidden;
        font-size: 13px;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 2;
      }}
      .dashboard-brief-panel .brief-row span:not(.status-pill) {{
        display: -webkit-box;
        overflow: hidden;
        font-size: 11.5px;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 2;
      }}
      .dashboard-brief-panel .brief-row .brief-action {{
        align-self: end;
        justify-self: start;
        padding: 4px 7px;
        border: 1px solid var(--line);
        border-radius: 999px;
        background: var(--primary-soft);
        font-size: 11.5px;
        text-align: left;
      }}
      #tab-dashboard .dashboard-work-strip,
      #tab-dashboard .dashboard-side {{
        display: flex;
        gap: 8px;
        margin: 0 -2px 10px;
        overflow-x: auto;
        overflow-y: hidden;
        padding: 0 2px 3px;
        scroll-snap-type: x proximity;
        scrollbar-width: none;
      }}
      #tab-dashboard .dashboard-work-strip::-webkit-scrollbar,
      #tab-dashboard .dashboard-side::-webkit-scrollbar {{
        display: none;
      }}
      #tab-dashboard .dashboard-work-strip > .panel,
      #tab-dashboard .dashboard-side > .panel {{
        flex: 0 0 min(292px, 82vw);
        max-height: 288px;
        margin-top: 0;
        overflow: auto;
        scroll-snap-align: start;
      }}
      #tab-dashboard .dashboard-work-strip > .dashboard-brief-panel,
      #tab-dashboard .dashboard-side > .dashboard-queue-panel {{
        flex-basis: min(320px, 86vw);
      }}
      #tab-dashboard .dashboard-work-strip .panel-head,
      #tab-dashboard .dashboard-side .panel-head {{
        gap: 8px;
        margin-bottom: 8px;
      }}
      .dashboard-playbook-panel,
      .dashboard-report-panel,
      .dashboard-log-panel {{
        padding: 14px;
      }}
      .dashboard-playbook-panel .panel-head,
      .dashboard-report-panel .panel-head {{
        gap: 8px;
        margin-bottom: 8px;
      }}
      .dashboard-playbook-panel .panel-head .inline-actions,
      .dashboard-report-panel .panel-head .inline-actions {{
        width: 100%;
        flex-wrap: nowrap;
        overflow-x: auto;
        padding-bottom: 2px;
        scrollbar-width: none;
      }}
      .dashboard-playbook-panel .panel-head .inline-actions::-webkit-scrollbar,
      .dashboard-report-panel .panel-head .inline-actions::-webkit-scrollbar {{
        display: none;
      }}
      .dashboard-playbook-panel .panel-head .inline-actions button,
      .dashboard-report-panel .panel-head .inline-actions button {{
        flex: 0 0 auto;
        min-height: 32px;
        padding-inline: 10px;
        font-size: 12px;
      }}
      .dashboard-playbook-panel .ops-report-preview.is-collapsed,
      .dashboard-report-panel .ops-report-preview.is-collapsed {{
        max-height: 72px !important;
      }}
      .dashboard-report-panel .handoff-list {{
        max-height: 72px;
        margin-top: 8px;
        overflow: auto;
      }}
      .dashboard-log-panel .log {{
        min-height: 130px;
        max-height: 150px;
      }}
      .diagnostics-panel {{
        padding: 14px;
      }}
      .diagnostics-panel .panel-head {{
        gap: 9px;
        margin-bottom: 8px;
      }}
      .diagnostics-panel .panel-head .inline-actions {{
        width: 100%;
        justify-content: flex-start;
        flex-wrap: nowrap;
        overflow-x: auto;
        padding-bottom: 2px;
        scrollbar-width: none;
      }}
      .diagnostics-panel .panel-head .inline-actions::-webkit-scrollbar {{
        display: none;
      }}
      .diagnostics-panel .panel-head .inline-actions button {{
        flex: 0 0 auto;
        min-height: 32px;
        padding-inline: 10px;
        font-size: 12px;
      }}
      .diagnostics-panel .diagnostic-summary {{
        display: flex;
        gap: 7px;
        margin: 8px -2px 8px;
        overflow-x: auto;
        padding: 0 2px 2px;
        scrollbar-width: none;
      }}
      .diagnostics-panel .diagnostic-summary::-webkit-scrollbar {{
        display: none;
      }}
      .diagnostics-panel .diagnostic-count {{
        flex: 0 0 94px;
        min-height: 52px;
        padding: 9px 10px;
      }}
      .diagnostics-panel .diagnostic-count strong {{
        margin-top: 3px;
        font-size: 17px;
      }}
      .diagnostics-panel .table-wrap {{
        display: none;
      }}
      .diagnostics-panel .diagnostic-card-list {{
        display: grid;
        gap: 8px;
      }}
      .diagnostics-panel .diagnostic-card {{
        padding: 10px;
      }}
      .diagnostics-panel .diagnostic-card span:not(.status-pill),
      .diagnostics-panel .diagnostic-next {{
        display: -webkit-box;
        overflow: hidden;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 2;
      }}
      .missing-panel {{
        padding: 14px;
      }}
      .missing-panel .panel-head {{
        gap: 9px;
        margin-bottom: 10px;
      }}
      .missing-panel .panel-head .inline-actions {{
        width: 100%;
        justify-content: flex-start;
        flex-wrap: nowrap;
        overflow-x: auto;
        padding-bottom: 2px;
        scrollbar-width: none;
      }}
      .missing-panel .panel-head .inline-actions::-webkit-scrollbar {{
        display: none;
      }}
      .missing-panel .panel-head .inline-actions button {{
        flex: 0 0 auto;
        min-height: 32px;
        padding-inline: 10px;
        font-size: 12px;
      }}
      .missing-panel .missing-filter {{
        margin-bottom: 8px;
      }}
      .missing-panel .table-wrap {{
        display: none;
      }}
      .missing-panel .pager {{
        justify-content: flex-start;
      }}
      .missing-panel .mobile-card-list {{
        display: flex;
        gap: 8px;
        margin: 0 -2px;
        overflow-x: auto;
        overflow-y: hidden;
        padding: 0 2px 3px;
        scroll-snap-type: x proximity;
        scrollbar-width: none;
      }}
      .missing-panel .mobile-card-list::-webkit-scrollbar {{
        display: none;
      }}
      .missing-card {{
        flex: 0 0 min(252px, 78vw);
        max-height: 214px;
        overflow: hidden;
        padding: 10px;
        scroll-snap-align: start;
      }}
      .notification-card-list .missing-card {{
        flex-basis: min(262px, 80vw);
        max-height: 222px;
      }}
      .missing-card-head {{
        grid-template-columns: 22px minmax(0, 1fr) auto;
      }}
      .missing-card-head > .status-pill,
      .missing-card-head > .badge {{
        justify-self: start;
      }}
      .missing-card-meta {{
        grid-column: 2 / 4;
      }}
      .missing-card-row {{
        grid-template-columns: 1fr;
        gap: 6px;
      }}
      .missing-card-list .missing-card-row {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .missing-card-message,
      .missing-card-field,
      .missing-card-title span {{
        display: -webkit-box;
        overflow: hidden;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 2;
      }}
      .missing-card-field {{
        padding: 7px 8px;
      }}
      .settings-panel .schedule-summary {{
        display: flex;
        gap: 7px;
        margin: 8px -2px 8px;
        overflow-x: auto;
        padding: 0 2px 2px;
        scrollbar-width: none;
      }}
      .settings-panel .schedule-summary::-webkit-scrollbar {{
        display: none;
      }}
      .settings-panel .schedule-summary .diagnostic-count {{
        flex: 0 0 104px;
      }}
      .report-review-panel .schedule-summary {{
        display: flex;
        gap: 7px;
        margin: 8px -2px 8px;
        overflow-x: auto;
        padding: 0 2px 2px;
        scrollbar-width: none;
      }}
      .report-review-panel .schedule-summary::-webkit-scrollbar {{
        display: none;
      }}
      .report-review-panel .schedule-summary .diagnostic-count {{
        flex: 0 0 104px;
      }}
      .table-wrap.is-compact {{
        max-height: none;
      }}
      .compact-table .inline-actions {{
        flex-wrap: wrap;
      }}
      .badge-strip {{
        max-width: 220px;
      }}
      .core-panel {{
        padding: 14px;
      }}
      .core-panel .panel-head {{
        margin-bottom: 10px;
      }}
      .core-grid {{
        display: flex;
        gap: 8px;
        margin: 0 -14px;
        padding: 0 14px 2px;
        overflow-x: auto;
        scroll-snap-type: x proximity;
        scrollbar-width: none;
      }}
      .core-grid::-webkit-scrollbar {{ display: none; }}
      .core-button {{
        flex: 0 0 min(260px, 78vw);
        min-height: 86px;
        padding: 12px;
        scroll-snap-align: start;
      }}
      .core-button .core-number {{
        width: 34px;
        height: 34px;
      }}
      .missing-action-panel {{
        grid-column: auto;
        padding: 14px;
      }}
      .missing-action-panel .stack {{
        grid-template-columns: 1fr;
      }}
      .missing-action-panel .inline-actions,
      .missing-action-panel .missing-filter,
      .missing-action-panel .selection-summary,
      .missing-action-panel .missing-action-list,
      .missing-action-panel .action-preview {{
        grid-column: auto;
      }}
      .missing-action-panel .missing-filter {{
        display: flex;
      }}
      .missing-action-panel .selection-summary {{
        display: grid;
        grid-template-columns: 1fr;
        gap: 3px;
      }}
      .missing-action-panel .selection-summary span {{
        text-align: left;
      }}
      .missing-action-panel .missing-action-list {{
        display: flex;
        gap: 8px;
        max-height: none;
        margin: 0;
        overflow-x: auto;
        overflow-y: hidden;
        padding: 0 0 3px;
        border: 0;
        background: transparent;
        scroll-snap-type: x proximity;
        scrollbar-width: none;
      }}
      .missing-action-panel .missing-action-list::-webkit-scrollbar {{
        display: none;
      }}
      .missing-action-panel .missing-action-list .selectable-row {{
        flex: 0 0 min(262px, 78vw);
        min-height: 136px;
        scroll-snap-align: start;
      }}
      .missing-action-panel .missing-action-list .empty,
      .missing-action-panel .missing-action-list .pager {{
        flex: 0 0 min(262px, 78vw);
      }}
      .missing-action-panel .missing-action-list .pager {{
        position: static;
        margin: 0;
        padding: 8px;
        border: 1px solid var(--line);
        border-radius: var(--radius-sm);
        background: var(--surface-2);
        justify-content: flex-start;
      }}
      .missing-action-panel .action-preview {{
        height: auto;
        max-height: 138px;
      }}
      .report-action-panel {{
        grid-column: auto;
      }}
      .report-action-panel .stack {{
        grid-template-columns: 1fr;
      }}
      .report-action-panel .mini-grid,
      .report-action-panel .inline-actions,
      .report-action-panel .selection-summary,
      .report-action-panel #reportTargetList,
      .report-action-panel #reportPreview {{
        grid-column: auto;
      }}
      .report-action-panel #reportTargetList {{
        max-height: none;
        overflow: visible;
      }}
      .report-action-panel #reportPreview {{
        height: auto;
        max-height: none;
      }}
      #tab-actions .action-layout {{
        gap: 10px;
      }}
      #tab-actions .core-panel,
      #tab-actions .missing-action-panel,
      #tab-actions .schedule-action-panel,
      #tab-actions .sso-action-panel,
      #tab-actions .report-action-panel {{
        padding: 12px;
      }}
      #tab-actions .core-panel .panel-head {{
        margin-bottom: 8px;
      }}
      #tab-actions .core-grid {{
        margin: 0 -12px;
        padding: 0 12px 2px;
      }}
      #tab-actions .core-button {{
        grid-template-columns: 30px minmax(0, 1fr);
        flex-basis: min(224px, 70vw);
        min-height: 72px;
        padding: 10px;
      }}
      #tab-actions .core-button .core-number {{
        width: 30px;
        height: 30px;
        font-size: 13px;
      }}
      #tab-actions .core-button span:not(.core-number) {{
        display: -webkit-box;
        overflow: hidden;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 1;
      }}
      #tab-actions #actionCommand {{
        gap: 7px;
        padding: 10px;
      }}
      #tab-actions #actionCommand .action-command-main > p {{
        -webkit-line-clamp: 1;
      }}
      #tab-actions #actionCommand .command-meta,
      #tab-actions #actionCommand .action-command-actions {{
        margin-top: 6px;
      }}
      #tab-actions #actionCommand .command-gate {{
        margin-top: 6px;
        padding: 6px 7px;
      }}
      #tab-actions #actionCommand .action-command-side .workflow-step {{
        flex-basis: min(112px, 46vw);
        min-height: 48px;
        padding: 6px 7px;
      }}
      #tab-actions #actionCommand .action-command-side .workflow-step p {{
        display: none;
      }}
      #tab-actions .action-controls,
      #tab-actions .missing-action-panel .stack,
      #tab-actions .schedule-action-panel .stack,
      #tab-actions .sso-action-panel .stack,
      #tab-actions .report-action-panel .stack {{
        gap: 8px;
      }}
      #tab-actions .action-controls {{
        display: flex;
        margin: 0 -2px;
        overflow-x: auto;
        overflow-y: hidden;
        padding: 0 2px 3px;
        scroll-snap-type: x proximity;
        scrollbar-width: none;
      }}
      #tab-actions .action-controls::-webkit-scrollbar {{
        display: none;
      }}
      #tab-actions .action-controls > .panel {{
        flex: 0 0 min(310px, 82vw);
        max-height: 636px;
        margin-top: 0;
        overflow: auto;
        scroll-snap-align: start;
      }}
      #tab-actions .action-controls > .missing-action-panel {{
        flex-basis: min(326px, 88vw);
      }}
      #tab-actions .action-controls > .report-action-panel {{
        flex-basis: min(318px, 84vw);
      }}
      #tab-actions .window-range-readonly-field {{
        display: none;
      }}
      #tab-actions .missing-action-panel .selection-summary,
      #tab-actions .report-action-panel .selection-summary {{
        min-height: 32px;
        padding: 7px 9px;
      }}
      #tab-actions .missing-action-panel .missing-action-list .selectable-row {{
        flex-basis: min(232px, 72vw);
        min-height: 112px;
        padding: 8px;
      }}
      #tab-actions .missing-action-panel .missing-action-list .empty,
      #tab-actions .missing-action-panel .missing-action-list .pager {{
        flex-basis: min(232px, 72vw);
      }}
      #tab-actions .missing-action-panel .action-preview {{
        max-height: 96px;
        padding: 8px 9px;
      }}
      #tab-actions .schedule-action-panel textarea.large {{
        min-height: 104px;
      }}
      #tab-actions .schedule-action-panel .action-preview,
      #tab-actions .sso-action-panel .action-preview {{
        min-height: 54px;
        max-height: 64px;
        padding: 8px 9px;
      }}
      #tab-actions .sso-action-panel .sso-fields,
      #tab-actions .report-action-panel .mini-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 8px;
      }}
      #tab-actions .report-action-panel #reportTargetList {{
        max-height: 96px;
        overflow: auto;
        padding: 6px;
      }}
      #tab-actions .report-action-panel #reportPreview {{
        min-height: 56px;
        max-height: 72px;
        padding: 8px 9px;
      }}
      .ops-command {{
        grid-template-columns: 1fr;
      }}
      .action-command {{
        grid-template-columns: 1fr;
      }}
      .workflow-steps {{
        grid-template-columns: 1fr;
      }}
      #tab-memo .memo-grid {{
        display: flex;
        gap: 8px;
        margin: 0 -2px;
        overflow-x: auto;
        overflow-y: hidden;
        padding: 0 2px 3px;
        scroll-snap-type: x proximity;
        scrollbar-width: none;
      }}
      #tab-memo .memo-grid::-webkit-scrollbar {{
        display: none;
      }}
      #tab-memo .memo-grid > .panel {{
        flex: 0 0 min(326px, 88vw);
        max-height: 600px;
        margin-top: 0;
        overflow: auto;
        scroll-snap-align: start;
      }}
      #tab-memo .memo-grid .panel-head {{
        gap: 8px;
        margin-bottom: 8px;
      }}
      #tab-reports .report-review-strip,
      #tab-reports .exam-form-strip,
      #tab-actions .action-controls,
      #tab-dashboard .dashboard-work-strip,
      #tab-dashboard .dashboard-side,
      #tab-settings .settings-panel-strip,
      #tab-data .data-panel-strip,
      #tab-memo .memo-grid,
      .core-grid,
      .dashboard-hub-panel .hub-focus,
      .dashboard-hub-panel .hub-lanes,
      .dashboard-brief-panel .brief-list,
      .dashboard-queue-panel .queue-card-strip,
      .missing-panel .mobile-card-list,
      .missing-action-panel .missing-action-list {{
        -webkit-overflow-scrolling: touch;
        overscroll-behavior-inline: contain;
        scroll-padding-inline: 2px;
      }}
      #tab-reports .report-review-strip > *,
      #tab-reports .exam-form-strip > *,
      #tab-actions .action-controls > *,
      #tab-dashboard .dashboard-work-strip > *,
      #tab-dashboard .dashboard-side > *,
      #tab-settings .settings-panel-strip > *,
      #tab-data .data-panel-strip > *,
      #tab-memo .memo-grid > *,
      .core-grid > *,
      .dashboard-hub-panel .hub-focus > *,
      .dashboard-hub-panel .hub-lanes > *,
      .dashboard-brief-panel .brief-list > *,
      .dashboard-queue-panel .queue-card-strip > *,
      .missing-panel .mobile-card-list > *,
      .missing-action-panel .missing-action-list > * {{
        scroll-snap-stop: always;
      }}
      .student-brief-grid {{
        grid-template-columns: 1fr;
      }}
      .queue-row {{
        grid-template-columns: 1fr;
      }}
      .queue-panel {{
        max-height: none;
        overflow: visible;
      }}
      .queue-list {{
        overflow: visible;
        padding-right: 0;
      }}
      .dashboard-queue-panel {{
        padding: 14px;
      }}
      .dashboard-queue-panel .panel-head {{
        gap: 9px;
        margin-bottom: 8px;
      }}
      .dashboard-queue-panel .panel-head .inline-actions {{
        width: 100%;
        justify-content: flex-start;
        flex-wrap: nowrap;
        overflow-x: auto;
        padding-bottom: 2px;
        scrollbar-width: none;
      }}
      .dashboard-queue-panel .panel-head .inline-actions::-webkit-scrollbar {{
        display: none;
      }}
      .dashboard-queue-panel .panel-head .inline-actions button {{
        flex: 0 0 auto;
        min-height: 32px;
        padding-inline: 10px;
        font-size: 12px;
      }}
      .dashboard-queue-panel .queue-filter {{
        margin-bottom: 8px;
      }}
      .dashboard-queue-panel .queue-list {{
        display: grid;
        gap: 8px;
        overflow: visible;
        padding-right: 0;
      }}
      .dashboard-queue-panel .queue-card-strip {{
        display: flex;
        gap: 8px;
        margin: 0;
        overflow-x: auto;
        overflow-y: hidden;
        padding: 0 0 3px;
        scroll-snap-type: x proximity;
        scrollbar-width: none;
      }}
      .dashboard-queue-panel .queue-card-strip::-webkit-scrollbar {{
        display: none;
      }}
      .dashboard-queue-panel .queue-row {{
        flex: 0 0 min(252px, 78vw);
        min-height: 166px;
        padding: 10px;
        scroll-snap-align: start;
      }}
      .dashboard-queue-panel .queue-row strong {{
        display: -webkit-box;
        overflow: hidden;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 2;
      }}
      .dashboard-queue-panel .queue-row > div:first-child > span:not(.status-pill) {{
        display: -webkit-box;
        overflow: hidden;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 2;
      }}
      .dashboard-queue-panel .queue-details {{
        display: none;
      }}
      .dashboard-queue-panel .queue-badges {{
        margin-top: 6px;
        padding-bottom: 2px;
      }}
      .dashboard-queue-panel .queue-badges .badge {{
        max-width: 178px;
      }}
      .dashboard-queue-panel .queue-actions {{
        align-items: flex-end;
        gap: 6px;
        margin-top: 6px;
      }}
      .dashboard-queue-panel .queue-actions button {{
        min-height: 30px;
        padding-inline: 9px;
        font-size: 11.5px;
      }}
      .dashboard-queue-panel .queue-action {{
        min-width: 0;
        max-width: 132px;
        padding: 4px 7px;
        border: 1px solid var(--line);
        border-radius: 999px;
        background: var(--primary-soft);
        font-size: 11.5px;
        text-align: left;
        overflow: hidden;
        text-overflow: ellipsis;
      }}
      .dashboard-queue-panel .pager {{
        justify-content: flex-start;
      }}
      .queue-actions {{
        display: flex;
        align-items: center;
        justify-content: space-between;
      }}
      .queue-actions button {{
        width: auto;
      }}
      .queue-action {{
        text-align: right;
      }}
      .missing-command .workflow-steps,
      .schedule-command .workflow-steps,
      .report-command .workflow-steps,
      .data-command .workflow-steps,
      .agent-command .workflow-steps,
      .settings-command .workflow-steps,
      .exam-command .workflow-steps {{
        grid-template-columns: 1fr;
      }}
      .agent-prompt-grid {{
        grid-template-columns: 1fr;
      }}
      .hub-focus {{
        display: flex;
        gap: 10px;
        overflow-x: auto;
        padding-bottom: 2px;
        scrollbar-width: none;
      }}
      .hub-focus::-webkit-scrollbar {{ display: none; }}
      .focus-card {{
        flex: 0 0 172px;
      }}
      .workspace main {{ padding: 16px; }}
      .status-strip {{ margin: -16px -16px 16px; }}
      dl {{ grid-template-columns: 1fr; gap: 3px; }}
    }}
    @media (max-width: 1200px) and (min-width: 921px) {{
      .action-controls {{ grid-template-columns: 1fr; }}
      .missing-action-panel {{ grid-column: auto; }}
      .report-action-panel {{ grid-column: auto; }}
      .report-action-panel .stack {{ grid-template-columns: 1fr; }}
      .actions.compact {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 560px) {{
      .quick-run-overlay {{
        padding: 16px;
      }}
      .student-brief-panel {{
        width: 100vw;
      }}
      .student-brief-head {{
        padding: 14px;
      }}
      .student-brief-body {{
        padding: 12px 14px 16px;
      }}
      .quick-run-panel {{
        max-height: calc(100vh - 32px);
      }}
      .quick-run-head {{
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
      }}
      .quick-run-head h2 {{
        grid-column: 1 / -1;
      }}
      .quick-run-list {{
        max-height: calc(100vh - 170px);
      }}
      .panel-head {{
        align-items: flex-start;
        flex-direction: column;
      }}
      .inline-actions {{
        width: 100%;
      }}
      .inline-actions button,
      .inline-actions label {{
        flex: 1 1 auto;
      }}
      .readiness-row {{
        grid-template-columns: 1fr;
      }}
      .readiness-fix {{
        max-width: none;
        text-align: left;
      }}
      .segmented {{
        display: grid;
        grid-template-columns: repeat(5, minmax(64px, 1fr));
        width: 100%;
        overflow-x: auto;
      }}
      .dashboard-hub-panel .panel-head {{
        flex-direction: row;
        align-items: center;
      }}
      .dashboard-hub-panel .panel-head .inline-actions {{
        width: auto;
      }}
      .dashboard-hub-panel .panel-head .inline-actions button {{
        flex: 0 0 auto;
        min-height: 32px;
        padding-inline: 10px;
        font-size: 12px;
      }}
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
        <button class="tab-button active" data-tab="dashboard" aria-current="page"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg><span class="label" data-short="홈">대시보드</span></button>
        <button class="tab-button" data-tab="actions"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z"/></svg><span class="label" data-short="액션">액션</span></button>
        <button class="tab-button" data-tab="data"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg><span class="label" data-short="데이터">데이터</span></button>
        <div class="nav-sep">학생</div>
        <button class="tab-button" data-tab="missing"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.3 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.3a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/></svg><span class="label" data-short="미제출">미제출</span></button>
        <button class="tab-button" data-tab="schedule"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4.5" width="18" height="17" rx="2.5"/><path d="M3 9h18M8 2.5v4M16 2.5v4"/></svg><span class="label" data-short="일정">스케줄</span></button>
        <button class="tab-button" data-tab="reports"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h6M8 17h8"/></svg><span class="label" data-short="리포트">리포트</span></button>
        <div class="nav-sep">도구</div>
        <button class="tab-button" data-tab="memo"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg><span class="label" data-short="메모">메모·AI</span></button>
        <button class="tab-button" data-tab="settings"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/></svg><span class="label" data-short="설정">설정</span></button>
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
          <button data-action="openQuickRun" class="secondary quick-run-button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m16.5 16.5 4 4"/></svg><span>빠른 실행</span></button>
          <span id="notifyModePill" class="status-pill {notify_mode_class}" title="현재 notify 출력 모드"><span class="dot" style="background:currentColor"></span>{notify_mode} 모드</span>
          <button class="icon-btn" title="다크 모드" aria-label="다크 모드 전환" data-action="toggleTheme"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/></svg></button>
          <button data-action="refreshStatus" class="secondary">상태 새로고침</button>
          <div class="topbar-user">
            <span class="avatar">{brand_initial}</span>
            <span class="rl">운영자</span>
          </div>
        </div>
      </header>
      <main>
    <section class="status-strip">
      <div class="metric"><span>수신 원본</span><strong id="incomingCount">0</strong></div>
      <div class="metric warn"><span>숙제 미제출</span><strong id="missingCount">0</strong></div>
      <div class="metric danger"><span>연락처 없음</span><strong id="noPhoneCount">0</strong></div>
      <div class="metric info"><span>문구 생성</span><strong id="dryRunCount">0</strong></div>
      <div class="metric ok"><span>발송 완료</span><strong id="sentCount">0</strong></div>
      <div class="metric danger"><span>발송 실패</span><strong id="failedCount">0</strong></div>
    </section>
    <section id="tab-dashboard" class="tab-panel active">
      <section class="grid">
        <div>
          <div class="panel dashboard-hub-panel">
            <div class="panel-head">
              <h2>오늘의 운영 허브</h2>
              <div class="inline-actions">
                <button data-action="refreshOpsHub" class="secondary">허브 새로고침</button>
              </div>
            </div>
            <div id="opsCommand" class="ops-command"></div>
            <div id="opsFocus" class="hub-focus"></div>
            <div id="opsLanes" class="hub-lanes"></div>
          </div>

          <div class="dashboard-work-strip">
            <div class="panel dashboard-brief-panel">
              <div class="panel-head">
                <h2>오늘의 운영 브리핑</h2>
                <div class="inline-actions">
                  <span class="badge">AI 운영 순서</span>
                  <button data-action="generateOpsReport" class="secondary">운영 리포트 만들기</button>
                  <button data-action="generateOpsPlaybook" class="secondary">실행계획 만들기</button>
                </div>
              </div>
              <div id="opsBrief" class="brief-list"></div>
            </div>

            <div class="panel dashboard-playbook-panel">
              <div class="panel-head">
                <h2>오늘의 자동화 실행계획</h2>
                <div class="inline-actions">
                  <button data-action="generateOpsPlaybook" class="secondary">다시 만들기</button>
                  <button data-action="copyOpsPlaybook" class="secondary">복사</button>
                  <button data-toggle-preview="#opsPlaybookPreview" class="secondary">펼치기</button>
                </div>
              </div>
              <div id="opsPlaybookSummary" class="schedule-summary"></div>
              <div id="opsPlaybookSteps" class="playbook-list"></div>
              <div id="opsPlaybookPreview" class="action-preview ops-report-preview is-collapsed">실행계획을 만들면 설정 점검, 데이터 확인, 미제출 알림, 리포트 보강, 마감 리포트 순서가 정리됩니다.</div>
            </div>

            <div class="panel dashboard-report-panel">
              <div class="panel-head">
                <h2>오늘의 운영 리포트</h2>
                <div class="inline-actions">
                  <button data-action="generateOpsReport" class="secondary">다시 만들기</button>
                  <button data-action="saveOpsHandoff" class="secondary">저장</button>
                  <button data-action="refreshOpsHandoffs" class="secondary">최근 기록</button>
                  <button data-action="copyOpsReport" class="secondary">복사</button>
                  <button data-toggle-preview="#opsReportPreview" class="secondary">펼치기</button>
                </div>
              </div>
              <div id="opsReportPreview" class="action-preview ops-report-preview is-collapsed">운영 리포트를 만들면 숙제 알림, ClassIn 데이터, 학원 데이터 융합, 리포트 품질 상태가 Markdown으로 정리됩니다.</div>
              <div id="opsHandoffList" class="handoff-list">
                <div class="empty compact">최근 저장한 운영 기록이 없습니다.</div>
              </div>
            </div>
          </div>

        </div>
        <aside class="dashboard-side">
          <div class="panel queue-panel dashboard-queue-panel">
            <div class="panel-head">
              <h2>오늘 처리할 학생</h2>
              <div class="inline-actions">
                <button data-action="focusMissingCore" class="secondary">미제출 처리</button>
                <button data-action="focusReportCore" class="secondary">리포트 생성</button>
              </div>
            </div>
            <div id="opsQueueFilters" class="segmented queue-filter" aria-label="학생 큐 필터"></div>
            <div id="opsQueue" class="queue-list"></div>
          </div>

          <div class="panel diagnostics-panel">
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

          <div class="panel dashboard-log-panel">
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
            <button data-action="focusMissingCore" data-flow="missing" class="core-button active">
              <span class="core-number">1</span>
              <strong>숙제 미제출 문자 발송</strong>
              <span>조회 후 선택한 학부모에게 발송</span>
            </button>
            <button data-action="focusScheduleCore" data-flow="schedule" class="core-button">
              <span class="core-number">2</span>
              <strong>스케줄표로 수업·숙제 생성</strong>
              <span>CSV/표 붙여넣기, 검토 후 ClassIn 생성</span>
            </button>
            <button data-action="focusReportCore" data-flow="report" class="core-button">
              <span class="core-number">3</span>
              <strong>반별 리포트 생성</strong>
              <span>시험 점수, 숙제, 출결을 모아 초안 생성</span>
            </button>
          </div>
        </div>

        <div id="actionCommand" class="action-command"></div>

        <div class="action-controls">
          <div class="panel missing-action-panel">
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
                <label class="window-range-readonly-field">현재 기준<input id="windowRangeReadonly" type="text" value="오늘 0시 이후" readonly></label>
              </div>
              <div class="inline-actions">
                <button data-action="refreshMissing" class="secondary">미제출 조회</button>
                <button data-action="selectAllMissing" class="secondary">전체 선택</button>
                <button data-action="clearMissingSelection" class="secondary">선택 해제</button>
	                <button data-action="previewMissingHomeworkSms" class="secondary">문구 미리보기</button>
	                <button data-action="sendMissingHomeworkSms">선택 문자 발송</button>
	              </div>
	              <div class="segmented missing-filter" aria-label="미제출 상태 필터"></div>
	              <div id="missingSelectionSummary" class="selection-summary">조회 후 발송 대상을 선택하세요.</div>
	              <div id="missingSelectionList" class="selectable-list missing-action-list">
	                <div class="empty">미제출 조회를 누르면 선택 목록이 표시됩니다.</div>
              </div>
              <div id="actionPreview" class="action-preview">미제출 학생을 조회하면 발송 대상과 현재 발송 모드가 정리됩니다.</div>
            </div>
          </div>

          <div class="panel schedule-action-panel">
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

          <div class="panel sso-action-panel">
            <div class="panel-head">
          <h2>ClassIn 앱 접속 링크</h2>
          <span class="badge">앱 링크</span>
            </div>
            <div class="stack">
              <div class="sso-fields">
                <label>사용자 식별번호<input id="ssoUid" type="text" autocomplete="off" placeholder="예: 10001"></label>
                <label>강좌 ID<input id="ssoCourseId" type="text" autocomplete="off" placeholder="ClassIn 강좌 번호"></label>
                <label>수업 ID<input id="ssoClassId" type="text" autocomplete="off" placeholder="ClassIn 수업 번호"></label>
                <label>전화번호<input id="ssoTelephone" type="tel" autocomplete="off"></label>
              </div>
              <div class="sso-fields">
                <label>기기<select id="ssoDeviceType">
                  <option value="1">PC/Mac</option>
                  <option value="2">iOS</option>
                  <option value="3">Android</option>
                </select></label>
                <label>유효 시간(초)<input id="ssoLifeTime" type="number" min="60" max="604800" step="60" value="86400"></label>
              </div>
              <div class="inline-actions">
                <button data-action="createSsoLink">접속 링크 생성</button>
                <button data-action="copySsoLink" class="secondary">링크 복사</button>
              </div>
              <div id="ssoPreview" class="action-preview">사용자 식별번호, 강좌 ID, 수업 ID, 전화번호를 입력한 뒤 접속 링크를 생성하세요.</div>
            </div>
          </div>

          <div class="panel report-action-panel">
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

    <section id="tab-data" class="tab-panel">
      <div id="dataCommand" class="action-command data-command"></div>

      <div class="data-panel-strip">
        <div class="panel data-inbox-panel">
          <div class="panel-head">
            <h2>ClassIn 수신 데이터</h2>
            <div class="inline-actions">
              <label>표시 개수<input id="webhookLimit" type="number" min="1" max="100" step="1" value="30"></label>
              <button data-action="refreshWebhookInbox" class="secondary">수신함 새로고침</button>
            </div>
          </div>
          <div id="webhookInboxSummary" class="schedule-summary"></div>
          <div id="webhookCmdSummary" class="selection-summary"></div>
          <div id="webhookInboxTable">
            <div class="empty">수신함을 새로고침하면 ClassIn 이벤트가 표시됩니다.</div>
          </div>
        </div>

        <div class="panel data-context-panel">
          <div class="panel-head">
            <h2>학원 데이터 융합</h2>
            <div class="inline-actions">
              <label>반 선택<select id="contextClassName"><option value="">전체 반</option></select></label>
              <button data-action="refreshAcademyContexts" class="secondary">융합 맥락 새로고침</button>
            </div>
          </div>
          <div id="academyContextSummary" class="schedule-summary"></div>
          <div id="academyContextReview" class="selection-summary"></div>
          <div id="academyContextTable">
            <div class="empty">융합 맥락을 새로고침하면 학생별 자체 데이터 연결 상태가 표시됩니다.</div>
          </div>
        </div>
      </div>
    </section>

    <section id="tab-schedule" class="tab-panel">
      <div id="scheduleCommand" class="action-command schedule-command"></div>

      <div class="panel neis-exam-panel">
        <div class="panel-head">
          <div>
            <h2>NEIS 학교별 시험일정 예시</h2>
            <p>학교명으로 NEIS schoolInfo 코드를 찾고 SchoolSchedule 학사일정에서 시험·고사 일정을 뽑아 ClassIn 운영 일정에 붙이는 예시입니다.</p>
          </div>
          <span class="badge">세종 5개교 · 2026-06-26 기준</span>
        </div>
        <div class="neis-exam-meta">
          <span class="badge">교육청 I10</span>
          <span class="badge">범위 2026-03-01~2027-02-28</span>
          <span class="badge">정기고사 키워드 필터</span>
        </div>
        <div class="table-wrap">
          <table class="neis-exam-table">
            <thead>
              <tr>
                <th>학교</th>
                <th>NEIS 코드</th>
                <th>1학기 주요 시험</th>
                <th>2학기 주요 시험</th>
                <th>ClassIn 연결 예시</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td><strong>종촌고등학교</strong><span class="school-address">달빛1로 60</span></td>
                <td><span class="badge">9300180</span></td>
                <td>
                  <div class="exam-lines">
                    <div class="exam-line"><span class="exam-label danger">1회고사</span><span class="exam-date">4/22~4/28</span></div>
                    <div class="exam-line"><span class="exam-label warn">2회고사</span><span class="exam-date">6/24~6/30</span></div>
                  </div>
                </td>
                <td>
                  <div class="exam-lines">
                    <div class="exam-line"><span class="exam-label danger">2학기 1회</span><span class="exam-date">10/1~10/8</span></div>
                    <div class="exam-line"><span class="exam-label warn">2학기 2회</span><span class="exam-date">12/7~12/11</span></div>
                  </div>
                </td>
                <td>시험 3주 전 대비반 시작, 시험 후 결과 CSV import</td>
              </tr>
              <tr>
                <td><strong>세종대성고등학교</strong><span class="school-address">다솜로 22</span></td>
                <td><span class="badge">9300278</span></td>
                <td>
                  <div class="exam-lines">
                    <div class="exam-line"><span class="exam-label danger">1회 정기</span><span class="exam-date">4/27~4/30</span></div>
                    <div class="exam-line"><span class="exam-label warn">2회 정기</span><span class="exam-date">6/26~7/2</span></div>
                  </div>
                </td>
                <td>
                  <div class="exam-lines">
                    <div class="exam-line"><span class="exam-label danger">1회 정기</span><span class="exam-date">10/2~10/8</span></div>
                    <div class="exam-line"><span class="exam-label warn">2회 정기</span><span class="exam-date">12/8~12/11</span></div>
                  </div>
                </td>
                <td>학교별 일정에 맞춰 단원 숙제·오답 리마인드 자동 생성</td>
              </tr>
              <tr>
                <td><strong>다정고등학교</strong><span class="school-address">다정남로 15</span></td>
                <td><span class="badge">9300235</span></td>
                <td>
                  <div class="exam-lines">
                    <div class="exam-line"><span class="exam-label danger">1회고사</span><span class="exam-date">4/23~4/28</span></div>
                    <div class="exam-line"><span class="exam-label warn">2회고사</span><span class="exam-date">6/30~7/3</span></div>
                  </div>
                </td>
                <td>
                  <div class="exam-lines">
                    <div class="exam-line"><span class="exam-label danger">1회고사</span><span class="exam-date">10/1~10/7</span></div>
                    <div class="exam-line"><span class="exam-label warn">2회고사</span><span class="exam-date">12/8~12/11</span></div>
                  </div>
                </td>
                <td>학교 일정 차이를 반별 캘린더와 리포트 타임라인에 반영</td>
              </tr>
              <tr>
                <td><strong>새롬고등학교</strong><span class="school-address">새롬서로 68</span></td>
                <td><span class="badge">9300219</span></td>
                <td>
                  <div class="exam-lines">
                    <div class="exam-line"><span class="exam-label danger">1회고사</span><span class="exam-date">4/27~4/30</span></div>
                    <div class="exam-line"><span class="exam-label warn">2회고사</span><span class="exam-date">6/22~6/26</span></div>
                  </div>
                </td>
                <td>
                  <div class="exam-line"><span class="exam-label info">미적재</span><span>조회 시점 기준 NEIS에 2학기 정기고사 미표시</span></div>
                </td>
                <td>미적재 일정은 수동 검수 큐에 올리고 추후 재조회</td>
              </tr>
              <tr>
                <td><strong>한솔고등학교</strong><span class="school-address">누리로 34</span></td>
                <td><span class="badge">9300058</span></td>
                <td>
                  <div class="exam-lines">
                    <div class="exam-line"><span class="exam-label danger">1회고사</span><span class="exam-date">4/20~4/24</span></div>
                    <div class="exam-line"><span class="exam-label warn">2회고사</span><span class="exam-date">6/22~6/26</span></div>
                  </div>
                </td>
                <td>
                  <div class="exam-lines">
                    <div class="exam-line"><span class="exam-label danger">1회고사</span><span class="exam-date">10/1~10/8</span></div>
                    <div class="exam-line"><span class="exam-label warn">2회고사</span><span class="exam-date">12/7~12/11</span></div>
                  </div>
                </td>
                <td>같은 시험 주간의 학교별 선후행 차이를 과제 마감에 반영</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="panel schedule-panel">
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
      <div id="missingCommand" class="action-command missing-command"></div>

      <div class="stack">
        <div>
          <div class="panel missing-panel missing-list-panel">
            <div class="panel-head">
              <h2>미제출 목록</h2>
              <div class="inline-actions">
                <button data-action="focusMissingCore" class="secondary">문자 발송으로 이동</button>
                <button data-action="refreshMissing" class="secondary">미제출 조회</button>
	                <button data-action="exportMissingCsv" class="secondary">CSV 내보내기</button>
	              </div>
	            </div>
	            <div class="segmented missing-filter" aria-label="미제출 상태 필터"></div>
	            <div id="missingTable" style="margin-top:12px"></div>
	          </div>

          <div class="panel missing-panel notification-panel">
            <h2>알림 발송 현황</h2>
            <div id="notificationTable"></div>
          </div>
        </div>
      </div>
    </section>

    <section id="tab-reports" class="tab-panel">
      <div id="reportCommand" class="action-command report-command"></div>

      <div class="report-review-strip">
        <div class="panel report-overview-panel">
          <div class="panel-head">
            <h2>리포트</h2>
            <button data-action="focusReportCore" class="secondary">반별 리포트로 이동</button>
          </div>
          <div class="inline-actions">
            <label>일자<input id="dailyDate" type="date" value="{today}"></label>
            <button data-action="renderDaily">일일 현황 생성</button>
            <button data-action="generateWeekly" class="secondary">전체 주간 드래프트</button>
            <button data-action="focusReportCore" class="secondary">반별 리포트 선택</button>
            <button data-action="focusExamImport" class="secondary">시험 가져오기</button>
            <button data-action="focusAnswerSheet" class="secondary">OMR 생성</button>
          </div>
        </div>

        <div class="panel report-review-panel report-composition-panel">
          <div class="panel-head">
            <h2>개별 리포트 구성</h2>
            <div class="inline-actions">
              <button data-action="refreshReportCompositions" class="secondary">구성 새로고침</button>
              <button data-action="focusReportCore" class="secondary">대상 선택</button>
            </div>
          </div>
          <div id="reportCompositionSummary" class="schedule-summary"></div>
          <div id="reportCompositionTable">
            <div class="empty">아직 조회된 리포트 구성이 없습니다.</div>
          </div>
        </div>

        <div class="panel student-report-panel">
          <div class="panel-head">
            <h2>개별 리포트 초안</h2>
            <div class="inline-actions">
              <button data-action="generateStudentReportPack" class="secondary">첫 학생 초안</button>
              <button data-action="copyStudentReportPack" class="secondary">복사</button>
              <button data-toggle-preview="#studentReportPreview" class="secondary">펼치기</button>
            </div>
          </div>
          <div id="studentReportPreview" class="action-preview student-report-preview is-collapsed">개별 리포트 구성에서 학생별 초안을 만들 수 있습니다.</div>
        </div>

        <div class="panel report-review-panel weekly-draft-panel">
          <div class="panel-head">
            <h2>주간 드래프트 검토</h2>
            <div class="inline-actions">
              <label class="checkbox-field">차단 포함<input id="forceBlockedQuality" type="checkbox"></label>
              <button data-action="refreshWeeklyDrafts" class="secondary">드래프트 새로고침</button>
              <button data-action="approveWeekly" class="secondary">승인</button>
            </div>
          </div>
          <div id="weeklyDraftSummary" class="schedule-summary"></div>
          <div id="weeklyDraftTable">
            <div class="empty">드래프트를 생성하거나 새로고침하면 품질 큐가 표시됩니다.</div>
          </div>
        </div>
      </div>

      <div id="examCommand" class="action-command exam-command"></div>

      <div class="exam-form-strip">
        <div id="answerSheetPanel" class="panel exam-form-panel">
          <div class="panel-head">
            <h2>OMR 답안지 생성</h2>
            <span class="badge">생성 전 검토</span>
          </div>
          <div class="stack">
            <div class="actions compact">
              <label>강좌 ID<input id="answerSheetCourseId" type="text" placeholder="ClassIn 강좌 번호"></label>
              <label>단원 ID<input id="answerSheetUnitId" type="text" placeholder="ClassIn 단원 번호"></label>
              <label>활동명<input id="answerSheetName" type="text" value="OMR 답안지"></label>
              <label>교사 식별번호<input id="answerSheetTeacherUid" type="text" placeholder="ClassIn 교사 번호"></label>
            </div>
            <div class="actions compact exam-date-grid">
              <label>시작<input id="answerSheetStart" type="datetime-local"></label>
              <label>종료<input id="answerSheetEnd" type="datetime-local"></label>
            </div>
            <div class="actions compact exam-toggle-grid">
              <label class="checkbox-field">검토 모드<input id="answerSheetDryRun" type="checkbox" checked></label>
              <label class="checkbox-field">게시<input id="answerSheetRelease" type="checkbox"></label>
            </div>
            <div class="inline-actions">
              <button data-action="createAnswerSheet">OMR 답안지 생성</button>
            </div>
            <div id="answerSheetPreview" class="action-preview">강좌 ID와 단원 ID를 입력하고 검토 모드로 먼저 생성 내용을 확인하세요.</div>
          </div>
        </div>

        <div id="examImportPanel" class="panel exam-form-panel">
          <div class="panel-head">
            <h2>시험 결과 가져오기</h2>
            <span class="badge">CSV</span>
          </div>
          <div class="stack">
            <div class="actions compact">
              <label>시험명<input id="examName" type="text"></label>
              <label>시험일<input id="examDate" type="date" value="{today}"></label>
              <label>반<input id="examClassName" type="text"></label>
              <label class="checkbox-field">검토 모드<input id="examDryRun" type="checkbox" checked></label>
            </div>
            <input id="examFile" type="file" accept=".csv,.tsv,.txt">
            <label class="exam-csv-input">시험 CSV<textarea id="examCsvText" class="large"></textarea></label>
            <div class="inline-actions">
              <button data-action="readExamFile" class="secondary">파일 읽기</button>
              <button data-action="downloadExamTemplate" class="secondary">시험 템플릿</button>
              <button data-action="importExamResults">시험 결과 가져오기</button>
            </div>
            <div id="examPreview" class="action-preview">시험 CSV를 붙여넣고 먼저 검토 모드로 학생 매칭을 확인하세요.</div>
          </div>
        </div>
      </div>
    </section>

    <section id="tab-memo" class="tab-panel">
      <div id="agentCommand" class="action-command agent-command"></div>
      <div class="grid memo-grid">
        <div class="panel">
          <div class="panel-head">
            <h2>AI 질문</h2>
            <span class="badge">수동 오더</span>
          </div>
          <div class="stack">
            <div class="agent-prompt-grid">
              <button type="button" class="secondary" data-agent-prompt="이번 주 숙제 미제출 학생을 우선순위로 정리해줘">미제출 우선순위</button>
              <button type="button" class="secondary" data-agent-prompt="리포트 blocked 학생과 보강할 근거를 알려줘">리포트 보강</button>
              <button type="button" class="secondary" data-agent-prompt="학원 데이터 매칭 확인이 필요한 학생을 요약해줘">데이터 매칭</button>
            </div>
            <label>질문<textarea id="agentQuestion" class="large" placeholder="예: 이번 주 숙제 미제출 학생을 우선순위로 정리해줘"></textarea></label>
            <div class="inline-actions">
              <button data-action="askAgent">질문 보내기</button>
              <button data-action="focusMemoTarget" class="secondary">학생 메모</button>
            </div>
            <div id="agentAnswerPreview" class="action-preview">AI 답변이 여기에 표시됩니다.</div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h2>학생 메모</h2>
            <span class="badge">Notion</span>
          </div>
          <div class="stack">
            <div class="actions">
              <label>ClassIn ID<input id="memoClassinId" type="text" placeholder="10001"></label>
              <label>태그<input id="memoTag" type="text" placeholder="상담"></label>
            </div>
            <label>내용<textarea id="memoText" class="large" placeholder="상담 내용, 다음 액션, 보호자 연락 맥락을 남기세요."></textarea></label>
            <div class="inline-actions">
              <button data-action="writeMemo">메모 저장</button>
              <button data-action="focusMemoText" class="secondary">내용 입력</button>
              <button data-action="focusAgentQuestion" class="secondary">AI 질문</button>
            </div>
            <div id="memoPreview" class="action-preview">저장 후 대상 학생과 태그가 여기에 정리됩니다.</div>
          </div>
        </div>
      </div>
    </section>

    <section id="tab-settings" class="tab-panel">
      <div id="readinessCommand" class="action-command settings-command"></div>
      <div class="settings-panel-strip">
        <div class="panel settings-panel">
          <div class="panel-head">
            <h2>운영 전환 체크리스트</h2>
            <div class="inline-actions">
              <button data-action="setReadinessMode" data-readiness-mode="local-demo" class="secondary active">데모</button>
              <button data-action="setReadinessMode" data-readiness-mode="classin-live" class="secondary">ClassIn 실연동</button>
              <button data-action="setReadinessMode" data-readiness-mode="kakao-live" class="secondary">카톡 실발송</button>
              <button data-action="refreshReadiness" class="secondary">다시 점검</button>
            </div>
          </div>
          <div id="readinessSummary" class="schedule-summary"></div>
          <div id="readinessList" class="readiness-list">
            <div class="empty compact">운영 전환 단계를 선택해 준비 상태를 확인하세요.</div>
          </div>
        </div>

        <div class="panel settings-panel">
          <div class="panel-head">
            <h2>Notion DB 설계 미리보기</h2>
            <div class="inline-actions">
              <label>Prefix<input id="notionSchemaPrefix" type="text" value="{title}"></label>
              <button data-action="loadNotionSchema" class="secondary">미리보기</button>
              <button data-action="copyNotionSchema" class="secondary">복사</button>
            </div>
          </div>
          <div id="notionSchemaSummary" class="schedule-summary"></div>
          <div id="notionSchemaList" class="schema-list">
            <div class="empty compact">미리보기를 누르면 생성될 학생·수업·리포트·메모·시험 DB 속성이 표시됩니다.</div>
          </div>
          <div id="notionSchemaCommand" class="action-preview schema-command is-collapsed">DB 생성 전 검토 명령과 config.yaml 조각이 여기에 표시됩니다.</div>
        </div>

        <div class="panel settings-panel">
          <div class="panel-head">
            <h2>파일럿 브링업</h2>
            <div class="inline-actions">
              <button data-action="loadPilotBrief" class="secondary">브리프 갱신</button>
              <button data-action="copyPilotBrief" class="secondary">복사</button>
              <button data-toggle-preview="#pilotBriefPreview" class="secondary">펼치기</button>
            </div>
          </div>
          <div id="pilotBriefSummary" class="schedule-summary"></div>
          <div id="pilotBriefList" class="readiness-list">
            <div class="empty compact">브리프를 갱신하면 Cloudflare, DataSub, Windows 상시 구동 체크리스트가 표시됩니다.</div>
          </div>
          <div id="pilotBriefPreview" class="action-preview schema-command is-collapsed">DataSub 신청 메일과 실행 명령이 여기에 표시됩니다.</div>
        </div>

        <div class="panel settings-panel">
          <h2>설정</h2>
          <dl id="settings"></dl>
        </div>
      </div>
    </section>
      </main>
    </div>
  </div>

  <div id="quickRunOverlay" class="quick-run-overlay" hidden>
    <div class="quick-run-panel" role="dialog" aria-modal="true" aria-labelledby="quickRunTitle">
      <div class="quick-run-head">
        <h2 id="quickRunTitle">빠른 실행</h2>
        <input id="quickRunSearch" class="quick-run-search" type="search" placeholder="업무 검색" autocomplete="off">
        <button data-action="closeQuickRun" class="icon-btn" aria-label="닫기"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg></button>
      </div>
      <div id="quickRunList" class="quick-run-list"></div>
    </div>
  </div>

  <div id="studentBriefOverlay" class="student-brief-overlay" hidden>
    <aside id="studentBriefPanel" class="student-brief-panel" role="dialog" aria-modal="true" aria-labelledby="studentBriefTitle">
      <div class="student-brief-head">
        <div>
          <h2 id="studentBriefTitle">학생 360</h2>
          <span id="studentBriefSubtitle">숙제·시험·리포트 근거를 한곳에서 확인합니다.</span>
        </div>
        <button data-action="closeStudentBrief" class="icon-btn" aria-label="닫기"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg></button>
      </div>
      <div id="studentBriefBody" class="student-brief-body"></div>
    </aside>
  </div>

  <div id="toastWrap" class="toast-wrap" aria-live="polite"></div>

  <script id="initial-status" type="application/json">{status_json}</script>
  <script>
    const log = document.querySelector("#log");
    const statusNode = document.querySelector("#initial-status");
    let status = JSON.parse(statusNode.textContent);
    let opsHub = null;
    let currentOpsPlaybook = null;
    let diagnostics = null;
    let diagnosticsCacheAt = 0;
    const DIAGNOSTICS_TTL_MS = 12000;
    const PAGE_SIZE = {{
      opsQueue: 5,
      opsPlaybook: 3,
      webhookInbox: 6,
      academyContexts: 6,
      schedule: 8,
      missing: 5,
      notifications: 8,
      reportTargets: 5,
      reportCompositions: 5,
      weeklyDrafts: 6,
      readiness: 8,
      pilotBrief: 4,
    }};
    let opsQueuePage = 1;
    let opsQueueFilter = "all";
    let opsPlaybookPage = 1;
    let webhookInboxPage = 1;
    let academyContextPage = 1;
    let schedulePage = 1;
    let missingPage = 1;
    let notificationPage = 1;
    let reportTargetPage = 1;
    let reportCompositionPage = 1;
    let weeklyDraftPage = 1;
    let readinessPage = 1;
    let pilotBriefPage = 1;
    let currentSchedule = [];
    let currentScheduleSummary = {{}};
    let currentScheduleBlocked = false;
    let currentMissing = [];
    let missingFilter = "all";
    let currentMissingSummary = {{}};
    let currentMissingBlocked = false;
    let selectedMissingKeys = new Set();
    let lastMissingPreview = null;
    let currentReportTargets = [];
    let selectedReportIds = new Set();
    let currentNotifications = [];
    let currentWeeklyDrafts = [];
    let currentWeeklyDraftsExists = false;
    let currentReportCompositions = [];
    let currentWebhookEvents = [];
    let currentWebhookSummary = {{}};
    let currentAcademyContexts = [];
    let currentAcademyContextSummary = {{}};
    let currentAcademyReviewItems = [];
    let currentReadinessItems = [];
    let currentReadinessSummary = {{}};
    let currentReadinessReady = false;
    let currentReadinessDemo = false;
    let currentReadinessMode = "local-demo";
    let currentNotionSchema = null;
    let lastNotionSchemaText = "";
    let currentPilotBrief = null;
    let lastPilotBriefText = "";
    let academyContextClassesLoaded = false;
    let reportClassesLoaded = false;
    let lastSsoLink = "";
    let lastMaskedSsoLink = "";
    let lastOpsReportMarkdown = "";
    let currentOpsHandoffs = [];
    let lastOpsPlaybookMarkdown = "";
    let lastStudentReportMarkdown = "";
    let lastStudentReportId = "";
    let currentStudentBrief = null;
    let currentExamFlow = "exam";
    let lastExamImportResult = null;
    let lastAnswerSheetResult = null;

    function logLabel(key) {{
      return ({{
        total_lessons: "수업",
        total_student_rows: "학생 기록",
        late: "지각",
        absent: "결석",
        homework_missing: "미제출",
        total_missing: "미제출",
        needs_message: "연락 필요",
        needs_retry: "재시도",
        needs_phone: "연락처 보완",
        students_with_context: "학생 맥락",
        data_needs_review: "데이터 확인",
        report_blocked: "리포트 차단",
        report_review: "리포트 확인",
        report_ready: "승인 가능",
        count: "건수",
        rows: "행",
        chars: "글자",
      }})[key] || key.replaceAll("_", " ");
    }}

    function compactLogDetail(data) {{
      if (!data) return "";
      if (typeof data !== "object") return String(data);
      const source = data.summary && typeof data.summary === "object" ? data.summary : data;
      const parts = Object.entries(source)
        .filter(([, value]) => ["string", "number", "boolean"].includes(typeof value))
        .slice(0, 6)
        .map(([key, value]) => `${{logLabel(key)}}: ${{value}}`);
      return parts.join(" · ");
    }}

    function evidenceLabel(value) {{
      const text = String(value || "").trim();
      if (!text) return "-";
      if (text.startsWith("demo://weekly") || text.includes("/weekly/")) return "리포트 초안";
      if (text.startsWith("demo://ops")) return "운영 기록";
      if (text.startsWith("demo://")) return "데모 자료";
      if (/\\.html?$/i.test(text)) return "리포트 파일";
      if (/^https?:\\/\\//i.test(text)) return "외부 링크";
      return text;
    }}

    function writeLog(message, data) {{
      const now = new Date().toLocaleTimeString();
      const detailText = compactLogDetail(data);
      const detail = detailText ? "\\n" + detailText : "";
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
      const notifyPill = document.querySelector("#notifyModePill");
      const notify = output.notify_mode || "-";
      notifyPill.className = `status-pill ${{notify === "live" ? "sent" : notify === "dry_run" ? "dry_run" : "warn"}}`;
      notifyPill.innerHTML = `<span class="dot" style="background:currentColor"></span>${{escapeHtml(notifyModeLabel(notify))}}`;
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
        dry_run: "외부 발송 없음",
        sent: "발송 완료",
        failed: "실패",
        ok: "정상",
        warn: "확인",
        info: "정보",
        missing: "누락",
        skipped: "건너뜀",
        ready: "준비됨",
        review: "확인 필요",
        blocked: "차단",
        approved: "승인됨",
      }};
      return labels[status] || status || "-";
    }}

    function statusPill(status) {{
      const safe = escapeHtml(status || "pending");
      return `<span class="status-pill ${{safe}}">${{escapeHtml(statusLabel(status))}}</span>`;
    }}

    function qualityLabel(status) {{
      const labels = {{
        ready: "통과",
        review: "확인 필요",
        blocked: "발송 제외",
      }};
      return labels[status] || statusLabel(status);
    }}

    function qualityPill(status) {{
      const safe = escapeHtml(status || "review");
      return `<span class="status-pill ${{safe}}">${{escapeHtml(qualityLabel(status))}}</span>`;
    }}

    function qualitySummaryText(ready, review, blocked) {{
      return `통과 ${{escapeHtml(ready || 0)}} · 확인 ${{escapeHtml(review || 0)}} · 제외 ${{escapeHtml(blocked || 0)}}`;
    }}

    function paged(items, page, size) {{
      const totalPages = Math.max(1, Math.ceil((items || []).length / size));
      const safePage = Math.min(Math.max(1, page || 1), totalPages);
      const start = (safePage - 1) * size;
      return {{
        items: (items || []).slice(start, start + size),
        page: safePage,
        totalPages,
        totalItems: (items || []).length,
      }};
    }}

    function pagerHtml(kind, page, totalPages, totalItems) {{
      if (totalPages <= 1) return "";
      const pages = [];
      for (let value = 1; value <= totalPages; value += 1) {{
        if (totalPages > 9 && value > 1 && value < totalPages && Math.abs(value - page) > 2) {{
          if (pages[pages.length - 1] !== "gap") pages.push("gap");
          continue;
        }}
        pages.push(value);
      }}
      return `
        <div class="pager" aria-label="페이지 이동">
          <button type="button" class="secondary" data-page-kind="${{escapeHtml(kind)}}" data-page-value="${{page - 1}}" ${{page <= 1 ? "disabled" : ""}}>이전</button>
          ${{pages.map((value) => value === "gap"
            ? `<span class="pager-count">...</span>`
            : `<button type="button" class="secondary ${{value === page ? "active" : ""}}" data-page-kind="${{escapeHtml(kind)}}" data-page-value="${{value}}">${{escapeHtml(value)}}</button>`
          ).join("")}}
          <button type="button" class="secondary" data-page-kind="${{escapeHtml(kind)}}" data-page-value="${{page + 1}}" ${{page >= totalPages ? "disabled" : ""}}>다음</button>
          <span class="pager-count">${{escapeHtml(page)}}/${{escapeHtml(totalPages)}} · ${{escapeHtml(totalItems)}}건</span>
        </div>
      `;
    }}

    function setPreviewCollapsed(selector, collapsed) {{
      const target = document.querySelector(selector);
      if (!target) return;
      target.classList.toggle("is-collapsed", collapsed);
      target.classList.toggle("is-expanded", !collapsed);
      document.querySelectorAll(`[data-toggle-preview="${{selector}}"]`).forEach((button) => {{
        button.textContent = collapsed ? "펼치기" : "접기";
      }});
    }}

    function setPreviewText(selector, text) {{
      const target = document.querySelector(selector);
      if (!target) return;
      target.textContent = text || "";
      setPreviewCollapsed(selector, true);
    }}

    function setPagedList(kind, value) {{
      const page = Number(value || 1);
      if (kind === "opsQueue") {{
        opsQueuePage = page;
        renderOpsQueue((opsHub || {{}}).work_queue || []);
      }} else if (kind === "opsPlaybook") {{
        opsPlaybookPage = page;
        renderOpsPlaybookSteps((currentOpsPlaybook || {{}}).steps || []);
      }} else if (kind === "webhookInbox") {{
        webhookInboxPage = page;
        renderWebhookInboxTable();
      }} else if (kind === "academyContexts") {{
        academyContextPage = page;
        renderAcademyContextTable();
      }} else if (kind === "schedule") {{
        schedulePage = page;
        renderScheduleTable();
      }} else if (kind === "missing") {{
        missingPage = page;
        renderMissingSelectionList();
        renderMissingTable();
      }} else if (kind === "notifications") {{
        notificationPage = page;
        renderNotificationTable();
      }} else if (kind === "reportTargets") {{
        reportTargetPage = page;
        renderReportTargetList();
      }} else if (kind === "reportCompositions") {{
        reportCompositionPage = page;
        renderReportCompositionTable();
      }} else if (kind === "weeklyDrafts") {{
        weeklyDraftPage = page;
        renderWeeklyDraftTable();
      }} else if (kind === "readiness") {{
        readinessPage = page;
        renderReadinessList();
      }} else if (kind === "pilotBrief") {{
        pilotBriefPage = page;
        renderPilotBriefList();
      }}
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

    async function loadOpsHub() {{
      const params = new URLSearchParams();
      params.set("window_hours", document.querySelector("#windowHours").value || "24");
      const lessonId = document.querySelector("#lessonId").value.trim();
      if (lessonId) params.set("lesson_id", lessonId);
      const response = await fetch(`/api/ops-hub?${{params.toString()}}`);
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || data.message || "ops hub load failed");
      }}
      renderOpsHub(data);
      return data;
    }}

    function renderOpsHub(data) {{
      opsHub = data;
      renderOpsCommand(data);
      const focus = data.focus || [];
      document.querySelector("#opsFocus").innerHTML = focus.map((item) => `
        <div class="focus-card ${{escapeHtml(item.tone || "info")}}" data-hub-tab="${{escapeHtml(item.tab || "dashboard")}}" role="button" tabindex="0">
          <span>${{escapeHtml(item.label || "-")}}</span>
          <strong>${{escapeHtml(item.count ? item.count : "OK")}}</strong>
          <span>${{escapeHtml(item.detail || "")}}</span>
        </div>
      `).join("");

      const lanes = data.lanes || [];
      document.querySelector("#opsLanes").innerHTML = lanes.map((lane) => `
        <div class="lane-card">
          <div>${{statusPill(laneStateToStatus(lane.state))}}</div>
          <h3>${{escapeHtml(lane.title || "-")}}</h3>
          <p>${{escapeHtml(lane.label || "")}}</p>
          <div class="lane-foot">
            <span>${{escapeHtml(lane.metric || "")}}</span>
            <span>${{escapeHtml(lane.next_action || "")}}</span>
          </div>
        </div>
      `).join("");

      const brief = data.ops_brief || [];
      const briefTarget = document.querySelector("#opsBrief");
      if (!brief.length) {{
        briefTarget.innerHTML = `<div class="empty">오늘 실행할 운영 브리핑이 없습니다.</div>`;
      }} else {{
        briefTarget.innerHTML = brief.map((item) => `
          <div class="brief-row" data-hub-tab="${{escapeHtml(item.tab || "dashboard")}}" role="button" tabindex="0">
            <div>
              <div>${{statusPill(toneToStatus(item.tone))}}</div>
              <strong>${{escapeHtml(item.title || "-")}}</strong>
              <span>${{escapeHtml(item.detail || "")}}</span>
              <span>${{escapeHtml(item.lane || "운영 허브")}} · 근거 ${{escapeHtml(evidenceLabel(item.evidence))}}</span>
            </div>
            <div class="brief-action">${{escapeHtml(item.action_label || "확인")}}</div>
          </div>
        `).join("");
      }}

      renderOpsQueue(data.work_queue || []);
    }}

    function renderOpsCommand(data) {{
      const target = document.querySelector("#opsCommand");
      if (!target) return;
      const brief = (data.ops_brief || [])[0] || null;
      const queue = (data.work_queue || [])[0] || null;
      const readyQueue = (data.work_queue || [])
        .filter((item) => item.execution_state === "ready").length;
      const reviewQueue = Math.max(0, (data.work_queue || []).length - readyQueue);
      if (!brief && !queue) {{
        target.className = "ops-command ok";
        target.innerHTML = `
          <div class="ops-command-main">
            <span class="command-label">오늘 먼저 볼 것</span>
            <strong>긴급 처리 항목 없음</strong>
            <p>현재 데모 기준으로 바로 막힌 운영 항목은 없습니다. 데이터 수신과 리포트 품질만 주기적으로 확인하세요.</p>
            <div class="command-meta">
              <span class="badge ok">안정</span>
              <span class="badge">학생 큐 0명</span>
            </div>
          </div>
          <div class="ops-command-side">
            <span class="command-label">다음 액션</span>
            <strong>운영 리포트 확인</strong>
            <p>마감 기록을 남기면 다음 교대자가 같은 상태에서 이어받을 수 있습니다.</p>
            <button class="secondary command-action" data-hub-tab="dashboard">리포트 만들기</button>
          </div>
          <div class="command-gate"><span>안전 게이트</span><p>실제 발송이나 ClassIn 쓰기 작업 전에는 설정 점검과 검토 결과를 확인하세요.</p></div>
        `;
        return;
      }}
      const tone = brief?.tone || queue?.tone || "info";
      const title = brief?.title || queue?.reason || "운영 항목 확인";
      const detail = brief?.detail || queue?.reason || "오늘 처리할 운영 항목을 확인하세요.";
      const actionLabel = brief?.action_label || queue?.action_label || queue?.next_action || "확인";
      const actionTab = brief?.tab || queue?.tab || "dashboard";
      const studentName = queue?.student_name || "학생 큐 없음";
      const studentDetail = queue
        ? `${{queue.class_name || "-"}} · ${{queue.reason || queue.next_action || "확인 필요"}}`
        : "현재 바로 처리할 학생 큐가 없습니다.";
      const safetyGate = queue?.safety_gate || "실행 전 설정·연락처·품질 상태를 확인하세요.";
      target.className = `ops-command ${{escapeHtml(tone)}}`;
      target.innerHTML = `
        <div class="ops-command-main">
          <span class="command-label">오늘 먼저 볼 것</span>
          <strong>${{escapeHtml(title)}}</strong>
          <p>${{escapeHtml(detail)}}</p>
          <div class="command-meta">
            ${{statusPill(toneToStatus(tone))}}
            <span class="badge">근거 ${{escapeHtml(evidenceLabel(brief?.evidence || queue?.evidence))}}</span>
            <span class="badge ok">처리 가능 ${{escapeHtml(readyQueue)}}</span>
            <span class="badge ${{reviewQueue ? "warn" : ""}}">검토 필요 ${{escapeHtml(reviewQueue)}}</span>
          </div>
        </div>
        <div class="ops-command-side">
          <span class="command-label">다음 학생</span>
          <strong>${{escapeHtml(studentName)}}</strong>
          <p>${{escapeHtml(studentDetail)}}</p>
          <button class="secondary command-action" data-hub-tab="${{escapeHtml(actionTab)}}">${{escapeHtml(actionLabel)}}</button>
        </div>
        <div class="command-gate"><span>안전 게이트</span><p>${{escapeHtml(safetyGate)}}</p></div>
      `;
    }}

    function renderOpsQueue(queue) {{
      const target = document.querySelector("#opsQueue");
      renderOpsQueueFilters(queue);
      if (!queue.length) {{
        target.innerHTML = `<div class="empty">오늘 바로 처리할 학생 큐가 없습니다.</div>`;
        return;
      }}
      const filtered = queueFilterItems(queue);
      if (!filtered.length) {{
        target.innerHTML = `<div class="empty">${{escapeHtml(queueFilterEmptyText())}}</div>`;
        return;
      }}
      const page = paged(filtered, opsQueuePage, PAGE_SIZE.opsQueue);
      opsQueuePage = page.page;
      const rowsHtml = page.items.map((item) => `
        <div class="queue-row ${{escapeHtml(item.tone || "info")}}" data-hub-tab="${{escapeHtml(item.tab || "missing")}}" role="button" tabindex="0">
          <div>
            <div>${{statusPill(toneToStatus(item.tone))}}</div>
            <strong>${{escapeHtml(item.student_name || "미등록")}} · ${{escapeHtml(item.class_name || "-")}}</strong>
            <span>${{escapeHtml(item.reason || "")}}</span>
            <span>${{escapeHtml(item.lane || "운영 허브")}} · 근거 ${{escapeHtml(evidenceLabel(item.evidence))}}</span>
            <details class="queue-details">
              <summary>게이트·완료 기준</summary>
              <span class="queue-note">게이트: ${{escapeHtml(item.safety_gate || "담당자 확인 후 진행")}}</span>
              <span class="queue-note">완료: ${{escapeHtml(item.completion_check || "운영 리포트에 처리 결과 기록")}}</span>
            </details>
              <div class="queue-badges">
                <span class="badge ${{item.execution_state === "ready" ? "ok" : "warn"}}">${{escapeHtml(item.operator_note || (item.can_execute ? "바로 실행 가능" : "검토 필요"))}}</span>
                ${{(item.badges || []).map((badge) => `<span class="badge">${{escapeHtml(badge)}}</span>`).join("")}}
              </div>
            </div>
          <div class="queue-actions">
            <button
              type="button"
              data-action="openStudentBrief"
              data-student-id="${{escapeHtml(item.student_classin_id || studentIdFromQueueId(item.id) || "")}}"
              data-student-name="${{escapeHtml(item.student_name || "")}}"
              data-class-name="${{escapeHtml(item.class_name || "")}}"
              class="secondary"
            >학생 보기</button>
            <div class="queue-action">${{escapeHtml(item.next_action || item.action_label || "확인")}}</div>
          </div>
        </div>
      `).join("");
      target.innerHTML = `<div class="queue-card-strip">${{rowsHtml}}</div>` + pagerHtml("opsQueue", page.page, page.totalPages, page.totalItems);
    }}

    function queueFilterDefinitions(queue) {{
      const counts = queueFilterCounts(queue);
      return [
        ["all", "전체", counts.all],
        ["ready", "바로 실행", counts.ready],
        ["review", "검토 필요", counts.review],
        ["missing", "미제출", counts.missing],
        ["report", "리포트", counts.report],
        ["data", "데이터", counts.data],
      ];
    }}

    function queueFilterCounts(queue) {{
      return (queue || []).reduce((counts, item) => {{
        counts.all += 1;
        if (item.execution_state === "ready") counts.ready += 1;
        if (item.execution_state !== "ready") counts.review += 1;
        if (item.kind === "missing_homework") counts.missing += 1;
        if (["weekly_report", "report_composition"].includes(item.kind)) counts.report += 1;
        if (item.kind === "data_review") counts.data += 1;
        return counts;
      }}, {{ all: 0, ready: 0, review: 0, missing: 0, report: 0, data: 0 }});
    }}

    function queueFilterItems(queue) {{
      return (queue || []).filter((item) => {{
        if (opsQueueFilter === "ready") return item.execution_state === "ready";
        if (opsQueueFilter === "review") return item.execution_state !== "ready";
        if (opsQueueFilter === "missing") return item.kind === "missing_homework";
        if (opsQueueFilter === "report") return ["weekly_report", "report_composition"].includes(item.kind);
        if (opsQueueFilter === "data") return item.kind === "data_review";
        return true;
      }});
    }}

    function queueFilterEmptyText() {{
      const labels = {{
        ready: "바로 실행 가능한 학생 큐가 없습니다.",
        review: "검토가 필요한 학생 큐가 없습니다.",
        missing: "미제출 처리 학생 큐가 없습니다.",
        report: "리포트 보강 학생 큐가 없습니다.",
        data: "데이터 확인 학생 큐가 없습니다.",
      }};
      return labels[opsQueueFilter] || "선택한 조건의 학생 큐가 없습니다.";
    }}

    function renderOpsQueueFilters(queue) {{
      const target = document.querySelector("#opsQueueFilters");
      if (!target) return;
      const definitions = queueFilterDefinitions(queue || []);
      if (!definitions.some(([, , count]) => count > 0)) {{
        target.innerHTML = "";
        return;
      }}
      target.innerHTML = definitions.map(([key, label, count]) => `
        <button
          type="button"
          data-queue-filter="${{escapeHtml(key)}}"
          class="${{opsQueueFilter === key ? "active" : ""}}"
        >${{escapeHtml(label)}} ${{escapeHtml(count)}}</button>
      `).join("");
    }}

    function studentBriefOverlay() {{
      return document.querySelector("#studentBriefOverlay");
    }}

    function studentIdFromQueueId(value) {{
      const text = String(value || "");
      const match = text.match(/:(\\d+)(?:::|$)/);
      return match ? match[1] : "";
    }}

    function studentBriefSeedFromButton(button) {{
      return {{
        student_classin_id: button?.dataset.studentId || "",
        student_name: button?.dataset.studentName || "",
        class_name: button?.dataset.className || "",
      }};
    }}

    function studentMatches(item, seed) {{
      if (!item || !seed) return false;
      const seedId = String(seed.student_classin_id || "").trim();
      const itemId = String(item.student_classin_id || studentIdFromQueueId(item.id) || "").trim();
      if (seedId && itemId && seedId === itemId) return true;
      const seedName = String(seed.student_name || "").trim();
      const itemName = String(item.student_name || "").trim();
      const seedClass = String(seed.class_name || "").trim();
      const itemClass = String(item.class_name || item.student_class_name || "").trim();
      if (!seedName || !itemName || seedName !== itemName) return false;
      return !seedClass || !itemClass || seedClass === itemClass;
    }}

    function firstStudentMatch(items, seed) {{
      return (items || []).find((item) => studentMatches(item, seed)) || null;
    }}

    function studentBriefBadges(...groups) {{
      const seen = new Set();
      return groups.flat().filter((badge) => {{
        const text = String(badge || "").trim();
        if (!text || seen.has(text)) return false;
        seen.add(text);
        return true;
      }}).slice(0, 8);
    }}

    function studentBriefSourceRows(context) {{
      const sources = (context?.sources || []).slice(0, 5);
      if (!sources.length) {{
        return `<li><strong>연결된 자체 데이터 없음</strong>오프라인 출결·성적·메모가 붙으면 여기에 출처가 표시됩니다.</li>`;
      }}
      return sources.map((source) => `
        <li>
          <strong>${{escapeHtml(source.kind || "source")}} · ${{escapeHtml(source.date || source.source_name || "-")}}</strong>
          ${{escapeHtml(source.detail || source.source_name || "출처 세부 정보 없음")}}
        </li>
      `).join("");
    }}

    function studentBriefQueueRows(queueItems) {{
      if (!queueItems.length) {{
        return `<li><strong>오늘 큐 항목 없음</strong>현재 이 학생에게 바로 연결된 처리 항목은 없습니다.</li>`;
      }}
      return queueItems.slice(0, 4).map((item) => `
        <li>
          <strong>${{escapeHtml(item.next_action || item.action_label || "확인")}} · ${{escapeHtml(item.lane || "운영")}}</strong>
          ${{escapeHtml(item.reason || item.safety_gate || "상태 확인")}}<br>
          <span class="badge ${{item.execution_state === "ready" ? "ok" : "warn"}}">${{escapeHtml(item.operator_note || item.execution_state || "검토")}}</span>
        </li>
      `).join("");
    }}

    function studentBriefSectionRows(composition) {{
      const sections = (composition?.sections || []).slice(0, 6);
      if (!sections.length) {{
        return `<li><strong>리포트 구성 미조회</strong>리포트 탭에서 구성 새로고침을 누르면 섹션 준비도가 연결됩니다.</li>`;
      }}
      return sections.map((section) => `
        <li>
          <strong>${{escapeHtml(section.title || "섹션")}} · ${{escapeHtml(statusLabel(section.status))}}</strong>
          ${{escapeHtml(section.detail || section.reason || "준비도 확인")}}
        </li>
      `).join("");
    }}

    function openStudentBrief(button) {{
      const seed = studentBriefSeedFromButton(button);
      const fallback = firstStudentMatch((opsHub || {{}}).work_queue || [], seed)
        || firstStudentMatch(currentAcademyContexts, seed)
        || firstStudentMatch(currentReportCompositions, seed)
        || firstStudentMatch(currentMissing, seed)
        || seed;
      const identity = {{
        student_classin_id: seed.student_classin_id || fallback.student_classin_id || studentIdFromQueueId(fallback.id) || "",
        student_name: seed.student_name || fallback.student_name || "미등록",
        class_name: seed.class_name || fallback.class_name || fallback.student_class_name || "",
      }};
      if (!identity.student_classin_id && !identity.student_name) {{
        throw new Error("학생 정보를 찾을 수 없습니다.");
      }}

      const queueItems = ((opsHub || {{}}).work_queue || []).filter((item) => studentMatches(item, identity));
      const context = firstStudentMatch(currentAcademyContexts, identity);
      const composition = firstStudentMatch(currentReportCompositions, identity);
      const missingRows = currentMissing.filter((item) => studentMatches(item, identity));
      const weeklyDraft = firstStudentMatch(currentWeeklyDrafts, identity);
      const latestNotification = firstStudentMatch(currentNotifications, identity);
      const primary = queueItems[0] || {{}};
      const counts = context?.counts || {{}};
      const compositionCounts = composition?.source_counts || {{}};
      const examCount = (Number(compositionCounts.exam_results || 0) + Number(compositionCounts.offline_scores || counts.offline_scores || 0));
      const memoCount = Number(compositionCounts.memos || counts.memos || 0);
      const attendanceCount = Number(counts.offline_attendance || 0);
      const missingCount = missingRows.reduce((max, item) => Math.max(max, Number(item.missing_count || 1)), 0);
      const reportStatus = composition?.readiness_status || weeklyDraft?.quality_status || context?.weekly_report?.status || "-";
      const title = identity.student_name || "학생";
      const subtitle = [identity.class_name, identity.student_classin_id ? `ClassIn ${{identity.student_classin_id}}` : ""]
        .filter(Boolean)
        .join(" · ") || "학생 식별 정보 없음";
      const badges = studentBriefBadges(
        primary.badges || [],
        context?.badges || [],
        composition?.readiness_warnings || [],
        latestNotification?.quality_warnings || []
      );

      currentStudentBrief = identity;
      document.querySelector("#studentBriefTitle").textContent = `${{title}} 360`;
      document.querySelector("#studentBriefSubtitle").textContent = subtitle;
      document.querySelector("#studentBriefBody").innerHTML = `
        <div class="student-brief-hero">
          <strong>${{escapeHtml(primary.reason || composition?.focus || context?.summary || "학생 상태 확인")}}</strong>
          <p>${{escapeHtml(primary.safety_gate || composition?.context_summary || context?.summary || "숙제·시험·메모·리포트 근거를 함께 확인하세요.")}}</p>
          <div class="student-brief-meta">
            ${{statusPill(toneToStatus(primary.tone || composition?.readiness_status || "info"))}}
            <span class="badge">${{escapeHtml(reportStatus)}} 리포트</span>
            <span class="badge">${{missingRows.length ? `미제출 ${{missingRows.length}}건` : "미제출 큐 없음"}}</span>
          </div>
          <div class="student-brief-badges">
            ${{badges.length ? badges.map((badge) => `<span class="badge">${{escapeHtml(badge)}}</span>`).join("") : `<span class="badge">추가 경고 없음</span>`}}
          </div>
          <div class="student-brief-actions">
            <button data-action="studentBriefAskAgent">AI 질문</button>
            <button data-action="studentBriefMemo" class="secondary" ${{identity.student_classin_id ? "" : "disabled"}}>메모 남기기</button>
            <button data-action="studentBriefReport" class="secondary" ${{identity.student_classin_id ? "" : "disabled"}}>리포트 초안</button>
            <button data-action="studentBriefData" class="secondary">데이터 확인</button>
          </div>
        </div>
        <div class="student-brief-grid">
          <div class="student-brief-card"><span>숙제 미제출</span><strong>${{escapeHtml(missingCount ? `${{missingCount}}회` : "없음")}}</strong></div>
          <div class="student-brief-card"><span>시험·성적</span><strong>${{escapeHtml(examCount)}}건</strong></div>
          <div class="student-brief-card"><span>상담·메모</span><strong>${{escapeHtml(memoCount)}}건</strong></div>
          <div class="student-brief-card"><span>오프라인 출결</span><strong>${{escapeHtml(attendanceCount)}}건</strong></div>
        </div>
        <div class="student-brief-section">
          <h3>오늘 해야 할 일</h3>
          <ul class="student-brief-list">${{studentBriefQueueRows(queueItems)}}</ul>
        </div>
        <div class="student-brief-section">
          <h3>학원 데이터 출처</h3>
          <ul class="student-brief-list">${{studentBriefSourceRows(context)}}</ul>
        </div>
        <div class="student-brief-section">
          <h3>리포트 구성</h3>
          <ul class="student-brief-list">${{studentBriefSectionRows(composition)}}</ul>
        </div>
      `;
      studentBriefOverlay().hidden = false;
      document.querySelector("#studentBriefPanel")?.focus();
    }}

    function closeStudentBrief() {{
      const overlay = studentBriefOverlay();
      if (overlay) overlay.hidden = true;
      currentStudentBrief = null;
    }}

    function studentBriefAskAgent() {{
      if (!currentStudentBrief) return;
      const brief = currentStudentBrief;
      closeStudentBrief();
      activateTab("memo");
      const target = document.querySelector("#agentQuestion");
      target.value = `${{brief.student_name}} 학생의 이번 주 숙제, 시험, 상담 메모, 리포트 보강 포인트를 근거별로 정리해줘`;
      target.focus();
      setPreviewText("#agentAnswerPreview", "학생 360 기준 질문이 준비되었습니다. AI에게 묻기를 실행하세요.");
      renderAgentCommand();
    }}

    function studentBriefMemo() {{
      if (!currentStudentBrief?.student_classin_id) return;
      const brief = currentStudentBrief;
      closeStudentBrief();
      activateTab("memo");
      document.querySelector("#memoClassinId").value = brief.student_classin_id;
      document.querySelector("#memoTag").value = "상담";
      document.querySelector("#memoText").focus();
      renderAgentCommand();
    }}

    async function studentBriefReport() {{
      if (!currentStudentBrief?.student_classin_id) return;
      const studentId = currentStudentBrief.student_classin_id;
      closeStudentBrief();
      activateTab("reports");
      await generateStudentReportPack({{ dataset: {{ studentId }} }});
    }}

    function studentBriefData() {{
      closeStudentBrief();
      activateTab("data");
      document.querySelector("#academyContextTable")?.scrollIntoView({{ block: "start", behavior: "smooth" }});
    }}

    async function generateOpsReport() {{
      const params = new URLSearchParams();
      params.set("window_hours", document.querySelector("#windowHours").value || "24");
      const lessonId = document.querySelector("#lessonId").value.trim();
      if (lessonId) params.set("lesson_id", lessonId);
      const response = await fetch(`/api/ops-report?${{params.toString()}}`);
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || data.message || "ops report failed");
      }}
      lastOpsReportMarkdown = data.markdown || "";
      setPreviewText("#opsReportPreview", lastOpsReportMarkdown || "운영 리포트 내용이 없습니다.");
      writeLog("오늘의 운영 리포트를 만들었습니다.", data.summary);
      toast("오늘의 운영 리포트를 만들었습니다.", "ok");
      return data;
    }}

    function renderOpsHandoffs(items) {{
      currentOpsHandoffs = items || [];
      const target = document.querySelector("#opsHandoffList");
      if (!target) return;
      if (!currentOpsHandoffs.length) {{
        target.innerHTML = `<div class="empty compact">최근 저장한 운영 기록이 없습니다.</div>`;
        return;
      }}
      target.innerHTML = currentOpsHandoffs.slice(0, 8).map((item) => {{
        const savedAt = formatDate(item.saved_at || item.generated_at);
        const meta = [
          `${{item.brief_items || 0}}개 브리핑`,
          `${{item.work_queue || 0}}명 큐`,
          `${{item.markdown_chars || 0}}자`,
        ].join(" · ");
        const action = item.preview_url
          ? `<a class="handoff-link" href="${{escapeHtml(item.preview_url)}}" target="_blank" rel="noopener">열기</a>`
          : `<span class="badge">${{escapeHtml(item.path && item.path.startsWith("demo://") ? "demo" : "저장됨")}}</span>`;
        return `
          <div class="handoff-row">
            <div>
              <strong>${{escapeHtml(item.filename || "운영 리포트")}}</strong>
              <span>${{escapeHtml(savedAt || "-")}} · ${{escapeHtml(meta)}}</span>
            </div>
            ${{action}}
          </div>
        `;
      }}).join("");
    }}

    function mergeOpsHandoff(item) {{
      if (!item) return;
      const next = [
        item,
        ...currentOpsHandoffs.filter((existing) => existing.filename !== item.filename),
      ];
      renderOpsHandoffs(next.slice(0, 8));
    }}

    async function refreshOpsHandoffs(logResult) {{
      const response = await fetch("/api/ops-handoffs?limit=8");
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || data.message || "ops handoff load failed");
      }}
      renderOpsHandoffs(data.items || []);
      if (logResult) {{
        writeLog("최근 운영 기록을 불러왔습니다.", {{ count: (data.items || []).length }});
      }}
      return data;
    }}

    async function saveOpsHandoff() {{
      const data = await callApi("/api/ops-handoff", {{
        window_hours: document.querySelector("#windowHours").value || "24",
        lesson_id: document.querySelector("#lessonId").value,
      }});
      lastOpsReportMarkdown = data.markdown || lastOpsReportMarkdown;
      if (lastOpsReportMarkdown) {{
        setPreviewText("#opsReportPreview", lastOpsReportMarkdown);
      }}
      mergeOpsHandoff(data.item);
      writeLog(data.message || "운영 리포트를 저장했습니다.", data.item || {{}});
      return data;
    }}

    function renderOpsPlaybook(data) {{
      currentOpsPlaybook = data;
      const summary = data.summary || {{}};
      const summaryCards = [
        ["단계", summary.total_steps || 0, ""],
        ["준비", summary.ready || 0, "ok"],
        ["확인", summary.review || 0, "warn"],
        ["차단", summary.blocked || 0, "failed"],
        ["검토", summary.dry_run || 0, "info"],
        ["분", summary.estimated_minutes || 0, "info"],
      ];
      document.querySelector("#opsPlaybookSummary").innerHTML = summaryCards.map(([label, value, tone]) => `
        <div class="diagnostic-count ${{escapeHtml(tone)}}">
          <span>${{escapeHtml(label)}}</span>
          <strong>${{escapeHtml(value)}}</strong>
        </div>
      `).join("");
      const steps = data.steps || [];
      renderOpsPlaybookSteps(steps);
      lastOpsPlaybookMarkdown = data.markdown || "";
      setPreviewText("#opsPlaybookPreview", lastOpsPlaybookMarkdown || "자동화 실행계획 내용이 없습니다.");
    }}

    function renderOpsPlaybookSteps(steps) {{
      const target = document.querySelector("#opsPlaybookSteps");
      if (!steps.length) {{
        target.innerHTML = `<div class="empty">오늘 실행할 자동화 단계가 없습니다.</div>`;
        setPreviewCollapsed("#opsPlaybookPreview", true);
      }} else {{
        const page = paged(steps, opsPlaybookPage, PAGE_SIZE.opsPlaybook);
        opsPlaybookPage = page.page;
        target.innerHTML = page.items.map((step, index) => `
          <div class="playbook-step" data-hub-tab="${{escapeHtml(step.tab || "dashboard")}}" role="button" tabindex="0">
            <div>
              <strong>${{escapeHtml((page.page - 1) * PAGE_SIZE.opsPlaybook + index + 1)}}. ${{escapeHtml(step.phase || "-")}} · ${{escapeHtml(step.title || "-")}}</strong>
              <span>${{escapeHtml(step.detail || "")}}</span>
              <span>${{escapeHtml(step.evidence || "")}}</span>
              <div class="step-meta">
                ${{statusPill(toneToStatus(step.status === "blocked" ? "danger" : step.status === "review" ? "warn" : "ok"))}}
                <span class="badge">${{escapeHtml(step.risk || "-")}}</span>
                <span class="badge">${{escapeHtml(step.owner || "-")}}</span>
                <span class="badge">${{escapeHtml(step.duration_min || 0)}}분</span>
              </div>
            </div>
            <div class="brief-action">${{escapeHtml(step.action_label || "확인")}}</div>
          </div>
        `).join("") + pagerHtml("opsPlaybook", page.page, page.totalPages, page.totalItems);
      }}
    }}

    async function generateOpsPlaybook() {{
      const params = new URLSearchParams();
      params.set("window_hours", document.querySelector("#windowHours").value || "24");
      const lessonId = document.querySelector("#lessonId").value.trim();
      if (lessonId) params.set("lesson_id", lessonId);
      const response = await fetch(`/api/ops-playbook?${{params.toString()}}`);
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || data.message || "ops playbook failed");
      }}
      opsPlaybookPage = 1;
      renderOpsPlaybook(data);
      writeLog("오늘의 자동화 실행계획을 만들었습니다.", data.summary);
      toast("자동화 실행계획을 만들었습니다.", "ok");
      return data;
    }}

    async function copyTextWithFallback(text, selector) {{
      try {{
        await navigator.clipboard.writeText(text);
        return "clipboard";
      }} catch (error) {{
        const target = document.querySelector(selector);
        if (!target) throw error;
        const range = document.createRange();
        range.selectNodeContents(target);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        if (document.execCommand && document.execCommand("copy")) {{
          return "execCommand";
        }}
        return "selected";
      }}
    }}

    function setTheme(theme) {{
      const next = theme === "dark" ? "dark" : "light";
      document.documentElement.dataset.theme = next;
      try {{
        localStorage.setItem("classin-ui-theme", next);
      }} catch (error) {{
        // localStorage may be blocked in embedded browser contexts.
      }}
    }}

    function toggleColorTheme() {{
      setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
    }}

    async function copyOpsReport() {{
      if (!lastOpsReportMarkdown) {{
        await generateOpsReport();
      }}
      if (!lastOpsReportMarkdown) {{
        throw new Error("복사할 운영 리포트가 없습니다.");
      }}
      const copyMode = await copyTextWithFallback(lastOpsReportMarkdown, "#opsReportPreview");
      writeLog("오늘의 운영 리포트를 복사했습니다.", {{
        chars: lastOpsReportMarkdown.length,
        mode: copyMode,
      }});
      toast(copyMode === "selected" ? "본문을 선택했습니다. Ctrl+C로 복사하세요." : "운영 리포트를 복사했습니다.", "ok");
    }}

    async function copyOpsPlaybook() {{
      if (!lastOpsPlaybookMarkdown) {{
        await generateOpsPlaybook();
      }}
      if (!lastOpsPlaybookMarkdown) {{
        throw new Error("복사할 자동화 실행계획이 없습니다.");
      }}
      const copyMode = await copyTextWithFallback(lastOpsPlaybookMarkdown, "#opsPlaybookPreview");
      writeLog("오늘의 자동화 실행계획을 복사했습니다.", {{
        chars: lastOpsPlaybookMarkdown.length,
        mode: copyMode,
      }});
      toast(copyMode === "selected" ? "본문을 선택했습니다. Ctrl+C로 복사하세요." : "자동화 실행계획을 복사했습니다.", "ok");
    }}

    function laneStateToStatus(state) {{
      if (state === "ready") return "ok";
      if (state === "warn") return "warn";
      if (state === "blocked") return "missing";
      return "skipped";
    }}

    function toneToStatus(tone) {{
      if (tone === "danger") return "failed";
      if (tone === "warn") return "warn";
      if (tone === "info") return "info";
      if (tone === "ok") return "ok";
      return "skipped";
    }}

    function dataMetric(value) {{
      const number = Number(value || 0);
      return Number.isFinite(number) ? number : 0;
    }}

    function hasLoadedSummary(summary) {{
      return Object.keys(summary || {{}}).length > 0;
    }}

    function dataCommandState() {{
      const webhookSummary = currentWebhookSummary || {{}};
      const academySummary = currentAcademyContextSummary || {{}};
      const totalEvents = dataMetric(webhookSummary.total || currentWebhookEvents.length);
      const parsedEvents = dataMetric(webhookSummary.parsed || currentWebhookEvents.filter((item) => item.status === "parsed").length);
      const webhookReview = dataMetric(webhookSummary.review || currentWebhookEvents.filter((item) => item.status !== "parsed").length);
      const studentEvents = dataMetric(
        webhookSummary.student_events ||
        currentWebhookEvents.reduce((sum, item) => sum + dataMetric(item.student_count), 0)
      );
      const totalStudents = dataMetric(academySummary.total_students || currentAcademyContexts.length);
      const withContext = dataMetric(
        academySummary.students_with_context ||
        currentAcademyContexts.filter((item) => item.has_context).length
      );
      const withoutContext = dataMetric(
        academySummary.students_without_context ||
        Math.max(0, totalStudents - withContext)
      );
      const contextReview = dataMetric(academySummary.needs_review || currentAcademyReviewItems.length);
      const weeklyReports = dataMetric(
        academySummary.weekly_reports ||
        currentAcademyContexts.filter((item) => item.weekly_report?.status).length
      );
      const hasWebhookData = totalEvents > 0 || hasLoadedSummary(webhookSummary);
      const hasContextData = totalStudents > 0 || hasLoadedSummary(academySummary) || currentAcademyReviewItems.length > 0;
      let phase = "수신 점검";
      let detail = "ClassIn 수신 기록을 새로고침해 출결·숙제·시험 신호가 들어왔는지 확인하세요.";
      let primaryAction = "refreshWebhookInbox";
      let primaryLabel = "수신함 새로고침";
      let tone = "";
      let activeIndex = 0;
      if (!status.ok) {{
        phase = "설정 확인";
        detail = "config.yaml을 읽은 뒤 ClassIn 수신함과 Notion 기반 학원 데이터 맥락을 확인할 수 있습니다.";
        primaryAction = "refreshStatus";
        primaryLabel = "상태 새로고침";
        tone = "danger";
      }} else if (webhookReview) {{
        phase = "수신 정리 확인";
        detail = "아직 정리되지 않은 수신 기록이 있습니다. 이벤트 종류, 수업, 학생 신호를 먼저 확인하세요.";
        tone = "warn";
        activeIndex = 1;
      }} else if (!hasWebhookData) {{
        activeIndex = 0;
      }} else if (!hasContextData) {{
        phase = "학생 매칭";
        detail = "ClassIn 원본은 들어왔습니다. 학원 출결·성적·메모 맥락을 학생별로 연결하세요.";
        primaryAction = "refreshAcademyContexts";
        primaryLabel = "융합 맥락 새로고침";
        activeIndex = 2;
      }} else if (contextReview || withoutContext) {{
        phase = "매칭 보강";
        detail = "자동 매칭이 애매하거나 자체 데이터가 비어 있는 학생이 있습니다. 리포트 반영 전 확인하세요.";
        primaryAction = "refreshAcademyContexts";
        primaryLabel = "매칭 다시 보기";
        tone = "warn";
        activeIndex = 2;
      }} else {{
        phase = "리포트 반영 준비";
        detail = "수신·파싱·학생 맥락이 연결되어 리포트와 알림 품질 근거로 사용할 수 있습니다.";
        primaryAction = "refreshAcademyContexts";
        primaryLabel = "맥락 다시 보기";
        activeIndex = 3;
      }}
      return {{
        phase,
        detail,
        primaryAction,
        primaryLabel,
        tone,
        activeIndex,
        meta: [
          ["수신 기록", `${{totalEvents}}건`],
          ["정리 완료", `${{parsedEvents}}건`],
          ["학생 신호", `${{studentEvents}}건`],
          ["확인 필요", `${{webhookReview + contextReview}}건`],
          ["학생 맥락", totalStudents ? `${{withContext}}/${{totalStudents}}명` : "0명"],
          ["리포트 근거", `${{weeklyReports}}건`],
        ],
      }};
    }}

    function renderDataCommand() {{
      const target = document.querySelector("#dataCommand");
      if (!target) return;
      const state = dataCommandState();
      target.className = `action-command data-command ${{escapeHtml(state.tone)}}`;
      const steps = [
        ["수신", "ClassIn 기록 도착 확인"],
        ["정리", "수업·학생 신호 연결"],
        ["매칭", "학생 정보와 자체 데이터 연결"],
        ["반영", "리포트·알림 품질 근거로 사용"],
      ];
      target.innerHTML = `
        <div class="action-command-main">
          <span class="command-label">데이터 운영 상태</span>
          <strong>${{escapeHtml(state.phase)}}</strong>
          <p>${{escapeHtml(state.detail)}}</p>
          <div class="command-meta">
            ${{state.meta.map(([label, value]) => `<span class="badge">${{escapeHtml(label)}} ${{escapeHtml(value)}}</span>`).join("")}}
          </div>
          <div class="action-command-actions">
            <button data-primary-action="${{escapeHtml(state.primaryAction)}}">${{escapeHtml(state.primaryLabel)}}</button>
            <button data-primary-action="refreshWebhookInbox" class="secondary">수신함</button>
            <button data-primary-action="refreshAcademyContexts" class="secondary">융합 맥락</button>
            <button data-hub-tab="reports" class="secondary">리포트 반영</button>
          </div>
          <div class="command-gate"><span>운영 원칙</span><p>ClassIn 수신 기록은 읽기 전용으로 확인하고, 자동 매칭이 애매한 학원 데이터는 리포트 문장에 반영하기 전 교사가 확인하세요.</p></div>
        </div>
        <div class="action-command-side">
          <span class="command-label">데이터 흐름</span>
          <div class="workflow-steps">
            ${{steps.map((step, index) => `
              <div class="workflow-step ${{index === state.activeIndex ? "active" : ""}}">
                <span>${{escapeHtml(index + 1)}}</span>
                <strong>${{escapeHtml(step[0])}}</strong>
                <p>${{escapeHtml(step[1])}}</p>
              </div>
            `).join("")}}
          </div>
        </div>
      `;
    }}

    function agentFieldValue(selector) {{
      return document.querySelector(selector)?.value.trim() || "";
    }}

    function agentCommandState() {{
      const question = agentFieldValue("#agentQuestion");
      const classinId = agentFieldValue("#memoClassinId");
      const tag = agentFieldValue("#memoTag");
      const memoText = agentFieldValue("#memoText");
      const hasQuestion = Boolean(question);
      const hasMemoTarget = Boolean(classinId);
      const hasMemoText = Boolean(memoText);
      let phase = "질문·메모 대기";
      let detail = "AI에게 물어볼 운영 질문을 쓰거나 학생별 상담 메모를 준비하세요.";
      let primaryAction = "focusAgentQuestion";
      let primaryLabel = "질문 쓰기";
      let tone = "warn";
      let activeIndex = 0;
      if (hasQuestion) {{
        phase = "AI 질문 준비";
        detail = "현재 질문은 읽기 중심의 수동 오더입니다. 답변을 확인한 뒤 필요한 후속 기록만 따로 저장하세요.";
        primaryAction = "askAgent";
        primaryLabel = "AI에게 묻기";
        tone = "ok";
        activeIndex = 1;
      }} else if (hasMemoText && !hasMemoTarget) {{
        phase = "학생 지정 필요";
        detail = "메모 내용은 준비되어 있지만 저장할 ClassIn ID가 비어 있습니다.";
        primaryAction = "focusMemoTarget";
        primaryLabel = "학생 지정";
        tone = "danger";
        activeIndex = 2;
      }} else if (hasMemoTarget && hasMemoText) {{
        phase = "학생 메모 준비";
        detail = "대상 학생과 메모 내용이 준비되었습니다. 저장 전 태그와 학생 ID를 한 번 더 확인하세요.";
        primaryAction = "writeMemo";
        primaryLabel = "메모 저장";
        tone = "ok";
        activeIndex = 2;
      }} else if (hasMemoTarget) {{
        phase = "메모 내용 대기";
        detail = "대상 학생이 선택되었습니다. 상담 내용이나 다음 액션을 기록하세요.";
        primaryAction = "focusMemoText";
        primaryLabel = "메모 쓰기";
        tone = "warn";
        activeIndex = 2;
      }}
      return {{
        phase,
        detail,
        primaryAction,
        primaryLabel,
        tone,
        activeIndex,
        meta: [
          ["질문", hasQuestion ? `${{question.length}}자` : "대기"],
          ["학생", classinId || "미지정"],
          ["태그", tag || "기본"],
          ["메모", hasMemoText ? `${{memoText.length}}자` : "대기"],
          ["모드", status.mode || "-"],
        ],
      }};
    }}

    function renderAgentCommand() {{
      const target = document.querySelector("#agentCommand");
      if (!target) return;
      const state = agentCommandState();
      target.className = `action-command agent-command ${{escapeHtml(state.tone)}}`;
      const steps = [
        ["대상", "학생·반 맥락 확인"],
        ["질문", "운영 질문 구체화"],
        ["메모", "상담·후속 조치 기록"],
        ["실행", "AI 답변 또는 메모 저장"],
      ];
      target.innerHTML = `
        <div class="action-command-main">
          <span class="command-label">메모·AI 운영 상태</span>
          <strong>${{escapeHtml(state.phase)}}</strong>
          <p>${{escapeHtml(state.detail)}}</p>
          <div class="command-meta">
            ${{state.meta.map(([label, value]) => `<span class="badge">${{escapeHtml(label)}} ${{escapeHtml(value)}}</span>`).join("")}}
          </div>
          <div class="action-command-actions">
            <button data-primary-action="${{escapeHtml(state.primaryAction)}}">${{escapeHtml(state.primaryLabel)}}</button>
            <button data-primary-action="focusAgentQuestion" class="secondary">질문</button>
            <button data-primary-action="focusMemoTarget" class="secondary">학생 메모</button>
            <button data-primary-action="refreshDiagnostics" class="secondary">설정 점검</button>
          </div>
          <div class="command-gate"><span>운영 원칙</span><p>AI 답변은 Notion/ClassIn 데이터를 조회·요약하는 수동 오더입니다. 쓰기 작업 전에는 대상 학생과 메모 내용을 확인하세요.</p></div>
        </div>
        <div class="action-command-side">
          <span class="command-label">메모·AI 흐름</span>
          <div class="workflow-steps">
            ${{steps.map((step, index) => `
              <div class="workflow-step ${{index === state.activeIndex ? "active" : ""}}">
                <span>${{escapeHtml(index + 1)}}</span>
                <strong>${{escapeHtml(step[0])}}</strong>
                <p>${{escapeHtml(step[1])}}</p>
              </div>
            `).join("")}}
          </div>
        </div>
      `;
    }}

    function focusAgentQuestionField() {{
      activateTab("memo");
      const target = document.querySelector("#agentQuestion");
      target?.focus();
      renderAgentCommand();
    }}

    function focusMemoTargetField() {{
      activateTab("memo");
      const target = document.querySelector("#memoClassinId");
      target?.focus();
      renderAgentCommand();
    }}

    function focusMemoTextField() {{
      activateTab("memo");
      const target = document.querySelector("#memoText");
      target?.focus();
      renderAgentCommand();
    }}

    async function loadWebhookInbox(logResult) {{
      if (!status.ok) {{
        renderWebhookInbox({{ summary: {{}}, items: [] }});
        document.querySelector("#webhookInboxTable").innerHTML =
          `<div class="empty">config.yaml을 읽은 뒤 ClassIn 수신 기록을 볼 수 있습니다.</div>`;
        return {{ summary: {{}}, items: [] }};
      }}
      const params = new URLSearchParams();
      params.set("limit", document.querySelector("#webhookLimit").value || "30");
      const response = await fetch(`/api/webhook-inbox?${{params.toString()}}`);
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || data.message || "webhook inbox load failed");
      }}
      renderWebhookInbox(data);
      if (logResult) {{
        writeLog("ClassIn 수신 기록을 갱신했습니다.", data.summary);
      }}
      return data;
    }}

    function webhookEventLabel(cmd) {{
      const labels = {{
        Attendance: "출결 기록",
        End: "수업 종료",
        HomeworkSubmit: "숙제 제출",
        HomeworkScore: "숙제 점수",
        AnswerSheetScore: "OMR 점수",
        unreadable: "읽을 수 없음",
        unknown: "알 수 없는 이벤트",
      }};
      return labels[cmd] || cmd || "알 수 없는 이벤트";
    }}

    function webhookEventSummary(summary) {{
      const source = summary.by_event_label || Object.entries(summary.by_cmd || {{}}).reduce((acc, [cmd, count]) => {{
        const label = webhookEventLabel(cmd);
        acc[label] = (acc[label] || 0) + Number(count || 0);
        return acc;
      }}, {{}});
      return Object.entries(source)
        .map(([label, count]) => `${{label}} ${{count}}`)
        .join(" · ") || "-";
    }}

    function webhookCourseLabel(item) {{
      if (item.course_label) return item.course_label;
      if (item.class_name) return item.class_name;
      const courseId = item.course_id || "";
      const classId = item.class_id || "";
      if (courseId && classId) return `수업 ${{courseId}} · ${{classId}}`;
      if (courseId) return `강좌 ${{courseId}}`;
      if (classId) return `수업 ${{classId}}`;
      return "수업 정보 없음";
    }}

    function renderWebhookInbox(data) {{
      currentWebhookEvents = data.items || [];
      currentWebhookSummary = data.summary || {{}};
      webhookInboxPage = 1;
      const summary = currentWebhookSummary;
      const eventText = webhookEventSummary(summary);
      const summaryRows = [
        ["수신 기록", summary.total || 0],
        ["정리 완료", summary.parsed || 0],
        ["확인 필요", summary.review || 0],
        ["학생 신호", summary.student_events || 0],
      ];
      document.querySelector("#webhookInboxSummary").innerHTML = summaryRows.map(([label, value]) => `
        <div class="diagnostic-count">
          <span>${{escapeHtml(label)}}</span>
          <strong>${{escapeHtml(value)}}</strong>
        </div>
      `).join("");
      document.querySelector("#webhookCmdSummary").innerHTML =
        `<strong>이벤트 종류</strong><span>${{escapeHtml(eventText)}}</span>`;
      renderDataCommand();
      renderWebhookInboxTable();
    }}

    function renderWebhookInboxTable() {{
      const target = document.querySelector("#webhookInboxTable");
      if (!currentWebhookEvents.length) {{
        target.innerHTML = `<div class="empty">아직 수신된 ClassIn 기록이 없습니다.</div>`;
        return;
      }}
      const page = paged(currentWebhookEvents, webhookInboxPage, PAGE_SIZE.webhookInbox);
      webhookInboxPage = page.page;
      target.innerHTML = `
        <div class="table-wrap is-compact">
          <table class="compact-table">
            <thead>
              <tr>
                <th>상태</th>
                <th>이벤트</th>
                <th>수업·활동</th>
                <th>학생</th>
                <th>수신</th>
                <th>보관</th>
              </tr>
            </thead>
            <tbody>
              ${{page.items.map((item) => `
                <tr>
                  <td>${{statusPill(item.status === "parsed" ? "ok" : "warn")}}<br><span class="badge">${{escapeHtml(item.status_label || (item.status === "parsed" ? "정리 완료" : "확인 필요"))}}</span></td>
                  <td><strong>${{escapeHtml(item.event_label || webhookEventLabel(item.cmd))}}</strong><br><span class="badge">${{escapeHtml(webhookCourseLabel(item))}}</span></td>
                  <td>${{escapeHtml(item.activity || item.class_name || "-")}}<br><span class="badge">${{escapeHtml(item.class_name || "-")}}</span></td>
                  <td>${{escapeHtml(item.student_count || 0)}}명<br><span class="badge">${{escapeHtml((item.students || []).join(", ") || "-")}}</span></td>
                  <td>${{escapeHtml(formatDate(item.received_at))}}<br><span class="badge">${{escapeHtml(formatDate(item.action_at))}}</span></td>
                  <td><span class="badge">${{escapeHtml(item.source_label || (item.status === "parsed" ? "보관됨" : "확인 필요"))}}</span></td>
                </tr>
              `).join("")}}
            </tbody>
          </table>
        </div>
        ${{pagerHtml("webhookInbox", page.page, page.totalPages, page.totalItems)}}
      `;
    }}

    async function loadAcademyContexts(logResult) {{
      if (!status.ok) {{
        renderBlockedAcademyContexts("config.yaml을 읽은 뒤 학원 데이터 융합을 볼 수 있습니다.");
        return {{ summary: {{}}, items: [] }};
      }}
      const params = new URLSearchParams();
      const className = document.querySelector("#contextClassName").value;
      if (className) params.set("class_name", className);
      const response = await fetch(`/api/academy-contexts?${{params.toString()}}`);
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || data.message || "academy context load failed");
      }}
      renderAcademyContexts(data);
      if (logResult) {{
        writeLog("학원 데이터 융합 맥락을 갱신했습니다.", data.summary);
      }}
      return data;
    }}

    function renderBlockedAcademyContexts(message) {{
      currentAcademyContexts = [];
      currentAcademyContextSummary = {{}};
      currentAcademyReviewItems = [];
      document.querySelector("#academyContextSummary").innerHTML = "";
      document.querySelector("#academyContextReview").innerHTML = "";
      document.querySelector("#academyContextTable").innerHTML =
        `<div class="empty">${{escapeHtml(message)}}</div>`;
      renderDataCommand();
    }}

    function updateContextClassOptions(classes, selected) {{
      const select = document.querySelector("#contextClassName");
      const current = selected || select.value || "";
      const options = [`<option value="">전체 반</option>`]
        .concat((classes || []).map((name) =>
          `<option value="${{escapeHtml(name)}}" ${{name === current ? "selected" : ""}}>${{escapeHtml(name)}}</option>`
        ));
      select.innerHTML = options.join("");
      academyContextClassesLoaded = true;
    }}

    function renderAcademyContexts(data) {{
      currentAcademyContexts = data.items || [];
      currentAcademyContextSummary = data.summary || {{}};
      currentAcademyReviewItems = data.needs_review_items || [];
      academyContextPage = 1;
      updateContextClassOptions(data.classes || [], data.class_name || "");
      const summary = currentAcademyContextSummary;
      const summaryRows = [
        ["학생", summary.total_students || 0],
        ["맥락 있음", summary.students_with_context || 0],
        ["맥락 없음", summary.students_without_context || 0],
        ["확인 필요", summary.needs_review || 0],
        ["메모", summary.memos || 0],
      ];
      document.querySelector("#academyContextSummary").innerHTML = summaryRows.map(([label, value]) => `
        <div class="diagnostic-count">
          <span>${{escapeHtml(label)}}</span>
          <strong>${{escapeHtml(value)}}</strong>
        </div>
      `).join("");

      const reviewItems = data.needs_review_items || [];
      if (reviewItems.length) {{
        const reviewText = reviewItems
          .slice(0, 3)
          .map((item) => `${{item.student_name || "미매칭"}} · ${{item.class_name || "-"}} · ${{item.detail || item.reason || "-"}}`)
          .join(" / ");
        document.querySelector("#academyContextReview").innerHTML =
          `<strong>확인 필요 ${{escapeHtml(reviewItems.length)}}건</strong><span>${{escapeHtml(reviewText)}}</span>`;
      }} else {{
        document.querySelector("#academyContextReview").innerHTML =
          `<strong>확인 필요 0건</strong><span>자동 매칭 대기 항목이 없습니다.</span>`;
      }}
      renderDataCommand();
      renderAcademyContextTable();
    }}

    function renderAcademyContextTable() {{
      const target = document.querySelector("#academyContextTable");
      if (!currentAcademyContexts.length) {{
        target.innerHTML = `<div class="empty">조회된 학생 맥락이 없습니다.</div>`;
        return;
      }}
      const page = paged(currentAcademyContexts, academyContextPage, PAGE_SIZE.academyContexts);
      academyContextPage = page.page;
      target.innerHTML = `
        <div class="table-wrap is-compact">
          <table class="compact-table">
            <thead>
              <tr>
                <th>상태</th>
                <th>학생</th>
                <th>융합 맥락</th>
                <th>출처</th>
                <th>리포트</th>
                <th>상세</th>
              </tr>
            </thead>
            <tbody>
              ${{page.items.map((item) => `
                <tr>
                  <td>${{statusPill(item.has_context ? "ok" : "skipped")}}</td>
                  <td><strong>${{escapeHtml(item.student_name || "-")}}</strong><br><span class="badge">${{escapeHtml(item.class_name || "-")}} · ${{escapeHtml(item.student_classin_id || "-")}}</span></td>
                  <td>${{escapeHtml(item.summary || "자체 데이터 없음")}}<br>
                    <span class="badge">출결 ${{escapeHtml(item.counts?.offline_attendance || 0)}} · 성적 ${{escapeHtml(item.counts?.offline_scores || 0)}} · 메모 ${{escapeHtml(item.counts?.memos || 0)}}</span>
                  </td>
                  <td>${{(item.sources || []).length
                    ? (item.sources || []).map((source) => `<span class="badge">${{escapeHtml(source.kind || "-")}} · ${{escapeHtml(source.detail || source.source_name || "-")}}</span>`).join("<br>")
                    : `<span class="badge">-</span>`}}</td>
                  <td>${{escapeHtml(item.weekly_report?.status || "-")}}<br><span class="badge">${{item.weekly_report?.approved ? "승인됨" : "미승인"}}</span></td>
                  <td>
                    <button
                      data-action="openStudentBrief"
                      data-student-id="${{escapeHtml(item.student_classin_id || "")}}"
                      data-student-name="${{escapeHtml(item.student_name || "")}}"
                      data-class-name="${{escapeHtml(item.class_name || "")}}"
                      class="secondary"
                    >학생 보기</button>
                  </td>
                </tr>
              `).join("")}}
            </tbody>
          </table>
        </div>
        ${{pagerHtml("academyContexts", page.page, page.totalPages, page.totalItems)}}
      `;
    }}

    async function loadSituation(options) {{
      if (!status.ok) {{
        renderMissing({{ summary: {{}}, items: [] }}, options || {{}});
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
      renderMissing(missingData, options || {{}});

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

    function setReadinessMode(mode) {{
      currentReadinessMode = mode || "local-demo";
      document.querySelectorAll("[data-readiness-mode]").forEach((button) => {{
        button.classList.toggle("active", button.dataset.readinessMode === currentReadinessMode);
      }});
    }}

    function readinessModeLabel(mode) {{
      return ({{
        "local-demo": "데모",
        "classin-live": "ClassIn 실연동",
        "kakao-live": "카톡 실발송",
      }})[mode] || mode || "-";
    }}

    function readinessStageLabel(stage) {{
      return ({{
        "local-demo": "데모",
        "classin-live": "ClassIn 실연동",
        "kakao-live": "카톡 실발송",
        config: "설정 파일",
      }})[stage] || stage || "-";
    }}

    function readinessCommandState() {{
      const summary = currentReadinessSummary || {{}};
      const blockers = Number(summary.blockers || 0);
      const warnings = Number(summary.warnings || 0);
      const firstBlocker = currentReadinessItems.find((item) => ["missing", "blocked", "failed"].includes(item.status));
      const firstWarning = currentReadinessItems.find((item) => item.status === "warn");
      const modeLabel = readinessModeLabel(currentReadinessMode);
      let phase = `${{modeLabel}} 점검 대기`;
      let detail = "운영 전환 단계를 선택하고 설정 상태를 점검하세요.";
      let primaryAction = "refreshReadiness";
      let primaryLabel = "준비 상태 점검";
      let tone = "warn";
      let activeIndex = currentReadinessMode === "kakao-live" ? 3 : currentReadinessMode === "classin-live" ? 1 : 0;

      if (currentReadinessItems.length) {{
        if (blockers) {{
          phase = `${{modeLabel}} 전환 차단`;
          detail = firstBlocker
            ? `${{firstBlocker.label || "설정 항목"}}: ${{firstBlocker.fix || firstBlocker.detail || "수정 후 다시 점검하세요."}}`
            : "막힌 항목을 해결한 뒤 다시 점검하세요.";
          tone = "danger";
          if (currentReadinessMode === "local-demo") {{
            primaryAction = "loadNotionSchema";
            primaryLabel = "Notion 설계 보기";
          }} else if (currentReadinessMode === "classin-live") {{
            primaryAction = "loadPilotBrief";
            primaryLabel = "파일럿 브리프";
          }} else {{
            primaryAction = "refreshReadiness";
            primaryLabel = "카톡 설정 재점검";
          }}
        }} else if (warnings) {{
          phase = `${{modeLabel}} 확인 필요`;
          detail = firstWarning
            ? `${{firstWarning.label || "경고 항목"}}: ${{firstWarning.fix || firstWarning.detail || "운영 전 확인하세요."}}`
            : "경고 항목을 확인한 뒤 실제 전환하세요.";
          primaryAction = "refreshReadiness";
          primaryLabel = "경고 다시 보기";
          tone = "warn";
        }} else if (currentReadinessReady) {{
          phase = `${{modeLabel}} 준비 완료`;
          detail = currentReadinessMode === "local-demo"
            ? "데모와 외부 발송 없는 검토 기준 준비가 끝났습니다. 다음은 ClassIn 실연동 준비 상태 확인입니다."
            : "오프라인 준비 점검은 통과했습니다. 실제 전환 전 비파괴 API 점검을 실행하세요.";
          primaryAction = currentReadinessMode === "local-demo" ? "loadPilotBrief" : "runLiveDiagnostics";
          primaryLabel = currentReadinessMode === "local-demo" ? "파일럿 브리프" : "실 API 점검";
          tone = "ok";
          activeIndex = currentReadinessMode === "local-demo" ? 0 : currentReadinessMode === "classin-live" ? 2 : 3;
        }}
      }}

      return {{
        phase,
        detail,
        primaryAction,
        primaryLabel,
        tone,
        activeIndex,
        meta: [
          ["모드", modeLabel],
          ["막힘", `${{blockers}}`],
          ["경고", `${{warnings}}`],
          ["정상", `${{summary.ok || 0}}`],
          ["항목", `${{summary.total || currentReadinessItems.length}}`],
          ["데모", currentReadinessDemo ? "예" : "아니오"],
        ],
      }};
    }}

    function renderReadinessCommand() {{
      const target = document.querySelector("#readinessCommand");
      if (!target) return;
      const state = readinessCommandState();
      target.className = `action-command settings-command ${{escapeHtml(state.tone)}}`;
      const steps = [
        ["데모", "Notion·Claude·외부 발송 없음 확인"],
        ["ClassIn", "SID/secret·Webhook 공개 URL"],
        ["DataSub", "고정 URL 등록·수신 확인"],
        ["카톡", "승인 템플릿과 실발송 게이트"],
      ];
      target.innerHTML = `
        <div class="action-command-main">
          <span class="command-label">운영 전환 상태</span>
          <strong>${{escapeHtml(state.phase)}}</strong>
          <p>${{escapeHtml(state.detail)}}</p>
          <div class="command-meta">
            ${{state.meta.map(([label, value]) => `<span class="badge">${{escapeHtml(label)}} ${{escapeHtml(value)}}</span>`).join("")}}
          </div>
          <div class="action-command-actions">
            <button data-primary-action="${{escapeHtml(state.primaryAction)}}">${{escapeHtml(state.primaryLabel)}}</button>
            <button data-action="setReadinessMode" data-readiness-mode="local-demo" class="secondary">데모</button>
            <button data-action="setReadinessMode" data-readiness-mode="classin-live" class="secondary">ClassIn 실연동</button>
            <button data-action="setReadinessMode" data-readiness-mode="kakao-live" class="secondary">카톡 실발송</button>
            <button data-primary-action="runLiveDiagnostics" class="secondary">실 API 점검</button>
          </div>
          <div class="command-gate"><span>전환 원칙</span><p>실연동 전에는 준비 점검과 비파괴 API 진단을 통과해야 합니다. 카톡은 승인 템플릿과 외부 발송 없는 검토가 끝나기 전까지 실제 발송하지 마세요.</p></div>
        </div>
        <div class="action-command-side">
          <span class="command-label">전환 흐름</span>
          <div class="workflow-steps">
            ${{steps.map((step, index) => `
              <div class="workflow-step ${{index === state.activeIndex ? "active" : ""}}">
                <span>${{escapeHtml(index + 1)}}</span>
                <strong>${{escapeHtml(step[0])}}</strong>
                <p>${{escapeHtml(step[1])}}</p>
              </div>
            `).join("")}}
          </div>
        </div>
      `;
    }}

    async function loadReadiness(logResult) {{
      if (!status.ok) {{
        const blocked = {{
          ok: true,
          mode: currentReadinessMode,
          ready: false,
          summary: {{ total: 1, ok: 0, warn: 0, missing: 1, blocked: 0, blockers: 1, warnings: 0 }},
          items: [{{
            stage: "config",
            label: "config.yaml",
            status: "missing",
            detail: status.error || "config.yaml을 읽을 수 없습니다.",
            fix: "config.yaml을 준비한 뒤 다시 점검하세요.",
          }}],
        }};
        renderReadiness(blocked);
        return blocked;
      }}
      const params = new URLSearchParams();
      params.set("mode", currentReadinessMode);
      const response = await fetch(`/api/readiness?${{params.toString()}}`);
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || data.message || "readiness check failed");
      }}
      renderReadiness(data);
      if (logResult) {{
        writeLog("운영 전환 체크리스트를 갱신했습니다.", data.summary);
      }}
      return data;
    }}

    function renderReadiness(data) {{
      currentReadinessItems = data.items || [];
      currentReadinessSummary = data.summary || {{}};
      currentReadinessReady = Boolean(data.ready);
      currentReadinessDemo = Boolean(data.demo);
      readinessPage = 1;
      setReadinessMode(data.mode || currentReadinessMode);
      const summary = currentReadinessSummary;
      const summaryRows = [
        ["상태", data.ready ? "준비 완료" : "확인 필요"],
        ["막힘", summary.blockers || 0],
        ["경고", summary.warnings || 0],
        ["정상", summary.ok || 0],
        ["총 항목", summary.total || currentReadinessItems.length],
      ];
      document.querySelector("#readinessSummary").innerHTML = summaryRows.map(([label, value]) => `
        <div class="diagnostic-count ${{label === "상태" && data.ready ? "ok" : label === "막힘" && Number(value) ? "failed" : label === "경고" && Number(value) ? "warn" : ""}}">
          <span>${{escapeHtml(label)}}</span>
          <strong>${{escapeHtml(value)}}</strong>
        </div>
      `).join("");
      renderReadinessList();
      renderReadinessCommand();
    }}

    function renderReadinessList() {{
      const target = document.querySelector("#readinessList");
      if (!currentReadinessItems.length) {{
        target.innerHTML = `<div class="empty compact">점검 항목이 없습니다.</div>`;
        return;
      }}
      const page = paged(currentReadinessItems, readinessPage, PAGE_SIZE.readiness);
      readinessPage = page.page;
      target.innerHTML = page.items.map((item) => `
        <div class="readiness-row">
          <div>
            ${{statusPill(item.status)}}
            <strong>${{escapeHtml(readinessStageLabel(item.stage))}} · ${{escapeHtml(item.label || "-")}}</strong>
            <span>${{escapeHtml(item.detail || "")}}</span>
          </div>
          <div class="readiness-fix">${{escapeHtml(item.fix || "-")}}</div>
        </div>
      `).join("") + pagerHtml("readiness", page.page, page.totalPages, page.totalItems);
    }}

    async function loadNotionSchema(logResult) {{
      const params = new URLSearchParams();
      params.set("prefix", document.querySelector("#notionSchemaPrefix").value || "ClassIn Toolkit");
      const response = await fetch(`/api/notion-schema?${{params.toString()}}`);
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || data.message || "notion schema preview failed");
      }}
      renderNotionSchema(data);
      if (logResult) {{
        writeLog("Notion DB 설계 미리보기를 갱신했습니다.", data.summary);
      }}
      return data;
    }}

    function renderNotionSchema(data) {{
      currentNotionSchema = data;
      const summary = data.summary || {{}};
      const summaryRows = [
        ["DB", summary.databases || 0],
        ["속성", summary.properties || 0],
        ["Prefix", data.prefix || "-"],
      ];
      document.querySelector("#notionSchemaSummary").innerHTML = summaryRows.map(([label, value]) => `
        <div class="diagnostic-count">
          <span>${{escapeHtml(label)}}</span>
          <strong>${{escapeHtml(value)}}</strong>
        </div>
      `).join("");
      const items = data.items || [];
      document.querySelector("#notionSchemaList").innerHTML = items.length
        ? items.map((item) => `
          <div class="schema-row">
            <div class="schema-meta">
              <span class="badge ok">${{escapeHtml(item.index || "")}}</span>
              <strong>${{escapeHtml(item.title || "-")}}</strong>
              <span class="badge">${{escapeHtml(item.property_count || 0)}}개 속성</span>
            </div>
            <details class="schema-details">
              <summary><span>속성 목록</span><span class="badge">${{escapeHtml(item.kind || "DB")}}</span></summary>
              <div class="schema-props">
                ${{(item.properties || []).map((prop) => `<span class="badge">${{escapeHtml(prop)}}</span>`).join("")}}
              </div>
            </details>
          </div>
        `).join("")
        : `<div class="empty compact">표시할 Notion DB 설계가 없습니다.</div>`;

      lastNotionSchemaText = [
        "# Notion DB 설계 미리보기",
        "",
        `prefix: ${{data.prefix || "ClassIn Toolkit"}}`,
        "",
        "## 생성 명령",
        data.commands?.dry_run || "",
        data.commands?.write || "",
        "",
        "## config.yaml 조각",
        data.config_snippet || "",
      ].join("\\n").trim();
      setPreviewText("#notionSchemaCommand", lastNotionSchemaText);
    }}

    async function copyNotionSchema() {{
      if (!lastNotionSchemaText) {{
        await loadNotionSchema(false);
      }}
      if (!lastNotionSchemaText) {{
        throw new Error("복사할 Notion DB 설계가 없습니다.");
      }}
      const copyMode = await copyTextWithFallback(lastNotionSchemaText, "#notionSchemaCommand");
      toast(copyMode === "selected" ? "본문을 선택했습니다. Ctrl+C로 복사하세요." : "Notion DB 설계를 복사했습니다.", "ok");
      writeLog("Notion DB 설계 미리보기를 복사했습니다.", {{ chars: lastNotionSchemaText.length }});
    }}

    async function loadPilotBrief(logResult) {{
      const response = await fetch("/api/pilot-brief");
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || data.message || "pilot bring-up brief failed");
      }}
      renderPilotBrief(data);
      if (logResult) {{
        writeLog("파일럿 브링업 브리프를 갱신했습니다.", data.summary);
      }}
      return data;
    }}

    function renderPilotBrief(data) {{
      currentPilotBrief = data;
      pilotBriefPage = 1;
      const summary = data.summary || {{}};
      const summaryRows = [
        ["정상", summary.ok || 0, "ok"],
        ["보강", summary.review || 0, "warn"],
        ["확인", summary.warn || 0, "warn"],
        ["누락", summary.missing || 0, "failed"],
        ["총 항목", summary.total || 0, ""],
      ];
      document.querySelector("#pilotBriefSummary").innerHTML = summaryRows.map(([label, value, tone]) => `
        <div class="diagnostic-count ${{escapeHtml(tone)}}">
          <span>${{escapeHtml(label)}}</span>
          <strong>${{escapeHtml(value)}}</strong>
        </div>
      `).join("");
      renderPilotBriefList();
      lastPilotBriefText = data.markdown || "";
      setPreviewText("#pilotBriefPreview", lastPilotBriefText || "브링업 브리프 내용이 없습니다.");
    }}

    function renderPilotBriefList() {{
      const checklist = currentPilotBrief?.checklist || [];
      const target = document.querySelector("#pilotBriefList");
      if (!target) return;
      if (!checklist.length) {{
        target.innerHTML = `<div class="empty compact">브링업 체크리스트가 없습니다.</div>`;
        return;
      }}
      const page = paged(checklist, pilotBriefPage, PAGE_SIZE.pilotBrief);
      pilotBriefPage = page.page;
      target.innerHTML = page.items.map((item, index) => `
          <div class="readiness-row">
            <div>
              ${{statusPill(item.status)}}
              <strong>${{escapeHtml((page.page - 1) * PAGE_SIZE.pilotBrief + index + 1)}}. ${{escapeHtml(item.title || "-")}}</strong>
              <span>${{escapeHtml(item.detail || "")}}</span>
            </div>
            <div class="readiness-fix">${{escapeHtml(item.fix || "-")}}</div>
          </div>
        `).join("") + pagerHtml("pilotBrief", page.page, page.totalPages, page.totalItems);
    }}

    async function copyPilotBrief() {{
      if (!lastPilotBriefText) {{
        await loadPilotBrief(false);
      }}
      if (!lastPilotBriefText) {{
        throw new Error("복사할 파일럿 브링업 브리프가 없습니다.");
      }}
      const copyMode = await copyTextWithFallback(lastPilotBriefText, "#pilotBriefPreview");
      toast(copyMode === "selected" ? "본문을 선택했습니다. Ctrl+C로 복사하세요." : "파일럿 브링업 브리프를 복사했습니다.", "ok");
      writeLog("파일럿 브링업 브리프를 복사했습니다.", {{ chars: lastPilotBriefText.length }});
    }}

    function renderBlockedSituation() {{
      currentMissing = [];
      selectedMissingKeys = new Set();
      renderMissing({{ summary: {{}}, items: [] }});
      currentMissingBlocked = true;
      renderMissingCommand();
      document.querySelector("#missingTable").innerHTML =
        `<div class="empty">Notion 설정을 먼저 채워야 조회할 수 있습니다.</div>`;
      document.querySelector("#missingSelectionList").innerHTML =
        `<div class="empty">Notion 설정을 먼저 채워야 조회할 수 있습니다.</div>`;
      renderNotifications({{ items: [] }});
    }}

    function renderBlockedSchedule() {{
      currentSchedule = [];
      currentScheduleSummary = {{}};
      currentScheduleBlocked = true;
      document.querySelector("#scheduleSummary").innerHTML = "";
      document.querySelector("#scheduleTable").innerHTML =
        `<div class="empty">Notion 설정을 먼저 채워야 스케줄을 조회할 수 있습니다.</div>`;
      renderScheduleCommand();
    }}

    function scheduleRangeLabel() {{
      const start = document.querySelector("#scheduleStart")?.value || "-";
      const days = dataMetric(document.querySelector("#scheduleDays")?.value || 0);
      if (!days) return start;
      return `${{start}} · ${{days}}일`;
    }}

    function scheduleCommandState() {{
      const summary = currentScheduleSummary || {{}};
      const lessons = dataMetric(summary.total_lessons || currentSchedule.length);
      const studentRows = dataMetric(summary.total_student_rows);
      const late = dataMetric(summary.late);
      const absent = dataMetric(summary.absent);
      const homeworkMissing = dataMetric(summary.homework_missing);
      const homeworkUnknown = currentSchedule.reduce((sum, item) => sum + dataMetric(item.homework_unknown), 0);
      const classCount = new Set(
        currentSchedule.flatMap((item) => item.class_names || []).filter(Boolean)
      ).size;
      const followUps = late + absent + homeworkMissing + homeworkUnknown;
      let phase = "스케줄 조회";
      let detail = "조회 범위를 고른 뒤 이번 주 수업·출결·숙제 상태를 확인하세요.";
      let primaryAction = "refreshSchedule";
      let primaryLabel = "스케줄 새로고침";
      let tone = "";
      let activeIndex = 0;
      if (currentScheduleBlocked || !status.ok) {{
        phase = "설정 확인";
        detail = "Notion 설정을 확인한 뒤 수업 기록과 학생별 후속 처리를 조회할 수 있습니다.";
        primaryAction = "refreshStatus";
        primaryLabel = "상태 새로고침";
        tone = "danger";
      }} else if (!lessons && hasLoadedSummary(summary)) {{
        phase = "수업 없음";
        detail = "현재 범위에는 조회된 수업 기록이 없습니다. 날짜 범위를 바꾸거나 스케줄 입력 화면에서 생성하세요.";
        primaryAction = "focusScheduleCore";
        primaryLabel = "스케줄 생성";
      }} else if (!lessons) {{
        phase = "조회 대기";
      }} else if (homeworkMissing) {{
        phase = "미제출 후속 처리";
        detail = "수업 기록에서 숙제 미제출 학생이 확인됐습니다. 문자 문구와 연락처 게이트를 확인하세요.";
        primaryAction = "focusMissingCore";
        primaryLabel = "미제출 처리";
        tone = "warn";
        activeIndex = 3;
      }} else if (absent || late || homeworkUnknown) {{
        phase = "출결·숙제 확인";
        detail = "지각·결석 또는 숙제 미기록 항목이 있습니다. 수업 기록이 정확히 들어왔는지 확인하세요.";
        tone = "warn";
        activeIndex = 2;
      }} else {{
        phase = "운영 정상";
        detail = "조회 범위의 수업 기록과 숙제 상태가 정리되어 있습니다.";
        tone = "ok";
        activeIndex = 3;
      }}
      return {{
        phase,
        detail,
        primaryAction,
        primaryLabel,
        tone,
        activeIndex,
        meta: [
          ["범위", scheduleRangeLabel()],
          ["수업", `${{lessons}}개`],
          ["반", `${{classCount}}개`],
          ["학생 기록", `${{studentRows}}건`],
          ["지각/결석", `${{late}}/${{absent}}`],
          ["미제출", `${{homeworkMissing}}건`],
        ],
      }};
    }}

    function renderScheduleCommand() {{
      const target = document.querySelector("#scheduleCommand");
      if (!target) return;
      const state = scheduleCommandState();
      target.className = `action-command schedule-command ${{escapeHtml(state.tone)}}`;
      const steps = [
        ["범위", "주간·일간 조회 기간 선택"],
        ["수업", "ClassIn 수업 기록 확인"],
        ["출결", "지각·결석·미기록 점검"],
        ["후속", "미제출 알림·리포트 근거 반영"],
      ];
      target.innerHTML = `
        <div class="action-command-main">
          <span class="command-label">스케줄 운영 상태</span>
          <strong>${{escapeHtml(state.phase)}}</strong>
          <p>${{escapeHtml(state.detail)}}</p>
          <div class="command-meta">
            ${{state.meta.map(([label, value]) => `<span class="badge">${{escapeHtml(label)}} ${{escapeHtml(value)}}</span>`).join("")}}
          </div>
          <div class="action-command-actions">
            <button data-primary-action="${{escapeHtml(state.primaryAction)}}">${{escapeHtml(state.primaryLabel)}}</button>
            <button data-primary-action="loadThisWeekSchedule" class="secondary">이번 주</button>
            <button data-primary-action="loadTodaySchedule" class="secondary">오늘</button>
            <button data-primary-action="focusScheduleCore" class="secondary">스케줄 입력</button>
            <button data-primary-action="exportScheduleCsv" class="secondary">CSV</button>
          </div>
          <div class="command-gate"><span>운영 원칙</span><p>ClassIn 대시보드와 API 생성을 동시에 조작하지 말고, 수업·숙제 생성은 액션 탭에서 생성 전 검토를 마친 뒤 진행하세요.</p></div>
        </div>
        <div class="action-command-side">
          <span class="command-label">스케줄 흐름</span>
          <div class="workflow-steps">
            ${{steps.map((step, index) => `
              <div class="workflow-step ${{index === state.activeIndex ? "active" : ""}}">
                <span>${{escapeHtml(index + 1)}}</span>
                <strong>${{escapeHtml(step[0])}}</strong>
                <p>${{escapeHtml(step[1])}}</p>
              </div>
            `).join("")}}
          </div>
        </div>
      `;
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
      currentScheduleSummary = summary;
      currentScheduleBlocked = false;
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
      schedulePage = 1;
      renderScheduleCommand();
      renderScheduleTable();
    }}

    function renderScheduleTable() {{
      const target = document.querySelector("#scheduleTable");
      if (!currentSchedule.length) {{
        target.innerHTML = `<div class="empty">조회 범위 안의 수업 기록이 없습니다.</div>`;
        return;
      }}
      const page = paged(currentSchedule, schedulePage, PAGE_SIZE.schedule);
      schedulePage = page.page;
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
              ${{page.items.map((item) => `
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
        </div>
        ${{pagerHtml("schedule", page.page, page.totalPages, page.totalItems)}}`;
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
      const rowsHtml = items.map((item) => `
        <tr>
          <td>${{escapeHtml(item.service)}}</td>
          <td>${{escapeHtml(item.check)}}</td>
          <td>${{statusPill(item.status)}}</td>
          <td>${{escapeHtml(item.detail || "-")}}</td>
          <td>${{escapeHtml(item.next_step || "-")}}</td>
        </tr>
      `).join("");
      const cardsHtml = items.map((item) => `
        <div class="diagnostic-card">
          <div class="diagnostic-card-head">
            <div>
              <strong>${{escapeHtml(item.service || "-")}}</strong>
              <span>${{escapeHtml(item.check || "-")}}</span>
              <span>${{escapeHtml(item.detail || "-")}}</span>
            </div>
            ${{statusPill(item.status)}}
          </div>
          <div class="diagnostic-next">${{escapeHtml(item.next_step || "-")}}</div>
        </div>
      `).join("");
      target.innerHTML = `
        <div class="diagnostic-card-list">${{cardsHtml}}</div>
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
              ${{rowsHtml}}
            </tbody>
          </table>
        </div>`;
    }}

    function isPendingMissing(item) {{
      return (item.notification_status || "pending") === "pending";
    }}

    function missingFilterCounts(items) {{
      return (items || []).reduce((counts, item) => {{
        counts.all += 1;
        if (missingFilterMatches(item, "needsMessage")) counts.needsMessage += 1;
        if (missingFilterMatches(item, "review")) counts.review += 1;
        if (missingFilterMatches(item, "noPhone")) counts.noPhone += 1;
        if (missingFilterMatches(item, "done")) counts.done += 1;
        return counts;
      }}, {{ all: 0, needsMessage: 0, review: 0, noPhone: 0, done: 0 }});
    }}

    function missingFilterDefinitions(items) {{
      const counts = missingFilterCounts(items);
      return [
        ["all", "전체", counts.all],
        ["needsMessage", "연락 필요", counts.needsMessage],
        ["review", "다시 확인", counts.review],
        ["noPhone", "연락처 없음", counts.noPhone],
        ["done", "완료", counts.done],
      ];
    }}

    function missingFilterMatches(item, filterKey = missingFilter) {{
      if (filterKey === "needsMessage") {{
        return item.action_required === "needs_message" || (item.has_parent_phone && isPendingMissing(item));
      }}
      if (filterKey === "review") {{
        return item.action_required === "needs_retry"
          || item.notification_status === "failed"
          || ["review", "blocked"].includes(item.notification_quality_status || "");
      }}
      if (filterKey === "noPhone") {{
        return item.action_required === "needs_phone" || !item.has_parent_phone;
      }}
      if (filterKey === "done") {{
        return item.action_required === "needs_review"
          || ["dry_run", "sent"].includes(item.notification_status || "");
      }}
      return true;
    }}

    function filteredMissingItems() {{
      return currentMissing.filter((item) => missingFilterMatches(item));
    }}

    function missingFilterEmptyText() {{
      const labels = {{
        needsMessage: "연락이 필요한 미제출 학생이 없습니다.",
        review: "다시 확인할 미제출 학생이 없습니다.",
        noPhone: "연락처가 없는 미제출 학생이 없습니다.",
        done: "처리 완료된 미제출 학생이 없습니다.",
      }};
      return labels[missingFilter] || "선택한 조건의 미제출 학생이 없습니다.";
    }}

    function renderMissingFilters() {{
      const definitions = missingFilterDefinitions(currentMissing);
      document.querySelectorAll(".missing-filter").forEach((target) => {{
        if (!definitions.some(([, , count]) => count > 0)) {{
          target.innerHTML = "";
          return;
        }}
        target.innerHTML = definitions.map(([key, label, count]) => `
          <button
            type="button"
            data-missing-filter="${{escapeHtml(key)}}"
            class="${{missingFilter === key ? "active" : ""}}"
          >${{escapeHtml(label)}} ${{escapeHtml(count)}}</button>
        `).join("");
      }});
    }}

    function defaultMissingSelectionKeys(items) {{
      return new Set(
        items
          .filter((item) => item.has_parent_phone && isPendingMissing(item))
          .map((item) => item.selection_key)
          .filter(Boolean)
      );
    }}

    function missingCommandState() {{
      const summary = currentMissingSummary || {{}};
      const loaded = currentMissing.length > 0 || hasLoadedSummary(summary);
      const total = dataMetric(summary.total_missing || currentMissing.length);
      const selected = selectedMissingRows().length;
      const withPhone = dataMetric(
        summary.with_parent_phone ||
        currentMissing.filter((item) => item.has_parent_phone).length
      );
      const noPhone = dataMetric(
        summary.no_parent_phone ||
        currentMissing.filter((item) => !item.has_parent_phone).length
      );
      const pending = dataMetric(
        summary.pending ||
        currentMissing.filter((item) => isPendingMissing(item)).length
      );
      const failed = dataMetric(summary.failed || currentMissing.filter((item) => item.notification_status === "failed").length);
      const needsPhone = dataMetric(summary.needs_phone || noPhone);
      const needsRetry = dataMetric(summary.needs_retry || failed);
      const qualityReady = dataMetric(summary.quality_ready || currentMissing.filter((item) => item.notification_quality_status === "ready").length);
      const qualityReview = dataMetric(summary.quality_review || currentMissing.filter((item) => item.notification_quality_status === "review").length);
      const qualityBlocked = dataMetric(summary.quality_blocked || currentMissing.filter((item) => item.notification_quality_status === "blocked").length);
      const previewSummary = lastMissingPreview?.data?.summary || null;
      const previewDispatchable = previewSummary ? dataMetric(previewSummary.dispatchable) : 0;
      const previewBlocked = previewSummary ? dataMetric(previewSummary.blocked || previewSummary.live_blocked) : 0;
      let phase = "미제출 조회";
      let detail = "조회 범위를 고른 뒤 미제출 학생과 발송 게이트를 확인하세요.";
      let primaryAction = "refreshMissing";
      let primaryLabel = "미제출 조회";
      let tone = "";
      let activeIndex = 0;
      if (currentMissingBlocked || !status.ok) {{
        phase = "설정 확인";
        detail = "Notion 설정을 먼저 확인해야 수업 기록과 미제출 학생을 조회할 수 있습니다.";
        primaryAction = "refreshStatus";
        primaryLabel = "상태 새로고침";
        tone = "danger";
      }} else if (!loaded) {{
        activeIndex = 0;
      }} else if (!total) {{
        phase = "처리 항목 없음";
        detail = "현재 조회 범위에는 숙제 미제출 학생이 없습니다.";
        tone = "ok";
        activeIndex = 3;
      }} else if (selected && previewSummary && (previewBlocked || previewDispatchable !== selected)) {{
        phase = "발송 게이트 확인";
        detail = "선택 대상 중 실제 발송 조건을 통과하지 못한 항목이 있습니다. 통과 문구와 연락처를 보강하세요.";
        primaryAction = "previewMissingHomeworkSms";
        primaryLabel = "다시 미리보기";
        tone = "warn";
        activeIndex = 2;
      }} else if (selected && previewSummary) {{
        phase = notifyMode() === "live" ? "실제 발송 준비" : "발송 전 검토 준비";
        detail = "선택 대상의 문구와 연락처 게이트가 통과했습니다. 현재 모드에 맞게 처리할 수 있습니다.";
        primaryAction = "sendMissingHomeworkSms";
        primaryLabel = notifyMode() === "live" ? "선택 문자 발송" : "검토 기록 생성";
        tone = "ok";
        activeIndex = 3;
      }} else if (needsRetry || qualityBlocked) {{
        phase = needsRetry ? "재시도 확인" : "문구 품질 차단";
        detail = needsRetry
          ? "지난 알림 실패가 있습니다. 번호와 로그를 확인한 뒤 다시 발송하세요."
          : "발송 제외 문구는 실제 발송에서 빠집니다. 문구 미리보기로 사유를 확인하세요.";
        primaryAction = needsRetry ? "selectAllMissing" : "previewMissingHomeworkSms";
        primaryLabel = needsRetry ? "발송 가능 선택" : "문구 미리보기";
        tone = "danger";
        activeIndex = needsRetry ? 0 : 2;
      }} else if (needsPhone) {{
        phase = "연락처 보완";
        detail = "보호자 연락처가 없어 자동 알림을 보낼 수 없는 학생이 있습니다.";
        primaryAction = "exportMissingCsv";
        primaryLabel = "CSV 내보내기";
        tone = "warn";
        activeIndex = 1;
      }} else if (!selected) {{
        phase = "대상 선택";
        detail = "발송 가능한 학생을 선택한 뒤 문구 품질을 먼저 확인하세요.";
        primaryAction = "selectAllMissing";
        primaryLabel = "전체 선택";
        tone = pending ? "warn" : "";
        activeIndex = 1;
      }} else if (!previewSummary) {{
        phase = "문구 검토";
        detail = "선택 대상의 AI 문구, 품질 상태, 연락처 게이트를 먼저 확인하세요.";
        primaryAction = "previewMissingHomeworkSms";
        primaryLabel = "문구 미리보기";
        tone = "warn";
        activeIndex = 2;
      }} else if (previewBlocked || previewDispatchable !== selected) {{
        phase = "발송 게이트 확인";
        detail = "선택 대상 중 실제 발송 조건을 통과하지 못한 항목이 있습니다. 통과 문구와 연락처를 보강하세요.";
        primaryAction = "previewMissingHomeworkSms";
        primaryLabel = "다시 미리보기";
        tone = "warn";
        activeIndex = 2;
      }} else {{
        phase = notifyMode() === "live" ? "실제 발송 준비" : "발송 전 검토 준비";
        detail = "선택 대상의 문구와 연락처 게이트가 통과했습니다. 현재 모드에 맞게 처리할 수 있습니다.";
        primaryAction = "sendMissingHomeworkSms";
        primaryLabel = notifyMode() === "live" ? "선택 문자 발송" : "검토 기록 생성";
        tone = "ok";
        activeIndex = 3;
      }}
      return {{
        phase,
        detail,
        primaryAction,
        primaryLabel,
        tone,
        activeIndex,
        meta: [
          ["조회", `${{total}}명`],
          ["선택", `${{selected}}명`],
          ["연락 가능", `${{withPhone}}명`],
          ["연락처 없음", `${{noPhone}}명`],
          ["문구 품질", qualitySummaryText(qualityReady, qualityReview, qualityBlocked)],
          ["발송 방식", notifyModeLabel()],
        ],
      }};
    }}

    function renderMissingCommand() {{
      const target = document.querySelector("#missingCommand");
      if (!target) return;
      const state = missingCommandState();
      target.className = `action-command missing-command ${{escapeHtml(state.tone)}}`;
      const steps = [
        ["조회", "미제출 학생과 알림 이력 확인"],
        ["대상", "연락처·중복·실패 상태 점검"],
        ["문구", "AI 문구 품질 게이트 검토"],
        ["처리", "검토 기록 또는 승인된 실제 발송"],
      ];
      target.innerHTML = `
        <div class="action-command-main">
          <span class="command-label">미제출 운영 상태</span>
          <strong>${{escapeHtml(state.phase)}}</strong>
          <p>${{escapeHtml(state.detail)}}</p>
          <div class="command-meta">
            ${{state.meta.map(([label, value]) => `<span class="badge">${{escapeHtml(label)}} ${{escapeHtml(value)}}</span>`).join("")}}
          </div>
          <div class="action-command-actions">
            <button data-primary-action="${{escapeHtml(state.primaryAction)}}">${{escapeHtml(state.primaryLabel)}}</button>
            <button data-primary-action="selectAllMissing" class="secondary">발송 가능 선택</button>
            <button data-primary-action="previewMissingHomeworkSms" class="secondary">문구 미리보기</button>
            <button data-primary-action="focusMissingCore" class="secondary">문자 발송</button>
            <button data-primary-action="exportMissingCsv" class="secondary">CSV</button>
          </div>
          <div class="command-gate"><span>발송 원칙</span><p>실제 발송은 보호자 연락처가 있고 품질 검토를 통과한 문구만 허용합니다. 제외 문구와 연락처 없는 학생은 기본 발송에서 빼세요.</p></div>
        </div>
        <div class="action-command-side">
          <span class="command-label">미제출 흐름</span>
          <div class="workflow-steps">
            ${{steps.map((step, index) => `
              <div class="workflow-step ${{index === state.activeIndex ? "active" : ""}}">
                <span>${{escapeHtml(index + 1)}}</span>
                <strong>${{escapeHtml(step[0])}}</strong>
                <p>${{escapeHtml(step[1])}}</p>
              </div>
            `).join("")}}
          </div>
        </div>
      `;
    }}

    function preservedMissingSelectionKeys(items, previous) {{
      const available = new Set(
        items
          .filter((item) => item.has_parent_phone)
          .map((item) => item.selection_key)
          .filter(Boolean)
      );
      return new Set([...previous].filter((key) => available.has(key)));
    }}

    function renderMissing(data, options) {{
      const summary = data.summary || {{}};
      currentMissingSummary = summary;
      currentMissingBlocked = false;
      document.querySelector("#missingCount").textContent = summary.total_missing || 0;
      document.querySelector("#noPhoneCount").textContent = summary.no_parent_phone || 0;
      document.querySelector("#dryRunCount").textContent = summary.dry_run || 0;
      document.querySelector("#sentCount").textContent = summary.sent || 0;
      document.querySelector("#failedCount").textContent = summary.failed || 0;

      const items = data.items || [];
      const previousSelection = selectedMissingKeys;
      currentMissing = items;
      selectedMissingKeys = options && options.preserveSelection
        ? preservedMissingSelectionKeys(items, previousSelection)
        : defaultMissingSelectionKeys(items);
      lastMissingPreview = null;
      const withPhone = items.filter((item) => item.has_parent_phone).length;
      const pendingWithPhone = items.filter((item) => item.has_parent_phone && isPendingMissing(item)).length;
      document.querySelector("#actionPreview").innerHTML =
        `<strong>숙제 미제출 문자 발송</strong>조회: ${{items.length}}명\\n연락 전: ${{pendingWithPhone}}명\\n발송 가능: ${{withPhone}}명\\n발송 방식: ${{escapeHtml(notifyModeLabel())}}`;
      missingPage = 1;
      renderMissingFilters();
      renderMissingSelectionList();
      renderMissingTable();
    }}

    function renderMissingTable() {{
      const target = document.querySelector("#missingTable");
      const items = filteredMissingItems();
      if (!items.length) {{
        target.innerHTML = `<div class="empty">${{escapeHtml(missingFilterEmptyText())}}</div>`;
        updateMissingSelectionSummary();
        return;
      }}
      const page = paged(items, missingPage, PAGE_SIZE.missing);
      missingPage = page.page;
      target.innerHTML = `
        <div class="mobile-card-list missing-card-list">
          ${{page.items.map((item) => `
            <label class="missing-card ${{item.has_parent_phone ? "" : "is-disabled"}}">
              <div class="missing-card-head">
                <input
                  type="checkbox"
                  class="select-missing"
                  data-key="${{escapeHtml(item.selection_key || "")}}"
                  ${{item.has_parent_phone ? "" : "disabled"}}
                  ${{selectedMissingKeys.has(item.selection_key) ? "checked" : ""}}
                  aria-label="${{escapeHtml(item.student_name)}} 문자 대상 선택"
                >
                <span class="missing-card-title">
                  <strong>${{escapeHtml(item.student_name)}} · ${{escapeHtml(item.class_name || "-")}}</strong>
                  <span>${{escapeHtml(item.lesson_classin_id || "-")}} · ${{escapeHtml(formatDate(item.date))}}</span>
                </span>
                ${{statusPill(item.notification_status)}}
                <span class="missing-card-meta">
                  <span class="badge">${{escapeHtml(item.attendance || "출석 미확인")}}</span>
                  ${{item.has_parent_phone ? `<span class="badge">${{escapeHtml(item.parent_phone)}}</span>` : '<span class="status-pill failed">연락처 없음</span>'}}
                </span>
              </div>
              <div class="missing-card-row">
                <span class="missing-card-field"><span>알림 이력</span>${{escapeHtml(formatDate(item.notification_at))}}</span>
                <span class="missing-card-field"><span>문구 품질</span>${{renderNotificationQuality(item)}}</span>
              </div>
            </label>
          `).join("")}}
        </div>
        <div class="table-wrap missing-table-wrap">
          <table class="missing-table">
            <thead>
              <tr>
                <th>선택</th>
                <th>학생</th>
                <th>반</th>
                <th>수업</th>
                <th>출석</th>
                <th>학부모 연락처</th>
                <th>알림 상태</th>
                <th>문구 품질</th>
              </tr>
            </thead>
            <tbody>
              ${{page.items.map((item) => `
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
                  <td>${{renderNotificationQuality(item)}}</td>
                </tr>
              `).join("")}}
            </tbody>
          </table>
        </div>
        ${{pagerHtml("missing", page.page, page.totalPages, page.totalItems)}}`;
      updateMissingSelectionSummary();
    }}

    function renderNotificationQuality(item) {{
      const status = item.notification_quality_status || "";
      if (!status) return `<span class="badge">-</span>`;
      const warnings = item.notification_quality_warnings || [];
      return `${{qualityPill(status)}}<br><span class="badge">${{escapeHtml(item.notification_quality_score || 0)}}/100</span>` +
        (warnings.length
          ? `<br>${{warnings.slice(0, 2).map((warning) => `<span class="badge">${{escapeHtml(warning)}}</span>`).join(" ")}}`
          : "");
    }}

    function selectedMissingRows() {{
      return currentMissing.filter((item) => selectedMissingKeys.has(item.selection_key));
    }}

    function selectedMissingSignature(rows) {{
      return (rows || selectedMissingRows())
        .map((item) => item.selection_key || "")
        .filter(Boolean)
        .sort()
        .join("|");
    }}

    function renderMissingHomeworkPreview(data) {{
      lastMissingPreview = {{
        signature: selectedMissingSignature(),
        data,
      }};
      const summary = data.summary || {{}};
      const items = data.items || [];
      const previewRows = items.slice(0, 5).map((item, index) => `
        <li>
          <details class="message-preview-card" ${{index === 0 ? "open" : ""}}>
            <summary>
              <span><strong>${{escapeHtml(index + 1)}}. ${{escapeHtml(item.student_name || "미등록")}}</strong> · ${{escapeHtml(item.parent_phone_masked || "연락처 없음")}}</span>
              <span class="message-preview-meta">${{qualityPill(item.quality_status)}} <span class="badge">${{escapeHtml(item.quality_score || 0)}}/100</span></span>
            </summary>
            <div class="message-preview-body">
              <p>${{escapeHtml(item.message || "생성된 문구가 없습니다.")}}</p>
              ${{item.block_reason ? `<p><span class="badge warn">${{escapeHtml(item.block_reason)}}</span></p>` : ""}}
              ${{(item.quality_warnings || []).length ? `<p>${{(item.quality_warnings || []).slice(0, 3).map((warning) => `<span class="badge">${{escapeHtml(warning)}}</span>`).join(" ")}}</p>` : ""}}
            </div>
          </details>
        </li>
      `).join("");
      const hidden = items.length > 5 ? `<p>${{escapeHtml(items.length - 5)}}명은 생략했습니다. 화면이 길어지지 않도록 상위 5건만 번호로 표시합니다.</p>` : "";
      setActionMode(
        "문구 미리보기",
        `<strong>숙제 알림 문구 미리보기</strong>선택: ${{escapeHtml(summary.total || 0)}}명\\n발송 가능: ${{escapeHtml(summary.dispatchable || 0)}}명\\n문구 품질: ${{qualitySummaryText(summary.ready, summary.review, summary.blocked)}}\\n연락처 없음: ${{escapeHtml(summary.no_parent_phone || 0)}}명\\n발송 방식: ${{escapeHtml(notifyModeLabel(data.notify_mode || notifyMode()))}}<ol class="message-preview-list">${{previewRows}}</ol>${{hidden}}`
      );
      renderMissingCommand();
      if (currentActionFlow === "missing") {{
        renderActionCommand("missing");
      }}
      writeLog("숙제 알림 문구 미리보기를 만들었습니다.", summary);
      return data;
    }}

    async function previewMissingHomeworkSms(logResult = true) {{
      if (!currentMissing.length) {{
        await loadSituation();
      }}
      const selected = selectedMissingRows();
      if (!selected.length) {{
        throw new Error("미제출 조회 후 문구 미리보기 대상을 선택하세요.");
      }}
      const data = await callApi("/api/preview-missing-homework", {{
        window_hours: document.querySelector("#windowHours").value,
        lesson_id: document.querySelector("#lessonId").value,
        selection_keys: selected.map((item) => item.selection_key),
      }});
      renderMissingHomeworkPreview(data);
      if (logResult) {{
        toast("숙제 알림 문구 미리보기를 만들었습니다.", "ok");
      }}
      return data;
    }}

    function updateMissingSelectionSummary() {{
      const selected = selectedMissingRows();
      const visible = filteredMissingItems();
      const withPhone = currentMissing.filter((item) => item.has_parent_phone).length;
      const visibleWithPhone = visible.filter((item) => item.has_parent_phone).length;
      document.querySelector("#missingSelectionSummary").innerHTML =
        `<strong>${{selected.length}}명 선택</strong><span>필터 ${{visible.length}}명 · 전체 ${{currentMissing.length}}명 · 표시 발송 가능 ${{visibleWithPhone}}명 · 전체 발송 가능 ${{withPhone}}명</span>`;
      document.querySelectorAll(".select-missing").forEach((checkbox) => {{
        checkbox.checked = selectedMissingKeys.has(checkbox.dataset.key);
      }});
      renderMissingCommand();
      if (currentActionFlow === "missing") {{
        renderActionCommand("missing");
      }}
    }}

    function renderMissingSelectionList() {{
      const target = document.querySelector("#missingSelectionList");
      const items = filteredMissingItems();
      if (!items.length) {{
        target.innerHTML = `<div class="empty">${{escapeHtml(missingFilterEmptyText())}}</div>`;
        updateMissingSelectionSummary();
        return;
      }}
      const page = paged(items, missingPage, PAGE_SIZE.missing);
      missingPage = page.page;
      target.innerHTML = page.items.map((item) => `
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
            <span>${{item.has_parent_phone ? escapeHtml(item.parent_phone) : "연락처 없음"}} · ${{escapeHtml(statusLabel(item.notification_status))}}</span>
          </span>
        </label>
      `).join("") + pagerHtml("missing", page.page, page.totalPages, page.totalItems);
      updateMissingSelectionSummary();
    }}

    function renderNotifications(data) {{
      currentNotifications = data.items || [];
      notificationPage = 1;
      renderMissingCommand();
      renderNotificationTable();
    }}

    function renderNotificationTable() {{
      const target = document.querySelector("#notificationTable");
      if (!currentNotifications.length) {{
        target.innerHTML = `<div class="empty">아직 알림 생성/발송 기록이 없습니다.</div>`;
        return;
      }}
      const page = paged(currentNotifications, notificationPage, PAGE_SIZE.notifications);
      notificationPage = page.page;
      target.innerHTML = `
        <div class="mobile-card-list notification-card-list">
          ${{page.items.map((item) => `
            <article class="missing-card notification-card">
              <div class="missing-card-head">
                <span></span>
                <span class="missing-card-title">
                  <strong>${{escapeHtml(item.student_name || "미등록")}}</strong>
                  <span>${{escapeHtml(formatDate(item.created_at))}} · ${{escapeHtml(item.student_classin_id || "-")}}</span>
                </span>
                ${{statusPill(item.status)}}
                <span class="missing-card-meta">
                  <span class="badge">${{escapeHtml(item.provider || "-")}}</span>
                  <span class="badge">${{escapeHtml(item.parent_phone || "수신자 없음")}}</span>
                </span>
              </div>
              <div class="missing-card-row">
                <span class="missing-card-field"><span>품질</span>${{renderNotificationQuality({{
                  notification_quality_status: item.quality_status,
                  notification_quality_score: item.quality_score,
                  notification_quality_warnings: item.quality_warnings || [],
                }})}}</span>
                <span class="missing-card-field"><span>문구</span>${{escapeHtml(shorten(item.message || "", 92))}}</span>
              </div>
            </article>
          `).join("")}}
        </div>
        <div class="table-wrap notification-table-wrap">
          <table class="notification-table">
            <thead>
              <tr>
                <th>시간</th>
                <th>학생</th>
                <th>수신자</th>
                <th>상태</th>
                <th>품질</th>
                <th>문구</th>
              </tr>
            </thead>
            <tbody>
              ${{page.items.map((item) => `
                <tr>
                  <td>${{escapeHtml(formatDate(item.created_at))}}</td>
                  <td>${{escapeHtml(item.student_name || "미등록")}}<br><span class="badge">${{escapeHtml(item.student_classin_id || "")}}</span></td>
                  <td>${{escapeHtml(item.parent_phone || "-")}}</td>
                  <td>${{statusPill(item.status)}}<br><span class="badge">${{escapeHtml(item.provider || "-")}}</span></td>
                  <td>${{renderNotificationQuality({{
                    notification_quality_status: item.quality_status,
                    notification_quality_score: item.quality_score,
                    notification_quality_warnings: item.quality_warnings || [],
                  }})}}</td>
                  <td>${{escapeHtml(shorten(item.message || "", 90))}}</td>
                </tr>
              `).join("")}}
            </tbody>
          </table>
        </div>
        ${{pagerHtml("notifications", page.page, page.totalPages, page.totalItems)}}`;
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

    function datetimeLocalToIso(value) {{
      if (!value) return "";
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? value : date.toISOString();
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
        if (button.dataset.tab === tab) {{
          button.setAttribute("aria-current", "page");
        }} else {{
          button.removeAttribute("aria-current");
        }}
      }});
      const activeButton = document.querySelector(`.tab-button[data-tab="${{tab}}"]`);
      activeButton?.scrollIntoView({{ block: "nearest", inline: "center" }});
      document.querySelectorAll(".tab-panel").forEach((panel) => {{
        panel.classList.toggle("active", panel.id === `tab-${{tab}}`);
      }});
      document.querySelector("#viewTitle").textContent = viewTitles[tab] || "대시보드";
      document.querySelector("#viewMeta").textContent = localIsoDate(new Date());
      if (tab === "actions") {{
        renderActionCommand(currentActionFlow);
      }} else if (tab === "data") {{
        renderDataCommand();
      }} else if (tab === "missing") {{
        renderMissingCommand();
      }} else if (tab === "schedule") {{
        renderScheduleCommand();
      }} else if (tab === "reports") {{
        renderReportCommand();
        renderExamCommand();
      }} else if (tab === "memo") {{
        renderAgentCommand();
      }} else if (tab === "settings") {{
        renderReadinessCommand();
      }}
    }}

    function setActionMode(mode, html) {{
      document.querySelector("#actionModeBadge").textContent = mode;
      document.querySelector("#actionPreview").innerHTML = html;
    }}

    function notifyMode() {{
      return ((status.output || {{}}).notify_mode || "-");
    }}

    function notifyModeLabel(mode = notifyMode()) {{
      const labels = {{
        dry_run: "외부 발송 없음",
        live: "실제 발송",
        "-": "미설정",
      }};
      return labels[mode] || mode || "미설정";
    }}

    const actionFlows = {{
      missing: {{
        title: "숙제 미제출 문자 발송",
        detail: "미제출 학생을 조회하고, 문구 품질을 확인한 뒤 선택 대상에게만 발송합니다.",
        primaryAction: "refreshMissing",
        primaryLabel: "미제출 조회",
        gate: "실제 발송 전에는 연락처, 문구 품질, 발송 방식이 모두 통과되어야 합니다.",
        steps: [
          ["조회", "오늘 범위와 수업 ID를 확인"],
          ["문구 검토", "AI 문구와 품질 경고 확인"],
          ["발송", "선택 대상만 검토 기록 또는 실제 발송"],
        ],
        meta() {{
          const selected = selectedMissingRows().length;
          const withPhone = currentMissing.filter((item) => item.has_parent_phone).length;
          return [
            ["조회", `${{currentMissing.length}}명`],
            ["선택", `${{selected}}명`],
            ["발송 가능", `${{withPhone}}명`],
            ["발송 방식", notifyModeLabel()],
          ];
        }},
      }},
      schedule: {{
        title: "스케줄표로 수업·숙제 생성",
        detail: "CSV나 표를 먼저 검토해서 파싱 오류를 잡고, 확인된 데이터만 ClassIn에 생성합니다.",
        primaryAction: "runScheduleImportDryRun",
        primaryLabel: "먼저 검토",
        gate: "ClassIn 대시보드와 API 동시 조작을 피하고, 실제 생성 전 검토 결과를 확인하세요.",
        steps: [
          ["입력", "CSV 파일 또는 표 붙여넣기"],
          ["검토", "파싱 결과와 오류 확인"],
          ["생성", "확인 후 수업·숙제 생성"],
        ],
        meta() {{
          const text = document.querySelector("#importText")?.value || "";
          const lineCount = text.trim() ? text.trim().split(/\\n+/).length : 0;
          return [
            ["입력", lineCount ? `${{lineCount}}줄` : "대기"],
            ["파일", document.querySelector("#importFile")?.files?.[0]?.name || "-"],
            ["다음", lineCount ? "검토" : "입력"],
          ];
        }},
      }},
      report: {{
        title: "반별 리포트 생성",
        detail: "반과 주차를 고른 뒤 학생 대상을 확인하고, 필요한 학생만 주간 리포트 초안으로 생성합니다.",
        primaryAction: "loadReportTargets",
        primaryLabel: "대상 조회",
        gate: "품질 차단 리포트는 기본 승인에서 제외하고, 근거·다음 액션·표현 안전을 먼저 보강하세요.",
        steps: [
          ["반·주차", "반 선택과 주 시작일 확인"],
          ["대상", "생성할 학생 선택"],
          ["생성", "선택 리포트 생성 후 품질 검토"],
        ],
        meta() {{
          const selected = selectedReportTargets().length;
          const classes = new Set(currentReportTargets.map((item) => item.class_name).filter(Boolean));
          return [
            ["조회", `${{currentReportTargets.length}}명`],
            ["선택", `${{selected}}명`],
            ["반", `${{classes.size || 0}}개`],
            ["주차", document.querySelector("#weekDate")?.value || "-"],
          ];
        }},
      }},
    }};
    let currentActionFlow = "missing";

    function activeActionStep(flowKey, index) {{
      if (flowKey === "missing") {{
        if (!currentMissing.length) return index === 0;
        if (!lastMissingPreview) return index === 1;
        return index === 2;
      }}
      if (flowKey === "schedule") {{
        const text = document.querySelector("#importText")?.value.trim() || "";
        const preview = document.querySelector("#importPreview")?.textContent || "";
        if (!text) return index === 0;
        if (preview.includes("스케줄 검토") || preview.includes("수업·숙제 생성")) return index === 2;
        return index === 1;
      }}
      if (flowKey === "report") {{
        if (!currentReportTargets.length) return index === 0;
        return index === 2;
      }}
      return index === 0;
    }}

    function renderActionCommand(flowKey) {{
      const target = document.querySelector("#actionCommand");
      if (!target) return;
      currentActionFlow = actionFlows[flowKey] ? flowKey : currentActionFlow;
      const flow = actionFlows[currentActionFlow];
      document.querySelectorAll(".core-button[data-flow]").forEach((button) => {{
        button.classList.toggle("active", button.dataset.flow === currentActionFlow);
      }});
      const meta = flow.meta();
      target.innerHTML = `
        <div class="action-command-main">
          <span class="command-label">현재 업무</span>
          <strong>${{escapeHtml(flow.title)}}</strong>
          <p>${{escapeHtml(flow.detail)}}</p>
          <div class="command-meta">
            ${{meta.map(([label, value]) => `<span class="badge">${{escapeHtml(label)}} ${{escapeHtml(value)}}</span>`).join("")}}
          </div>
          <div class="action-command-actions">
            <button data-primary-action="${{escapeHtml(flow.primaryAction)}}">${{escapeHtml(flow.primaryLabel)}}</button>
            <button data-action="refreshDiagnostics" class="secondary">설정 점검</button>
          </div>
          <div class="command-gate"><span>안전 게이트</span><p>${{escapeHtml(flow.gate)}}</p></div>
        </div>
        <div class="action-command-side">
          <span class="command-label">실행 순서</span>
          <div class="workflow-steps">
            ${{flow.steps.map((step, index) => `
              <div class="workflow-step ${{activeActionStep(currentActionFlow, index) ? "active" : ""}}">
                <span>${{escapeHtml(index + 1)}}</span>
                <strong>${{escapeHtml(step[0])}}</strong>
                <p>${{escapeHtml(step[1])}}</p>
              </div>
            `).join("")}}
          </div>
        </div>
      `;
    }}

    function focusMissingCore() {{
      activateTab("actions");
      renderActionCommand("missing");
      document.querySelector("[data-window-hours].active").focus();
      const withPhone = currentMissing.filter((item) => item.has_parent_phone).length;
      const selected = selectedMissingRows();
      setActionMode(
        "미제출 문자",
        `<strong>숙제 미제출 문자 발송</strong>조회: ${{currentMissing.length}}명\\n선택: ${{selected.length}}명\\n발송 가능: ${{withPhone}}명\\n발송 방식: ${{escapeHtml(notifyModeLabel())}}`
      );
    }}

    function focusScheduleCore() {{
      activateTab("actions");
      renderActionCommand("schedule");
      document.querySelector("#importText").focus();
      document.querySelector("#importPreview").innerHTML =
        `<strong>스케줄표로 수업·숙제 생성</strong>스케줄표를 붙여넣고 먼저 검토한 뒤 생성하세요.`;
    }}

    function focusReportCore() {{
      activateTab("actions");
      renderActionCommand("report");
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
      if (currentActionFlow === "report") {{
        renderActionCommand("report");
      }}
    }}

    function renderReportTargets(data) {{
      const items = data.items || [];
      currentReportTargets = items;
      selectedReportIds = new Set(items.map((item) => item.student_classin_id).filter(Boolean));
      reportTargetPage = 1;
      renderReportTargetList();
    }}

    function renderReportTargetList() {{
      const target = document.querySelector("#reportTargetList");
      if (!currentReportTargets.length) {{
        target.innerHTML = `<div class="empty">조회된 리포트 대상 학생이 없습니다.</div>`;
        updateReportSelectionSummary();
        return;
      }}
      const page = paged(currentReportTargets, reportTargetPage, PAGE_SIZE.reportTargets);
      reportTargetPage = page.page;
      target.innerHTML = page.items.map((item) => `
        <label class="selectable-row">
          <input
            type="checkbox"
            class="select-report"
            data-student-id="${{escapeHtml(item.student_classin_id || "")}}"
            ${{selectedReportIds.has(item.student_classin_id) ? "checked" : ""}}
          >
          <span>
            <strong>${{escapeHtml(item.student_name)}} · ${{escapeHtml(item.class_name || "-")}}</strong>
            <span>ClassIn ${{escapeHtml(item.student_classin_id || "-")}}</span>
            <span>${{item.has_parent_phone ? "학부모 연락처 있음" : "학부모 연락처 없음"}}</span>
          </span>
        </label>
      `).join("") + pagerHtml("reportTargets", page.page, page.totalPages, page.totalItems);
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
      await loadReportCompositions(false);
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

    function countByStatus(items, field) {{
      return (items || []).reduce((acc, item) => {{
        const key = item[field] || "pending";
        acc[key] = (acc[key] || 0) + 1;
        return acc;
      }}, {{}});
    }}

    function reportCommandState() {{
      const composition = countByStatus(currentReportCompositions, "readiness_status");
      const drafts = countByStatus(currentWeeklyDrafts, "quality_status");
      const approved = currentWeeklyDrafts.filter((item) => item.approved).length;
      const pendingDrafts = Math.max(0, currentWeeklyDrafts.length - approved);
      const needsReview = (composition.review || 0)
        + (composition.blocked || 0)
        + (drafts.review || 0)
        + (drafts.blocked || 0);
      let phase = "구성 점검";
      let detail = "학생별 출결·숙제·시험·메모 구성을 먼저 점검하세요.";
      let primaryAction = "refreshReportCompositions";
      let primaryLabel = "구성 새로고침";
      let tone = "";
      if (currentWeeklyDrafts.length) {{
        phase = pendingDrafts ? "품질 검토" : "승인 완료";
        detail = pendingDrafts
          ? "HTML 드래프트 품질 상태를 확인하고 통과 항목부터 승인하세요."
          : "현재 로드된 드래프트는 모두 승인 상태입니다.";
        primaryAction = pendingDrafts ? "refreshWeeklyDrafts" : "generateWeekly";
        primaryLabel = pendingDrafts ? "드래프트 새로고침" : "새 드래프트 생성";
        tone = needsReview ? "warn" : "ok";
      }} else if (currentReportCompositions.length) {{
        phase = needsReview ? "구성 보강" : "초안 생성";
        detail = needsReview
          ? "확인 필요·차단 학생은 근거와 다음 액션을 보강한 뒤 초안을 만드세요."
          : "구성이 준비된 학생은 주간 리포트 초안 생성으로 넘어갈 수 있습니다.";
        primaryAction = needsReview ? "refreshReportCompositions" : "generateWeekly";
        primaryLabel = needsReview ? "구성 다시 보기" : "전체 주간 드래프트";
        tone = needsReview ? "warn" : "ok";
      }}
      if ((drafts.blocked || 0) || (composition.blocked || 0)) {{
        tone = "danger";
      }}
      return {{
        phase,
        detail,
        primaryAction,
        primaryLabel,
        tone,
        meta: [
          ["구성", `${{currentReportCompositions.length}}명`],
          ["통과", `${{(composition.ready || 0) + (drafts.ready || 0)}}`],
          ["확인 필요", `${{(composition.review || 0) + (drafts.review || 0)}}`],
          ["차단", `${{(composition.blocked || 0) + (drafts.blocked || 0)}}`],
          ["승인 대기", `${{pendingDrafts}}`],
        ],
      }};
    }}

    function renderReportCommand() {{
      const target = document.querySelector("#reportCommand");
      if (!target) return;
      const state = reportCommandState();
      target.className = `action-command report-command ${{escapeHtml(state.tone)}}`;
      const steps = [
        ["구성", "학생별 근거와 섹션 준비도 점검"],
        ["초안", "HTML 드래프트 생성"],
        ["품질", "통과·확인 필요·차단 확인"],
        ["승인", "승인된 드래프트만 Notion 아카이브"],
      ];
      const activeIndex = state.phase === "구성 점검" || state.phase === "구성 보강"
        ? 0
        : state.phase === "초안 생성"
          ? 1
          : state.phase === "품질 검토"
            ? 2
            : 3;
      target.innerHTML = `
        <div class="action-command-main">
          <span class="command-label">리포트 운영 상태</span>
          <strong>${{escapeHtml(state.phase)}}</strong>
          <p>${{escapeHtml(state.detail)}}</p>
          <div class="command-meta">
            ${{state.meta.map(([label, value]) => `<span class="badge">${{escapeHtml(label)}} ${{escapeHtml(value)}}</span>`).join("")}}
          </div>
          <div class="action-command-actions">
            <button data-primary-action="${{escapeHtml(state.primaryAction)}}">${{escapeHtml(state.primaryLabel)}}</button>
            <button data-primary-action="refreshWeeklyDrafts" class="secondary">품질 큐 보기</button>
            <button data-primary-action="approveWeekly" class="secondary">승인</button>
          </div>
          <div class="command-gate"><span>승인 원칙</span><p>HTML 드래프트를 검토한 뒤 승인된 것만 Notion에 아카이브합니다. 품질 차단 항목은 기본 승인에서 제외하세요.</p></div>
        </div>
        <div class="action-command-side">
          <span class="command-label">리포트 흐름</span>
          <div class="workflow-steps">
            ${{steps.map((step, index) => `
              <div class="workflow-step ${{index === activeIndex ? "active" : ""}}">
                <span>${{escapeHtml(index + 1)}}</span>
                <strong>${{escapeHtml(step[0])}}</strong>
                <p>${{escapeHtml(step[1])}}</p>
              </div>
            `).join("")}}
          </div>
        </div>
      `;
    }}

    function examFieldValue(selector) {{
      return document.querySelector(selector)?.value.trim() || "";
    }}

    function examCommandState() {{
      const examName = examFieldValue("#examName");
      const examDate = examFieldValue("#examDate");
      const examCsv = examFieldValue("#examCsvText");
      const examDryRun = document.querySelector("#examDryRun")?.checked ?? true;
      const courseId = examFieldValue("#answerSheetCourseId");
      const unitId = examFieldValue("#answerSheetUnitId");
      const answerName = examFieldValue("#answerSheetName") || "OMR 답안지";
      const answerDryRun = document.querySelector("#answerSheetDryRun")?.checked ?? true;
      const release = document.querySelector("#answerSheetRelease")?.checked ?? false;
      const examSummary = lastExamImportResult?.summary || {{}};
      const unresolved = Number(examSummary.unresolved || 0) + Number(examSummary.errors || 0);

      let phase = "시험 운영 대기";
      let detail = "OMR 답안지는 생성 전 검토로 확인하고, 시험 CSV는 먼저 학생 매칭을 검토하세요.";
      let primaryAction = "focusExamImport";
      let primaryLabel = "시험 CSV 준비";
      let tone = "warn";
      let activeIndex = 0;

      if (currentExamFlow === "answer" && lastAnswerSheetResult) {{
        phase = lastAnswerSheetResult.dry_run ? "OMR 생성 전 검토 완료" : "OMR 생성 완료";
        detail = lastAnswerSheetResult.dry_run
          ? "생성 내용 확인이 끝났습니다. 실제 생성 전 ClassIn 연결 점검을 다시 확인하세요."
          : "ClassIn 활동 생성 결과를 확인했습니다. OMR 점수 수신을 데이터 탭에서 점검하세요.";
        primaryAction = "refreshWebhookInbox";
        primaryLabel = "OMR 수신 확인";
        tone = "ok";
        activeIndex = 3;
      }} else if (currentExamFlow === "answer" && courseId && unitId) {{
        phase = answerDryRun ? "OMR 생성 전 검토" : "OMR 실제 생성 전 확인";
        detail = answerDryRun
          ? "강좌 ID와 단원 ID가 준비되었습니다. 먼저 생성 내용을 검토하세요."
          : "ClassIn에 실제 OMR 활동을 생성하려는 상태입니다. 연결 점검과 교사 식별번호를 확인하세요.";
        primaryAction = "createAnswerSheet";
        primaryLabel = answerDryRun ? "OMR 검토" : "OMR 생성";
        tone = answerDryRun ? "ok" : "danger";
        activeIndex = answerDryRun ? 0 : 3;
      }} else if (lastExamImportResult && lastExamImportResult.dry_run && unresolved) {{
        phase = "학생 매칭 보강";
        detail = "매칭 검토에서 확인할 학생이 남았습니다. 학생 정보와 반 이름을 보강한 뒤 다시 확인하세요.";
        primaryAction = "importExamResults";
        primaryLabel = "매칭 다시 검토";
        tone = "danger";
        activeIndex = 2;
      }} else if (lastExamImportResult && lastExamImportResult.dry_run) {{
        phase = "시험 적재 준비";
        detail = "학생 매칭 검토가 통과했습니다. Notion 시험 DB에 쓰기 전 시험명과 시험일을 확인하세요.";
        primaryAction = "importExamResults";
        primaryLabel = "시험 결과 저장";
        tone = "ok";
        activeIndex = 2;
      }} else if (examCsv && examName && examDate) {{
        phase = examDryRun ? "시험 매칭 검토" : "Notion 적재 전 확인";
        detail = examDryRun
          ? "CSV와 시험 정보가 준비되었습니다. 먼저 학생 매칭을 확인하세요."
          : "검토 모드 없이 저장하려는 상태입니다. 매칭 확인이 끝났는지 확인하세요.";
        primaryAction = "importExamResults";
        primaryLabel = examDryRun ? "매칭 검토" : "시험 결과 저장";
        tone = examDryRun ? "ok" : "warn";
        activeIndex = examDryRun ? 1 : 2;
      }} else if (lastAnswerSheetResult) {{
        phase = lastAnswerSheetResult.dry_run ? "OMR 생성 전 검토 완료" : "OMR 생성 완료";
        detail = lastAnswerSheetResult.dry_run
          ? "생성 내용 확인이 끝났습니다. 실제 생성 전 ClassIn 연결 점검을 다시 확인하세요."
          : "ClassIn 활동 생성 결과를 확인했습니다. OMR 점수 수신을 데이터 탭에서 점검하세요.";
        primaryAction = "refreshWebhookInbox";
        primaryLabel = "OMR 수신 확인";
        tone = "ok";
        activeIndex = 3;
      }} else if (courseId && unitId) {{
        phase = answerDryRun ? "OMR 생성 전 검토" : "OMR 실제 생성 전 확인";
        detail = answerDryRun
          ? "강좌 ID와 단원 ID가 준비되었습니다. 먼저 생성 내용을 검토하세요."
          : "ClassIn에 실제 OMR 활동을 생성하려는 상태입니다. 연결 점검과 교사 식별번호를 확인하세요.";
        primaryAction = "createAnswerSheet";
        primaryLabel = answerDryRun ? "OMR 검토" : "OMR 생성";
        tone = answerDryRun ? "ok" : "danger";
        activeIndex = answerDryRun ? 0 : 3;
      }}

      return {{
        phase,
        detail,
        primaryAction,
        primaryLabel,
        tone,
        activeIndex,
        meta: [
          ["시험", examName || answerName],
          ["시험일", examDate || "-"],
          ["CSV", examCsv ? `${{inspectDelimited(examCsv).rows}}행` : "대기"],
          ["강좌/단원", courseId && unitId ? "준비" : "대기"],
          ["검토 모드", examDryRun && answerDryRun ? "켜짐" : "확인"],
          ["게시", release ? "예" : "아니오"],
        ],
      }};
    }}

    function renderExamCommand() {{
      const target = document.querySelector("#examCommand");
      if (!target) return;
      const state = examCommandState();
      target.className = `action-command exam-command ${{escapeHtml(state.tone)}}`;
      const steps = [
        ["OMR", "생성 전 내용 검토"],
        ["매칭", "시험 CSV와 학생 정보 확인"],
        ["적재", "Notion 시험 DB 저장 전 확인"],
        ["반영", "미응시·리포트 근거로 연결"],
      ];
      target.innerHTML = `
        <div class="action-command-main">
          <span class="command-label">시험·OMR 운영 상태</span>
          <strong>${{escapeHtml(state.phase)}}</strong>
          <p>${{escapeHtml(state.detail)}}</p>
          <div class="command-meta">
            ${{state.meta.map(([label, value]) => `<span class="badge">${{escapeHtml(label)}} ${{escapeHtml(value)}}</span>`).join("")}}
          </div>
          <div class="action-command-actions">
            <button data-primary-action="${{escapeHtml(state.primaryAction)}}">${{escapeHtml(state.primaryLabel)}}</button>
            <button data-primary-action="focusExamImport" class="secondary">시험 CSV</button>
            <button data-primary-action="focusAnswerSheet" class="secondary">OMR</button>
            <button data-primary-action="refreshWebhookInbox" class="secondary">OMR 수신함</button>
          </div>
          <div class="command-gate"><span>시험 원칙</span><p>시험 결과는 반드시 학생 매칭을 먼저 확인한 뒤 Notion에 저장하세요. OMR 실제 생성은 ClassIn 연결 점검과 대시보드 동시 조작 금지 확인 후 진행합니다.</p></div>
        </div>
        <div class="action-command-side">
          <span class="command-label">시험 흐름</span>
          <div class="workflow-steps">
            ${{steps.map((step, index) => `
              <div class="workflow-step ${{index === state.activeIndex ? "active" : ""}}">
                <span>${{escapeHtml(index + 1)}}</span>
                <strong>${{escapeHtml(step[0])}}</strong>
                <p>${{escapeHtml(step[1])}}</p>
              </div>
            `).join("")}}
          </div>
        </div>
      `;
    }}

    function focusExamImport() {{
      currentExamFlow = "exam";
      activateTab("reports");
      renderExamCommand();
      document.querySelector("#examImportPanel")?.scrollIntoView({{ block: "start", behavior: "smooth" }});
      document.querySelector("#examName")?.focus();
    }}

    function focusAnswerSheet() {{
      currentExamFlow = "answer";
      activateTab("reports");
      renderExamCommand();
      document.querySelector("#answerSheetPanel")?.scrollIntoView({{ block: "start", behavior: "smooth" }});
      document.querySelector("#answerSheetCourseId")?.focus();
    }}

    function renderReportCompositions(data) {{
      const summary = data.summary || {{}};
      currentReportCompositions = data.items || [];
      reportCompositionPage = 1;
      const summaryCards = [
        ["전체", summary.total || 0, ""],
        ["통과", summary.ready || 0, "ok"],
        ["확인 필요", summary.review || 0, "warn"],
        ["차단", summary.blocked || 0, "failed"],
        ["시험", summary.with_exam_signal || 0, "info"],
        ["메모", summary.with_memo_context || 0, "info"],
      ];
      document.querySelector("#reportCompositionSummary").innerHTML = summaryCards.map(([label, value, tone]) => `
        <div class="diagnostic-count ${{escapeHtml(tone)}}">
          <span>${{escapeHtml(label)}}</span>
          <strong>${{escapeHtml(value)}}</strong>
        </div>
      `).join("");
      renderReportCommand();
      renderReportCompositionTable();
    }}

    function renderReportCompositionTable() {{
      const target = document.querySelector("#reportCompositionTable");
      if (!currentReportCompositions.length) {{
        target.innerHTML = `<div class="empty">조회된 리포트 구성이 없습니다.</div>`;
        return;
      }}
      const page = paged(currentReportCompositions, reportCompositionPage, PAGE_SIZE.reportCompositions);
      reportCompositionPage = page.page;
      target.innerHTML = `
        <div class="table-wrap is-compact">
          <table class="compact-table">
            <thead>
              <tr>
                <th>학생</th>
                <th>준비도</th>
                <th>핵심 판단</th>
                <th>근거</th>
                <th>구성 섹션</th>
                <th>보강</th>
                <th>실행</th>
              </tr>
            </thead>
            <tbody>
              ${{page.items.map((item) => {{
                const counts = item.source_counts || {{}};
                const sections = item.sections || [];
                const warning = (item.readiness_warnings || [])[0] || "";
                return `
                  <tr>
                    <td>${{escapeHtml(item.student_name || "미등록")}}<br><span class="badge">${{escapeHtml(item.student_classin_id || "-")}}</span></td>
                    <td>${{qualityPill(item.readiness_status)}}<br><span class="badge">${{escapeHtml(item.readiness_score || 0)}}/100</span></td>
                    <td>${{escapeHtml(item.focus || "-")}}<br><span class="badge">${{escapeHtml(item.context_summary || "-")}}</span></td>
                    <td>
                      <span class="badge">ClassIn ${{escapeHtml(counts.classin_lessons || 0)}}</span>
                      <span class="badge">시험 ${{escapeHtml((counts.exam_results || 0) + (counts.offline_scores || 0))}}</span>
                      <span class="badge">메모 ${{escapeHtml(counts.memos || 0)}}</span>
                    </td>
                    <td><div class="badge-strip">${{sections.map((section) => `<span class="badge">${{escapeHtml(section.title)}} · ${{escapeHtml(statusLabel(section.status))}}</span>`).join("")}}</div></td>
                    <td><div class="badge-strip">${{warning ? `<span class="badge">${{escapeHtml(warning)}}</span>` : '<span class="badge ok">없음</span>'}}</div></td>
                    <td>
                      <div class="inline-actions">
                        <button
                          data-action="openStudentBrief"
                          data-student-id="${{escapeHtml(item.student_classin_id || "")}}"
                          data-student-name="${{escapeHtml(item.student_name || "")}}"
                          data-class-name="${{escapeHtml(item.class_name || "")}}"
                          class="secondary"
                        >학생 보기</button>
                        <button data-action="generateStudentReportPack" data-student-id="${{escapeHtml(item.student_classin_id || "")}}" class="secondary">초안</button>
                      </div>
                    </td>
                  </tr>
                `;
              }}).join("")}}
            </tbody>
          </table>
        </div>
        ${{pagerHtml("reportCompositions", page.page, page.totalPages, page.totalItems)}}
      `;
    }}

    async function loadReportCompositions(logResult = true) {{
      const params = new URLSearchParams();
      const week = document.querySelector("#weekDate").value;
      const className = document.querySelector("#reportClassName").value.trim();
      if (week) params.set("week", week);
      if (className) params.set("class_name", className);
      const response = await fetch(`/api/report-compositions?${{params.toString()}}`);
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || "report composition load failed");
      }}
      renderReportCompositions(data);
      if (logResult) {{
        writeLog("개별 리포트 구성을 갱신했습니다.", data.summary);
      }}
      return data;
    }}

    function reportPackStudentId(button) {{
      return (button && button.dataset.studentId)
        || lastStudentReportId
        || (currentReportCompositions[0] && currentReportCompositions[0].student_classin_id)
        || "";
    }}

    async function generateStudentReportPack(button) {{
      if (!currentReportCompositions.length) {{
        await loadReportCompositions(false);
      }}
      const studentId = reportPackStudentId(button);
      if (!studentId) {{
        throw new Error("초안을 만들 학생을 먼저 조회하세요.");
      }}
      const params = new URLSearchParams({{ student_classin_id: studentId }});
      const week = document.querySelector("#weekDate").value;
      const className = document.querySelector("#reportClassName").value.trim();
      if (week) params.set("week", week);
      if (className) params.set("class_name", className);
      const response = await fetch(`/api/student-report-pack?${{params.toString()}}`);
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || "student report pack failed");
      }}
      lastStudentReportMarkdown = data.markdown || "";
      lastStudentReportId = data.student_classin_id || studentId;
      setPreviewText("#studentReportPreview", lastStudentReportMarkdown || "개별 리포트 초안 내용이 없습니다.");
      renderReportCommand();
      writeLog("개별 리포트 초안을 만들었습니다.", {{
        student: data.student_name,
        readiness: data.readiness_status,
        score: data.readiness_score,
        summary: data.summary,
      }});
      toast("개별 리포트 초안을 만들었습니다.", "ok");
      return data;
    }}

    async function copyStudentReportPack(button) {{
      const requestedId = reportPackStudentId(button);
      if (!lastStudentReportMarkdown || (requestedId && requestedId !== lastStudentReportId)) {{
        await generateStudentReportPack(button);
      }}
      if (!lastStudentReportMarkdown) {{
        throw new Error("복사할 개별 리포트 초안이 없습니다.");
      }}
      const copyMode = await copyTextWithFallback(lastStudentReportMarkdown, "#studentReportPreview");
      writeLog("개별 리포트 초안을 복사했습니다.", {{
        student_classin_id: lastStudentReportId,
        chars: lastStudentReportMarkdown.length,
        mode: copyMode,
      }});
      toast(copyMode === "selected" ? "본문을 선택했습니다. Ctrl+C로 복사하세요." : "개별 리포트 초안을 복사했습니다.", "ok");
    }}

    function renderWeeklyDrafts(data) {{
      const summary = data.summary || {{}};
      currentWeeklyDrafts = data.items || [];
      currentWeeklyDraftsExists = Boolean(data.exists);
      weeklyDraftPage = 1;
      const summaryCards = [
        ["전체", summary.total || 0, ""],
        ["승인 대기", summary.pending || 0, "warn"],
        ["통과", summary.ready_unapproved || 0, "ok"],
        ["확인 필요", summary.review_unapproved || 0, "warn"],
        ["차단", summary.blocked_unapproved || 0, "failed"],
      ];
      document.querySelector("#weeklyDraftSummary").innerHTML = summaryCards.map(([label, value, tone]) => `
        <div class="diagnostic-count ${{escapeHtml(tone)}}">
          <span>${{escapeHtml(label)}}</span>
          <strong>${{escapeHtml(value)}}</strong>
        </div>
      `).join("");
      renderReportCommand();
      renderWeeklyDraftTable();
    }}

    function renderWeeklyDraftTable() {{
      const target = document.querySelector("#weeklyDraftTable");
      if (!currentWeeklyDraftsExists) {{
        target.innerHTML = `<div class="empty">해당 주차 드래프트 인덱스가 없습니다.</div>`;
        return;
      }}
      if (!currentWeeklyDrafts.length) {{
        target.innerHTML = `<div class="empty">해당 주차에 생성된 드래프트가 없습니다.</div>`;
        return;
      }}
      const page = paged(currentWeeklyDrafts, weeklyDraftPage, PAGE_SIZE.weeklyDrafts);
      weeklyDraftPage = page.page;
      target.innerHTML = `
        <div class="table-wrap is-compact">
          <table class="compact-table">
            <thead>
              <tr>
                <th>학생</th>
                <th>품질</th>
                <th>승인</th>
                <th>경고</th>
                <th>학생 맥락</th>
                <th>HTML</th>
              </tr>
            </thead>
            <tbody>
              ${{page.items.map((item) => {{
                const warnings = item.quality_warnings || [];
                const preview = item.preview_url
                  ? `<a href="${{escapeHtml(item.preview_url)}}" target="_blank" rel="noreferrer">열기</a>`
                  : escapeHtml(item.html_path || "-");
                return `
                  <tr>
                    <td>${{escapeHtml(item.student_name || "미등록")}}<br><span class="badge">${{escapeHtml(item.student_classin_id || "-")}}</span></td>
                    <td>${{qualityPill(item.quality_status)}}<br><span class="badge">${{escapeHtml(item.quality_score || 0)}}/100</span></td>
                    <td>${{item.approved ? statusPill("approved") : statusPill("pending")}}</td>
                    <td><div class="badge-strip">${{warnings.length ? warnings.map((warning) => `<span class="badge">${{escapeHtml(warning)}}</span>`).join("") : '<span class="badge ok">없음</span>'}}</div></td>
                    <td>${{escapeHtml(item.report_context_summary || "-")}}</td>
                    <td>${{preview}}</td>
                  </tr>
                `;
              }}).join("")}}
            </tbody>
          </table>
        </div>
        ${{pagerHtml("weeklyDrafts", page.page, page.totalPages, page.totalItems)}}
      `;
    }}

    async function loadWeeklyDrafts(logResult = true) {{
      const params = new URLSearchParams();
      const week = document.querySelector("#weekDate").value;
      if (week) params.set("week", week);
      const response = await fetch(`/api/weekly-drafts?${{params.toString()}}`);
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || "weekly draft load failed");
      }}
      renderWeeklyDrafts(data);
      if (logResult) {{
        writeLog("주간 드래프트 큐를 갱신했습니다.", data.summary);
      }}
      return data;
    }}

    async function approveWeekly() {{
      const data = await callApi("/api/approve-weekly", {{
        week: document.querySelector("#weekDate").value,
        force_blocked_quality: document.querySelector("#forceBlockedQuality").checked,
      }});
      writeLog(data.message, data);
      await refreshStatus();
      await loadWeeklyDrafts(false);
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
        const signature = selectedMissingSignature(selected);
        let preview = lastMissingPreview && lastMissingPreview.signature === signature
          ? lastMissingPreview.data
          : null;
        if (!preview) {{
          preview = await previewMissingHomeworkSms(false);
        }}
        const previewSummary = preview.summary || {{}};
        if ((previewSummary.live_blocked || 0) > 0 || (previewSummary.dispatchable || 0) !== selected.length) {{
          throw new Error("실제 발송 전 품질 통과 문구와 보호자 연락처를 먼저 보강하세요.");
        }}
        const ok = window.confirm(
          `실제 문자 발송 모드입니다. 미리보기 통과 ${{previewSummary.dispatchable || selected.length}}명에게 발송합니다.`
        );
        if (!ok) return;
      }}
      const result = await callApi("/api/sweep-missing-homework", {{
        window_hours: document.querySelector("#windowHours").value,
        lesson_id: document.querySelector("#lessonId").value,
        selection_keys: selected.map((item) => item.selection_key),
      }});
      await loadSituation();
      await loadOpsHub();
      lastMissingPreview = null;
      setActionMode(
        "문자 처리",
        `<strong>숙제 미제출 문자 발송</strong>선택: ${{selected.length}}명\\n처리: ${{escapeHtml(result.count || 0)}}건\\n모드: ${{escapeHtml(mode)}}`
      );
      writeLog(result.message, result);
    }}

    function selectAllMissing() {{
      selectedMissingKeys = new Set(
        filteredMissingItems()
          .filter((item) => item.has_parent_phone)
          .map((item) => item.selection_key)
          .filter(Boolean)
      );
      lastMissingPreview = null;
      updateMissingSelectionSummary();
      writeLog(`현재 필터에서 문자 발송 대상 ${{selectedMissingKeys.size}}명을 선택했습니다.`);
    }}

    function clearMissingSelection() {{
      selectedMissingKeys = new Set();
      lastMissingPreview = null;
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
      if (currentActionFlow === "schedule") {{
        renderActionCommand("schedule");
      }}
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
      lastExamImportResult = null;
      renderExamCommand();
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
      if (currentActionFlow === "schedule") {{
        renderActionCommand("schedule");
      }}
    }}

    async function runScheduleImportDryRun() {{
      const text = document.querySelector("#importText").value.trim();
      if (!text) throw new Error("스케줄 사전 검토에 사용할 CSV / 표 데이터가 필요합니다.");
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
      await loadWeeklyDrafts(false);
    }}

    function renderExamImportResult(data) {{
      lastExamImportResult = data;
      const summary = data.summary || {{}};
      const errors = data.errors || [];
      document.querySelector("#examPreview").innerHTML =
        `<strong>${{escapeHtml(data.dry_run ? "시험 결과 매칭 검토" : "시험 결과 가져오기")}}</strong>` +
        Object.entries(summary)
          .map(([key, value]) => `${{escapeHtml(key)}}: ${{escapeHtml(value)}}`)
          .join("\\n") +
        (errors.length
          ? `\\n\\n오류\\n${{escapeHtml(errors.slice(0, 8).join("\\n"))}}`
          : "");
      renderExamCommand();
    }}

    function renderAnswerSheetResult(data) {{
      lastAnswerSheetResult = data;
      document.querySelector("#answerSheetPreview").innerHTML =
        `<strong>${{escapeHtml(data.dry_run ? "OMR 생성 전 검토" : "OMR 답안지 생성")}}</strong>` +
        `활동명: ${{escapeHtml(data.name || "-")}}\\n` +
        `활동 ID: ${{escapeHtml(data.activity_id || "생성 전 검토")}}\\n` +
        `게시: ${{data.released ? "예" : "아니오"}}`;
      renderExamCommand();
    }}

    async function createAnswerSheet() {{
      const dryRun = document.querySelector("#answerSheetDryRun").checked;
      if (!dryRun) {{
        const ok = window.confirm("ClassIn에 OMR 답안지 활동을 실제로 생성합니다.");
        if (!ok) return;
      }}
      const data = await callApi("/api/create-answer-sheet", {{
        course_id: document.querySelector("#answerSheetCourseId").value,
        unit_id: document.querySelector("#answerSheetUnitId").value,
        name: document.querySelector("#answerSheetName").value,
        teacher_uid: document.querySelector("#answerSheetTeacherUid").value,
        start_at: datetimeLocalToIso(document.querySelector("#answerSheetStart").value),
        end_at: datetimeLocalToIso(document.querySelector("#answerSheetEnd").value),
        dry_run: dryRun,
        release: document.querySelector("#answerSheetRelease").checked,
      }});
      renderAnswerSheetResult(data);
      writeLog(data.message, data);
    }}

    function renderSsoLinkResult(data) {{
      lastSsoLink = data.link || "";
      lastMaskedSsoLink = data.masked_link || "";
      const lifeTime = Number(data.life_time || 0);
      const lifeLabel = lifeTime >= 3600
        ? `${{Math.round(lifeTime / 3600)}}시간`
        : `${{lifeTime}}초`;
      const linkMarkup = lastSsoLink
        ? `\\n\\n<a href="${{escapeHtml(lastSsoLink)}}" target="_blank" rel="noopener">접속 링크 열기</a>`
        : "";
      document.querySelector("#ssoPreview").innerHTML =
        `<strong>${{escapeHtml(data.demo ? "데모 접속 링크" : "ClassIn 앱 접속 링크")}}</strong>` +
        `기기: ${{escapeHtml(data.device_label || "-")}}\\n` +
        `표시: ${{escapeHtml(lastMaskedSsoLink || "생성됨")}}\\n` +
        `유효 시간: ${{escapeHtml(lifeLabel)}}` +
        linkMarkup;
    }}

    async function createSsoLink() {{
      const data = await callApi("/api/sso-link", {{
        uid: document.querySelector("#ssoUid").value,
        course_id: document.querySelector("#ssoCourseId").value,
        class_id: document.querySelector("#ssoClassId").value,
        telephone: document.querySelector("#ssoTelephone").value,
        device_type: document.querySelector("#ssoDeviceType").value,
        life_time: document.querySelector("#ssoLifeTime").value,
      }});
      renderSsoLinkResult(data);
      writeLog(data.message, {{
        demo: Boolean(data.demo),
        device: data.device_label,
        life_time: data.life_time,
        masked_link: data.masked_link,
      }});
    }}

    async function copySsoLink() {{
      if (!lastSsoLink) {{
        throw new Error("먼저 접속 링크를 생성하세요.");
      }}
      await navigator.clipboard.writeText(lastSsoLink);
      toast("접속 링크를 복사했습니다.", "ok");
      writeLog("접속 링크를 복사했습니다.", {{
        masked_link: lastMaskedSsoLink || "생성됨",
      }});
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
      data: "데이터",
      schedule: "스케줄",
      missing: "미제출",
      reports: "리포트",
      memo: "메모·AI",
      settings: "설정",
    }};

    const quickCommands = [
      {{
        id: "ops-hub",
        group: "운영",
        title: "오늘 운영 브리핑",
        detail: "대시보드와 처리 큐",
        tab: "dashboard",
        action: "refreshOpsHub",
        keywords: "대시보드 운영 브리핑 큐 리포트",
      }},
      {{
        id: "missing-homework",
        group: "학생",
        title: "숙제 미제출 처리",
        detail: "조회·선택·문구 미리보기",
        action: "focusMissingCore",
        keywords: "숙제 미제출 문자 보호자 카톡",
      }},
      {{
        id: "schedule-import",
        group: "수업",
        title: "스케줄표 입력",
        detail: "표 붙여넣기와 사전 검토",
        action: "focusScheduleCore",
        keywords: "스케줄 시간표 수업 숙제 생성",
      }},
      {{
        id: "class-report",
        group: "리포트",
        title: "반별 리포트 생성",
        detail: "대상 조회와 주간 초안",
        action: "focusReportCore",
        keywords: "리포트 주간 초안 학부모",
      }},
      {{
        id: "exam-import",
        group: "시험",
        title: "시험 결과 가져오기",
        detail: "CSV 학생 매칭 검토",
        action: "focusExamImport",
        keywords: "시험 성적 csv import 검토 매칭",
      }},
      {{
        id: "answer-sheet",
        group: "시험",
        title: "OMR 답안지 생성",
        detail: "OMR 생성 전 검토",
        action: "focusAnswerSheet",
        keywords: "omr answer sheet 답안지 activity",
      }},
      {{
        id: "data-inbox",
        group: "데이터",
        title: "수신 데이터 확인",
        detail: "출결·숙제·시험 기록 확인",
        tab: "data",
        action: "refreshWebhookInbox",
        keywords: "data subscription datasub 수신 기록 원본",
      }},
      {{
        id: "academy-context",
        group: "데이터",
        title: "학원 데이터 융합",
        detail: "출결·성적·메모 매칭",
        tab: "data",
        action: "refreshAcademyContexts",
        keywords: "오프라인 성적 출결 메모 매칭",
      }},
      {{
        id: "agent-question",
        group: "AI",
        title: "AI 질문",
        detail: "원장 수동 오더",
        action: "focusAgentQuestion",
        keywords: "agent ai 질문 원장",
      }},
      {{
        id: "student-memo",
        group: "메모",
        title: "학생 메모",
        detail: "상담 기록 저장 준비",
        action: "focusMemoTarget",
        keywords: "상담 학생 메모 notion",
      }},
      {{
        id: "readiness",
        group: "설정",
        title: "운영 전환 점검",
        detail: "데모·ClassIn 실연동·카톡 실발송",
        tab: "settings",
        action: "refreshReadiness",
        keywords: "check-ready diagnose api live 설정",
      }},
    ];

    function quickRunOverlay() {{
      return document.querySelector("#quickRunOverlay");
    }}

    function quickRunMatches(command, query) {{
      const needle = (query || "").trim().toLowerCase();
      if (!needle) return true;
      return [
        command.group,
        command.title,
        command.detail,
        command.keywords,
      ].join(" ").toLowerCase().includes(needle);
    }}

    function renderQuickRunList(query) {{
      const target = document.querySelector("#quickRunList");
      if (!target) return;
      const items = quickCommands.filter((command) => quickRunMatches(command, query));
      target.innerHTML = items.length
        ? items.map((command) => `
          <button type="button" class="quick-command-row" data-quick-command="${{escapeHtml(command.id)}}">
            <span>
              <strong>${{escapeHtml(command.title)}}</strong>
              <span>${{escapeHtml(command.detail)}}</span>
            </span>
            <span class="badge">${{escapeHtml(command.group)}}</span>
          </button>
        `).join("")
        : `<div class="empty compact">검색 결과가 없습니다.</div>`;
    }}

    function openQuickRun() {{
      const overlay = quickRunOverlay();
      if (!overlay) return;
      overlay.hidden = false;
      document.querySelector("#quickRunSearch").value = "";
      renderQuickRunList("");
      requestAnimationFrame(() => document.querySelector("#quickRunSearch")?.focus());
    }}

    function closeQuickRun() {{
      const overlay = quickRunOverlay();
      if (!overlay) return;
      overlay.hidden = true;
    }}

    async function executeQuickCommand(commandId) {{
      const command = quickCommands.find((item) => item.id === commandId);
      if (!command) return;
      closeQuickRun();
      if (command.tab) {{
        activateTab(command.tab);
      }}
      if (command.action) {{
        const action = actions[command.action];
        if (action) {{
          await action();
        }}
      }}
      writeLog(`빠른 실행: ${{command.title}}`);
    }}

    const actions = {{
      async refreshOpsHub() {{
        const data = await loadOpsHub();
        writeLog("운영 허브를 갱신했습니다.", data.summary);
      }},
      async toggleTheme() {{
        toggleColorTheme();
      }},
      async openQuickRun() {{
        openQuickRun();
      }},
      async closeQuickRun() {{
        closeQuickRun();
      }},
      async generateOpsReport() {{
        await generateOpsReport();
      }},
      async saveOpsHandoff() {{
        await saveOpsHandoff();
      }},
      async refreshOpsHandoffs() {{
        await refreshOpsHandoffs(true);
      }},
      async setReadinessMode(button) {{
        setReadinessMode(button.dataset.readinessMode || "local-demo");
        await loadReadiness(true);
      }},
      async refreshReadiness() {{
        await loadReadiness(true);
      }},
      async loadNotionSchema() {{
        await loadNotionSchema(true);
      }},
      async copyNotionSchema() {{
        await copyNotionSchema();
      }},
      async loadPilotBrief() {{
        await loadPilotBrief(true);
      }},
      async copyPilotBrief() {{
        await copyPilotBrief();
      }},
      async copyOpsReport() {{
        await copyOpsReport();
      }},
      async generateOpsPlaybook() {{
        await generateOpsPlaybook();
      }},
      async copyOpsPlaybook() {{
        await copyOpsPlaybook();
      }},
      async focusMissingCore() {{
        focusMissingCore();
      }},
      async focusScheduleCore() {{
        focusScheduleCore();
      }},
      async focusReportCore() {{
        focusReportCore();
      }},
      async focusExamImport() {{
        focusExamImport();
      }},
      async focusAnswerSheet() {{
        focusAnswerSheet();
      }},
      async openStudentBrief(button) {{
        openStudentBrief(button);
      }},
      async closeStudentBrief() {{
        closeStudentBrief();
      }},
      async studentBriefAskAgent() {{
        studentBriefAskAgent();
      }},
      async studentBriefMemo() {{
        studentBriefMemo();
      }},
      async studentBriefReport() {{
        await studentBriefReport();
      }},
      async studentBriefData() {{
        studentBriefData();
      }},
      async sendMissingHomeworkSms() {{
        await sendMissingHomeworkSms();
      }},
      async previewMissingHomeworkSms() {{
        await previewMissingHomeworkSms(true);
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
      async refreshWeeklyDrafts() {{
        await loadWeeklyDrafts();
      }},
      async refreshReportCompositions() {{
        await loadReportCompositions();
      }},
      async generateStudentReportPack(button) {{
        await generateStudentReportPack(button);
      }},
      async copyStudentReportPack(button) {{
        await copyStudentReportPack(button);
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
      async createAnswerSheet() {{
        await createAnswerSheet();
      }},
      async createSsoLink() {{
        await createSsoLink();
      }},
      async copySsoLink() {{
        await copySsoLink();
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
        await loadWeeklyDrafts(false);
      }},
      async approveWeekly() {{
        await approveWeekly();
      }},
      async refreshMissing() {{
        const data = await loadDiagnostics(false);
        if (!canLoadNotion(data)) {{
          renderBlockedSituation();
          writeLog("Notion 설정을 먼저 채워야 조회할 수 있습니다.");
          return;
        }}
        await loadSituation();
        await loadOpsHub();
        writeLog("미제출/알림 상황을 갱신했습니다.");
      }},
      async refreshSchedule() {{
        await loadSchedule();
      }},
      async refreshWebhookInbox() {{
        await loadWebhookInbox(true);
      }},
      async refreshAcademyContexts() {{
        await loadAcademyContexts(true);
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
      async focusAgentQuestion() {{
        focusAgentQuestionField();
      }},
      async focusMemoTarget() {{
        focusMemoTargetField();
      }},
      async focusMemoText() {{
        focusMemoTextField();
      }},
      async writeMemo() {{
        const classinId = document.querySelector("#memoClassinId").value.trim();
        const tag = document.querySelector("#memoTag").value.trim();
        const text = document.querySelector("#memoText").value.trim();
        if (!classinId) {{
          focusMemoTargetField();
          throw new Error("ClassIn ID를 입력하세요.");
        }}
        if (!text) {{
          focusMemoTextField();
          throw new Error("메모 내용을 입력하세요.");
        }}
        const data = await callApi("/api/write-memo", {{
          classin_id: classinId,
          tag,
          text,
        }});
        writeLog(data.message, data);
        setPreviewText("#memoPreview", `${{data.message}}\\nClassIn ID: ${{classinId}}\\n태그: ${{tag || "기본"}}`);
        renderAgentCommand();
        toast(data.message, "ok");
      }},
      async askAgent() {{
        const question = document.querySelector("#agentQuestion").value.trim();
        if (!question) {{
          focusAgentQuestionField();
          throw new Error("질문을 입력하세요.");
        }}
        const data = await callApi("/api/agent", {{
          question,
        }});
        writeLog(data.answer || data.message, {{ message: data.message }});
        setPreviewText("#agentAnswerPreview", data.answer || data.message || "응답이 없습니다.");
        renderAgentCommand();
        toast("AI 응답을 받았습니다.", "ok");
      }},
      async refreshStatus() {{
        await refreshStatus();
        await loadReadiness(false);
        const data = await loadDiagnostics(false, true);
        if (canLoadNotion(data)) {{
          await loadSituation();
          await loadOpsHub();
          await loadSchedule();
          await loadWebhookInbox(false);
          await loadAcademyContexts(false);
        }} else {{
          renderBlockedSituation();
          renderBlockedSchedule();
          await loadOpsHub();
          await loadWebhookInbox(false);
          renderBlockedAcademyContexts("Notion 설정을 먼저 채워야 학생별 학원 데이터 맥락을 조회할 수 있습니다.");
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
      const previewButton = event.target.closest("button[data-toggle-preview]");
      if (previewButton) {{
        const selector = previewButton.dataset.togglePreview;
        const target = document.querySelector(selector);
        if (target) {{
          setPreviewCollapsed(selector, target.classList.contains("is-expanded"));
        }}
        return;
      }}
      const pageButton = event.target.closest("button[data-page-kind]");
      if (pageButton) {{
        setPagedList(pageButton.dataset.pageKind, pageButton.dataset.pageValue);
        return;
      }}
      const button = event.target.closest("button[data-action], button[data-primary-action]");
      if (!button) return;
      const actionKey = button.dataset.action || button.dataset.primaryAction;
      const action = actions[actionKey];
      if (!action) return;
      button.disabled = true;
      button.classList.add("is-busy");
      try {{
        await action(button);
      }} catch (error) {{
        writeLog(error.message);
        toast(error.message, "err");
      }} finally {{
        button.disabled = false;
        button.classList.remove("is-busy");
      }}
    }});

    document.addEventListener("click", async (event) => {{
      if (event.target.id === "quickRunOverlay") {{
        closeQuickRun();
        return;
      }}
      const commandButton = event.target.closest("[data-quick-command]");
      if (!commandButton) return;
      commandButton.disabled = true;
      commandButton.classList.add("is-busy");
      try {{
        await executeQuickCommand(commandButton.dataset.quickCommand);
      }} catch (error) {{
        writeLog(error.message);
        toast(error.message, "err");
      }} finally {{
        commandButton.disabled = false;
        commandButton.classList.remove("is-busy");
      }}
    }});

    document.addEventListener("click", (event) => {{
      if (event.target.id === "studentBriefOverlay") {{
        closeStudentBrief();
        return;
      }}
      const hubTarget = event.target.closest("[data-hub-tab]");
      if (hubTarget && event.target.closest("button, a, input, select, textarea, label, summary")) {{
        return;
      }}
      if (hubTarget) {{
        activateTab(hubTarget.dataset.hubTab || "dashboard");
        return;
      }}
    }});

    document.addEventListener("click", (event) => {{
      const promptButton = event.target.closest("button[data-agent-prompt]");
      if (!promptButton) return;
      const target = document.querySelector("#agentQuestion");
      target.value = promptButton.dataset.agentPrompt || "";
      target.focus();
      setPreviewText("#agentAnswerPreview", "질문이 준비되었습니다. AI에게 묻기를 실행하세요.");
      renderAgentCommand();
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
      lastMissingPreview = null;
      if (currentMissing.length) {{
        document.querySelector("#actionPreview").innerHTML =
          `<strong>조회 범위 변경</strong>${{escapeHtml(document.querySelector("#windowRangeReadonly").value)}} 기준으로 다시 조회하세요.`;
      }}
    }});

    document.addEventListener("click", (event) => {{
      const filterButton = event.target.closest("button[data-queue-filter]");
      if (!filterButton) return;
      opsQueueFilter = filterButton.dataset.queueFilter || "all";
      opsQueuePage = 1;
      renderOpsQueue((opsHub || {{}}).work_queue || []);
    }});

    document.addEventListener("click", (event) => {{
      const filterButton = event.target.closest("button[data-missing-filter]");
      if (!filterButton) return;
      missingFilter = filterButton.dataset.missingFilter || "all";
      missingPage = 1;
      lastMissingPreview = null;
      renderMissingFilters();
      renderMissingSelectionList();
      renderMissingTable();
      updateMissingSelectionSummary();
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
        if (currentActionFlow === "report") {{
          renderActionCommand("report");
        }}
        return;
      }}
      if (event.target.id === "weekDate" && currentActionFlow === "report") {{
        renderActionCommand("report");
        return;
      }}
      if (event.target.id === "scheduleStart" || event.target.id === "scheduleDays") {{
        renderScheduleCommand();
        return;
      }}
      if (["examDryRun", "answerSheetDryRun", "answerSheetRelease"].includes(event.target.id)) {{
        if (event.target.id === "examDryRun") {{
          currentExamFlow = "exam";
          lastExamImportResult = null;
        }} else {{
          currentExamFlow = "answer";
          lastAnswerSheetResult = null;
        }}
        renderExamCommand();
        return;
      }}
      if (event.target.id === "contextClassName") {{
        loadAcademyContexts(true).catch((error) => {{
          writeLog(error.message);
          toast(error.message, "err");
        }});
        return;
      }}
      const missing = event.target.closest(".select-missing");
      if (missing) {{
        if (missing.checked) {{
          selectedMissingKeys.add(missing.dataset.key);
        }} else {{
          selectedMissingKeys.delete(missing.dataset.key);
        }}
        lastMissingPreview = null;
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

    document.addEventListener("input", (event) => {{
      if (event.target.id === "importText" && currentActionFlow === "schedule") {{
        renderActionCommand("schedule");
      }}
      if (event.target.id === "quickRunSearch") {{
        renderQuickRunList(event.target.value);
      }}
      if (["agentQuestion", "memoClassinId", "memoTag", "memoText"].includes(event.target.id)) {{
        renderAgentCommand();
      }}
      if (["examName", "examDate", "examClassName", "examCsvText"].includes(event.target.id)) {{
        currentExamFlow = "exam";
        lastExamImportResult = null;
        renderExamCommand();
      }}
      if ([
        "answerSheetCourseId",
        "answerSheetUnitId",
        "answerSheetName",
        "answerSheetTeacherUid",
        "answerSheetStart",
        "answerSheetEnd",
      ].includes(event.target.id)) {{
        currentExamFlow = "answer";
        lastAnswerSheetResult = null;
        renderExamCommand();
      }}
    }});

    document.addEventListener("keydown", (event) => {{
      const editable = ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName);
      if (event.key === "Escape" && !quickRunOverlay()?.hidden) {{
        closeQuickRun();
        return;
      }}
      if (event.key === "Escape" && !studentBriefOverlay()?.hidden) {{
        closeStudentBrief();
        return;
      }}
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {{
        event.preventDefault();
        openQuickRun();
        return;
      }}
      if (!editable && event.key === "/") {{
        event.preventDefault();
        openQuickRun();
      }}
    }});

    try {{
      setTheme(localStorage.getItem("classin-ui-theme") || "light");
    }} catch (error) {{
      setTheme("light");
    }}
    setMissingWindowPreset("today");
    renderStatus(status);
    renderActionCommand("missing");
    renderDataCommand();
    renderMissingCommand();
    renderScheduleCommand();
    renderReportCommand();
    renderExamCommand();
    renderAgentCommand();
    renderReadinessCommand();
    loadOpsHub().catch((error) => writeLog(error.message));
    loadReadiness(false).catch((error) => writeLog(error.message));
    loadNotionSchema(false).catch((error) => writeLog(error.message));
    loadPilotBrief(false).catch((error) => writeLog(error.message));
    refreshOpsHandoffs(false).catch((error) => writeLog(error.message));
    loadWebhookInbox(false).catch((error) => writeLog(error.message));
    if (status.mode === "demo") {{
      loadAcademyContexts(false).catch((error) => writeLog(error.message));
    }}
    loadDiagnostics(false)
      .then((data) => {{
        if (canLoadNotion(data)) {{
          return Promise.all([loadSituation(), loadSchedule(), loadAcademyContexts(false)]);
        }}
        renderBlockedSituation();
        renderBlockedSchedule();
        renderBlockedAcademyContexts("Notion 설정을 먼저 채워야 학생별 학원 데이터 맥락을 조회할 수 있습니다.");
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
