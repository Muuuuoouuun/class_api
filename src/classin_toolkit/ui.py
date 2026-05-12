"""로컬 운영 UI (FastAPI).

원장/컨설턴트가 반복 실행하는 CLI 작업을 브라우저 버튼으로 감싼 얇은 계층이다.
비즈니스 로직은 pipelines/와 intelligence/의 기존 함수를 그대로 호출한다.
"""
from __future__ import annotations

import html
import json
from datetime import date as date_cls
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .config import AppConfig, DEFAULT_CONFIG_PATH, load_config
from .notify.dispatcher import load_notification_history, notification_history_path
from .pipelines.daily import render_daily
from .pipelines.data_merge import build_report_contexts
from .pipelines.demo_seed import (
    build_demo_missing_homework_rows,
    build_demo_notification_history,
)
from .pipelines.course_dashboard import (
    build_course_dashboard,
    build_course_options,
    build_student_options,
    demo_course_dashboard,
    demo_course_options,
    demo_student_options,
)
from .pipelines.exams import import_exam_results, query_missing_exam, sweep_missing_exam
from .pipelines.missing_homework import query_missing_homework, sweep_missing_homework
from .pipelines.weekly import approve_all, generate_drafts
from .storage.notion_repo import NotionRepo

_DEMO_ACADEMY = "ClassIn Demo Academy"


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

    @app.get("/api/missing-homework")
    async def api_missing_homework(
        window_hours: int = 24,
        lesson_id: str | None = None,
    ) -> dict:
        if state.demo:
            return _demo_missing_homework_payload()
        cfg = _require_config(state)
        rows = query_missing_homework(
            cfg,
            window_hours=window_hours,
            lesson_id=(lesson_id or "").strip() or None,
        )
        history = load_notification_history(cfg, limit=300)
        report_contexts = build_report_contexts(cfg, rows)
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
        rows = load_notification_history(cfg, limit=limit)
        return {"ok": True, "items": rows, "summary": _notification_summary(rows)}

    @app.get("/api/course-dashboard/courses")
    async def api_course_dashboard_courses(q: str = "", limit: int = 30) -> dict:
        if state.demo:
            return demo_course_options(query=q, limit=limit)
        cfg = _require_config(state)
        return build_course_options(cfg, query=q, limit=limit)

    @app.get("/api/course-dashboard/students")
    async def api_course_dashboard_students(q: str = "", limit: int = 30) -> dict:
        if state.demo:
            return demo_student_options(query=q, limit=limit)
        cfg = _require_config(state)
        return build_student_options(cfg, query=q, limit=limit)

    @app.get("/api/course-dashboard")
    async def api_course_dashboard(
        course_id: str | None = None,
        student_id: str | None = None,
        days: int = 90,
    ) -> dict:
        if state.demo:
            return demo_course_dashboard(course_id=course_id, student_id=student_id, days=days)
        cfg = _require_config(state)
        return build_course_dashboard(
            cfg,
            course_id=(course_id or "").strip() or None,
            student_id=(student_id or "").strip() or None,
            days=days,
        )

    @app.post("/api/render-daily")
    async def api_render_daily(request: Request) -> JSONResponse:
        if state.demo:
            return _demo_ok("Demo mode: 일일 현황 생성은 외부 쓰기 없이 처리되었습니다.")
        cfg = _require_config(state)
        payload = await _json_payload(request)
        raw_date = (payload.get("date") or "").strip()
        target = date_cls.fromisoformat(raw_date) if raw_date else None
        result = render_daily(cfg, target=target)
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
        count = generate_drafts(cfg)
        return _ok(f"주간 드래프트 {count}건을 생성했습니다.", count=count)

    @app.post("/api/approve-weekly")
    async def api_approve_weekly(request: Request) -> JSONResponse:
        if state.demo:
            return _demo_ok("Demo mode: 주간 리포트 승인 흐름을 외부 쓰기 없이 확인했습니다.")
        cfg = _require_config(state)
        payload = await _json_payload(request)
        week = (payload.get("week") or "").strip()
        if not week:
            raise HTTPException(status_code=400, detail="week 값이 필요합니다.")
        period_start = datetime.fromisoformat(week).replace(tzinfo=timezone.utc)
        count = approve_all(cfg, period_start=period_start)
        return _ok(f"주간 리포트 {count}건을 승인 처리했습니다.", count=count)

    @app.post("/api/sweep-missing-homework")
    async def api_sweep_missing_homework(request: Request) -> JSONResponse:
        if state.demo:
            return _demo_ok("Demo mode: 미제출 sweep이 dry-run으로 처리되었습니다.", count=3)
        cfg = _require_config(state)
        payload = await _json_payload(request)
        window_hours = int(payload.get("window_hours") or 24)
        lesson_id = (payload.get("lesson_id") or "").strip() or None
        count = sweep_missing_homework(cfg, window_hours=window_hours, lesson_id=lesson_id)
        return _ok(f"미제출 알림 {count}건을 처리했습니다.", count=count)

    @app.post("/api/import-exam-results")
    async def api_import_exam_results(request: Request) -> JSONResponse:
        if state.demo:
            return _demo_ok("Demo mode: exam import checked without external writes.", count=2)
        cfg = _require_config(state)
        payload = await _json_payload(request)
        path_raw = (payload.get("path") or "").strip()
        if not path_raw:
            raise HTTPException(status_code=400, detail="path is required")
        dry_run = bool(payload.get("dry_run", True))
        result = import_exam_results(
            cfg,
            path=Path(path_raw).expanduser(),
            exam_name=(payload.get("exam_name") or "").strip() or None,
            exam_date=(payload.get("exam_date") or "").strip() or None,
            class_name=(payload.get("class_name") or "").strip() or None,
            source=(payload.get("source") or "").strip() or None,
            dry_run=dry_run,
        )
        verb = "checked" if result.dry_run else "imported"
        return _ok(
            f"Exam results {verb}.",
            total=result.total_rows,
            merged=result.merged_rows,
            unresolved=result.unresolved_rows,
            skipped=result.skipped_rows,
            errors=result.errors[:20],
            dry_run=result.dry_run,
        )

    @app.get("/api/missing-exam")
    async def api_missing_exam(
        exam_name: str,
        exam_date: str,
        class_name: str | None = None,
    ) -> dict:
        if state.demo:
            payload = _demo_missing_exam_payload(
                exam_name=exam_name,
                exam_date=exam_date,
                class_name=(class_name or "").strip() or None,
            )
            payload["demo"] = True
            return payload
        cfg = _require_config(state)
        exam_name = exam_name.strip()
        exam_date = exam_date.strip()
        class_name = (class_name or "").strip() or None
        if not exam_name or not exam_date:
            raise HTTPException(status_code=400, detail="exam_name and exam_date are required")
        rows = query_missing_exam(
            cfg,
            exam_name=exam_name,
            exam_date=exam_date,
            class_name=class_name,
        )
        return _missing_exam_payload(
            rows,
            exam_name=exam_name,
            exam_date=exam_date,
            class_name=class_name,
        )

    @app.post("/api/sweep-missing-exam")
    async def api_sweep_missing_exam(request: Request) -> JSONResponse:
        if state.demo:
            return _demo_ok("Demo mode: missing-exam sweep completed without external writes.", count=1)
        cfg = _require_config(state)
        payload = await _json_payload(request)
        exam_name = (payload.get("exam_name") or "").strip()
        exam_date = (payload.get("exam_date") or "").strip()
        class_name = (payload.get("class_name") or "").strip() or None
        if not exam_name or not exam_date:
            raise HTTPException(status_code=400, detail="exam_name and exam_date are required")
        count = sweep_missing_exam(
            cfg,
            exam_name=exam_name,
            exam_date=exam_date,
            class_name=class_name,
        )
        return _ok("Missing-exam notifications processed.", count=count)

    @app.post("/api/write-memo")
    async def api_write_memo(request: Request) -> JSONResponse:
        if state.demo:
            return _demo_ok("Demo mode: 메모 저장은 외부 쓰기 없이 처리되었습니다.")
        cfg = _require_config(state)
        payload = await _json_payload(request)
        classin_id = (payload.get("classin_id") or "").strip()
        text = (payload.get("text") or "").strip()
        tag = (payload.get("tag") or "").strip() or None
        if not classin_id or not text:
            raise HTTPException(status_code=400, detail="classin_id와 text가 필요합니다.")
        if cfg.output.memo.mode == "off":
            return _ok("memo mode가 off라 기록을 건너뛰었습니다.")
        page_id = NotionRepo.from_config(cfg).write_memo(
            student_classin_id=classin_id,
            text=text,
            tag=tag,
        )
        return _ok("메모를 저장했습니다.", page_id=page_id)

    @app.post("/api/agent")
    async def api_agent(request: Request) -> JSONResponse:
        if state.demo:
            payload = await _json_payload(request)
            question = (payload.get("question") or "").strip()
            answer = (
                "데모 기준으로 김지각, 이하락, 최결석 학생이 숙제 확인 대상입니다. "
                "연락처가 없는 학생은 먼저 학생 Master를 보완해야 합니다."
            )
            return _demo_ok("Demo mode: AI 응답을 샘플로 생성했습니다.", answer=answer, question=question)
        cfg = _require_config(state)
        payload = await _json_payload(request)
        question = (payload.get("question") or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="question 값이 필요합니다.")
        from .intelligence.agent import run_agent_turn

        answer, _messages = run_agent_turn(cfg, [{"role": "user", "content": question}])
        return _ok("AI 응답을 생성했습니다.", answer=answer)

    @app.get("/reports/{kind}/{filename}")
    async def serve_report(kind: str, filename: str):
        if state.demo:
            if kind not in ("daily", "weekly"):
                raise HTTPException(status_code=404)
            return HTMLResponse(
                f"<html><body><h1>Demo {html.escape(kind)} report</h1>"
                f"<p>{html.escape(filename)}</p></body></html>"
            )
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


def _demo_ok(message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": True, "demo": True, "message": message, **extra})


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


def _count_files(path: Path, pattern: str) -> int:
    if not path.exists():
        return 0
    return sum(1 for p in path.glob(pattern) if p.is_file())


def _count_history_rows(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


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
    }
    return {
        "ok": True,
        "summary": summary,
        "items": items,
        "data_context": data_context
        or {"summary": {}, "needs_review_items": []},
    }


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
        counts[student_id] = counts.get(student_id, 0) + 1
    return counts


def _missing_action(has_parent_phone: bool, notification_status: str) -> str:
    if not has_parent_phone:
        return "needs_phone"
    if notification_status == "failed":
        return "needs_retry"
    if notification_status == "pending":
        return "needs_message"
    if notification_status == "dry_run":
        return "needs_review"
    return "done"


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


def _missing_exam_payload(
    rows: list[dict[str, Any]],
    *,
    exam_name: str,
    exam_date: str,
    class_name: str | None,
) -> dict[str, Any]:
    items = []
    for row in rows:
        parent_phone = row.get("parent_phone") or ""
        items.append(
            {
                "student_classin_id": row.get("student_classin_id") or "",
                "student_name": row.get("student_name") or "미등록",
                "class_name": row.get("student_class_name") or row.get("class_name") or "",
                "parent_phone": parent_phone,
                "has_parent_phone": bool(parent_phone),
                "exam_name": row.get("exam_name") or exam_name,
                "exam_date": row.get("exam_date") or exam_date,
                "attended": row.get("attended"),
                "subject": row.get("subject") or "",
                "score": row.get("score"),
                "max_score": row.get("max_score"),
                "percent": row.get("percent"),
                "source": row.get("source") or "",
            }
        )

    return {
        "ok": True,
        "summary": {
            "exam_name": exam_name,
            "exam_date": exam_date,
            "class_name": class_name or "",
            "total_missing": len(items),
            "with_parent_phone": sum(1 for item in items if item["has_parent_phone"]),
            "no_parent_phone": sum(1 for item in items if not item["has_parent_phone"]),
            "recorded_absent": sum(1 for item in items if item["attended"] is False),
            "not_recorded": sum(1 for item in items if item["attended"] is None),
        },
        "items": items,
    }


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


def _demo_missing_exam_payload(
    *,
    exam_name: str,
    exam_date: str,
    class_name: str | None,
) -> dict[str, Any]:
    demo_class = class_name or "고2-A"
    return _missing_exam_payload(
        [
            {
                "student_classin_id": "10002",
                "student_name": "김지각",
                "student_class_name": demo_class,
                "parent_phone": "01055556666",
                "exam_name": exam_name,
                "exam_date": exam_date,
                "attended": False,
                "subject": "수학",
                "source": "demo://exam-results",
            },
            {
                "student_classin_id": "10005",
                "student_name": "최미연",
                "student_class_name": demo_class,
                "parent_phone": "",
                "exam_name": exam_name,
                "exam_date": exam_date,
                "attended": None,
                "subject": "수학",
                "source": "demo://student-master",
            },
        ],
        exam_name=exam_name,
        exam_date=exam_date,
        class_name=class_name,
    )


def _demo_missing_rows() -> list[dict[str, Any]]:
    return build_demo_missing_homework_rows(base_date=date_cls.today(), weeks=3)


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
    title = html.escape(status.get("academy") or "Classin++")
    initial_source = (status.get("academy") or "Classin++").strip()
    title_initial = html.escape(initial_source[:1] if initial_source else "C")
    today_value = date_cls.today()
    _korean_weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    today_korean = html.escape(
        f"{today_value.year}년 {today_value.month}월 {today_value.day}일 "
        f"{_korean_weekdays[today_value.weekday()]}요일"
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ClassIn Toolkit UI</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7faf6;
      --bg-grad: var(--bg);
      --panel: #ffffff;
      --panel-soft: #fafdf9;
      --line: #e8eee6;
      --line-2: #eef2eb;
      --line-strong: #d6dfd1;
      --text: #0e1a14;
      --text-2: #33433b;
      --muted: #6b7a72;
      --muted-2: #9ba89f;
      --primary: #16a34a;
      --primary-strong: #15803d;
      --primary-soft: #dcf2e3;
      --primary-softer: #f0faf2;
      --primary-ink: #0b3d20;
      --primary-ring: rgba(22, 163, 74, .18);
      --accent: #d97706;
      --accent-soft: #fef6e7;
      --accent-ink: #92520a;
      --danger: #dc2626;
      --danger-soft: #fdecec;
      --danger-ink: #991b1b;
      --ok: #15803d;
      --ok-soft: #dcf2e3;
      --blue: #3730a3;
      --blue-soft: #e0e7ff;
      --shadow: 0 1px 0 rgba(15, 23, 17, .04), 0 8px 24px -16px rgba(15, 23, 17, .12);
      --shadow-soft: 0 1px 0 rgba(15, 23, 17, .04), 0 12px 28px -16px rgba(15, 23, 17, .18);
      --shadow-pop: 0 30px 80px -20px rgba(15, 23, 17, .35);
      --radius-sm: 8px;
      --radius: 10px;
      --radius-lg: 14px;
    }}
    * {{ box-sizing: border-box; }}
    *:focus-visible {{
      outline: 3px solid var(--primary-ring);
      outline-offset: 2px;
      border-radius: 6px;
    }}
    body {{
      margin: 0;
      min-width: 320px;
      background: var(--bg);
      color: var(--text);
      font-family: "Pretendard Variable", Pretendard, "Noto Sans KR", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      font-feature-settings: "ss01", "ss03";
      letter-spacing: -0.01em;
      -webkit-font-smoothing: antialiased;
    }}
    ::selection {{ background: #bef0c9; color: #062812; }}
    .layout {{
      display: flex;
      min-height: 100vh;
    }}
    aside.sidebar {{
      width: 248px;
      flex: 0 0 248px;
      background: #fff;
      border-right: 1px solid var(--line);
      position: sticky;
      top: 0;
      height: 100vh;
      display: flex;
      flex-direction: column;
    }}
    .sidebar-brand {{
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 22px 20px;
      border-bottom: 1px solid var(--line-2);
    }}
    .brand-mark {{
      width: 32px;
      height: 32px;
      flex: 0 0 auto;
      border-radius: 8px;
      background: var(--primary);
      display: grid;
      place-items: center;
      color: #fff;
      box-shadow: inset 0 -1px 0 rgba(0,0,0,.12);
      position: relative;
    }}
    .brand-mark svg {{ display: block; }}
    .brand-wordmark {{
      font-size: 16px;
      font-weight: 800;
      letter-spacing: -0.03em;
      line-height: 1;
      color: var(--text);
    }}
    .brand-wordmark .accent {{ color: var(--primary); }}
    .brand-tagline {{
      font-size: 10.5px;
      color: var(--muted);
      margin-top: 3px;
    }}
    nav.sidenav {{
      padding: 12px 10px;
      flex: 1;
      display: flex;
      flex-direction: column;
      gap: 2px;
    }}
    .sidenav button {{
      width: 100%;
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 11px 14px;
      border-radius: 10px;
      background: transparent;
      color: var(--text-2);
      font-size: 14px;
      font-weight: 500;
      transition: background .12s ease, color .12s ease;
      position: relative;
      border: 0;
      cursor: pointer;
      text-align: left;
      min-height: auto;
    }}
    .sidenav button .nav-icon {{
      display: inline-flex;
      width: 18px;
      height: 18px;
      color: var(--muted);
      flex: 0 0 auto;
    }}
    .sidenav button .nav-label {{ flex: 1; }}
    .sidenav button .nav-badge {{
      background: var(--accent);
      color: #fff;
      font-size: 11px;
      font-weight: 700;
      padding: 2px 7px;
      border-radius: 999px;
      min-width: 20px;
      text-align: center;
      font-variant-numeric: tabular-nums;
    }}
    .sidenav button:hover {{
      background: #f5f8f3;
      transform: none;
      box-shadow: none;
    }}
    .sidenav button.active {{
      background: var(--primary-softer);
      color: var(--primary-strong);
      font-weight: 700;
    }}
    .sidenav button.active .nav-icon {{ color: var(--primary); }}
    .sidenav button.active::before {{
      content: "";
      position: absolute;
      left: 0;
      top: 8px;
      bottom: 8px;
      width: 3px;
      border-radius: 3px;
      background: var(--primary);
    }}
    .sidebar-profile {{
      padding: 12px 14px;
      border-top: 1px solid var(--line-2);
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .profile-avatar {{
      width: 32px;
      height: 32px;
      border-radius: 50%;
      background: var(--primary-soft);
      color: var(--primary-ink);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      font-size: 13.4px;
      flex: 0 0 auto;
    }}
    .profile-meta {{ flex: 1; min-width: 0; }}
    .profile-name {{
      font-size: 13px;
      font-weight: 600;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      color: var(--text);
    }}
    .profile-sub {{
      font-size: 11px;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .app-main {{ flex: 1; min-width: 0; }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 20;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 32px;
      background: rgba(247, 250, 246, .85);
      backdrop-filter: blur(8px);
      -webkit-backdrop-filter: blur(8px);
      border-bottom: 1px solid var(--line-2);
    }}
    .crumb {{
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
    }}
    .crumb .crumb-home {{ color: var(--muted); font-weight: 500; }}
    .crumb .crumb-current {{ color: var(--text); font-weight: 600; }}
    .crumb svg {{ flex: 0 0 auto; }}
    .topbar-actions {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .topbar-search {{
      position: relative;
    }}
    .topbar-search input {{
      padding: 8px 14px 8px 36px;
      width: 280px;
      min-height: 36px;
      font-size: 13px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fff;
    }}
    .topbar-search svg {{
      position: absolute;
      left: 11px;
      top: 10px;
      color: var(--muted-2);
    }}
    .topbar-bell {{
      position: relative;
      width: 36px;
      height: 36px;
      min-height: 36px;
      border-radius: 10px;
      background: #fff;
      border: 1px solid var(--line);
      color: var(--text-2);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0;
    }}
    .topbar-bell:hover {{ background: var(--panel-soft); border-color: var(--line-strong); color: var(--text); }}
    .topbar-bell .dot {{
      position: absolute;
      top: 6px;
      right: 7px;
      width: 7px;
      height: 7px;
      background: var(--accent);
      border-radius: 50%;
      border: 2px solid #fff;
    }}
    h1 {{
      margin: 0;
      font-size: 19px;
      line-height: 1.2;
      font-weight: 700;
      letter-spacing: -0.02em;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    main.app-body {{
      padding: 28px 32px 80px;
      max-width: 1400px;
      margin: 0 auto;
    }}
    .page-head {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 22px;
    }}
    .page-eyebrow {{
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 6px;
    }}
    .page-title {{
      font-size: 26px;
      font-weight: 700;
      letter-spacing: -0.025em;
      color: var(--text);
      margin: 0;
    }}
    .page-title .wave {{ display: inline-block; margin-left: 6px; }}
    .page-sub {{
      font-size: 14px;
      color: var(--muted-2);
      margin-top: 6px;
    }}
    .page-actions {{
      display: flex;
      gap: 8px;
      flex-shrink: 0;
    }}
    .quick-actions {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }}
    .quick-card {{
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 16px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow);
      cursor: pointer;
      text-align: left;
      transition: transform .15s ease, box-shadow .15s ease;
      min-height: auto;
      color: var(--text);
      font: inherit;
    }}
    .quick-card:hover {{
      transform: translateY(-1px);
      box-shadow: var(--shadow-soft);
      background: var(--panel);
    }}
    .quick-icon {{
      width: 40px;
      height: 40px;
      border-radius: 10px;
      background: var(--primary-soft);
      color: var(--primary-strong);
      display: flex;
      align-items: center;
      justify-content: center;
      flex: 0 0 auto;
    }}
    .quick-icon.warn {{ background: var(--accent-soft); color: var(--accent-ink); }}
    .quick-icon.blue {{ background: var(--blue-soft); color: var(--blue); }}
    .quick-meta {{ flex: 1; min-width: 0; }}
    .quick-title {{
      font-size: 14px;
      font-weight: 700;
      color: var(--text);
      line-height: 1.3;
    }}
    .quick-desc {{
      font-size: 11.5px;
      color: var(--muted);
      margin-top: 2px;
    }}
    .quick-card .chev {{ color: var(--muted-2); flex: 0 0 auto; }}
    @media (max-width: 1100px) {{
      .quick-actions {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 600px) {{
      .quick-actions {{ grid-template-columns: 1fr; }}
      .page-head {{ flex-direction: column; align-items: flex-start; }}
    }}
    .status-strip {{
      display: grid;
      grid-template-columns: minmax(180px, .9fr) minmax(320px, 1.6fr) minmax(420px, 2.1fr);
      gap: 12px;
      margin-bottom: 18px;
    }}
    .status-group {{
      display: grid;
      grid-template-columns: repeat(var(--cols, 1), minmax(0, 1fr));
      gap: 10px;
      padding: 14px;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: var(--radius-lg);
      position: relative;
      box-shadow: var(--shadow);
    }}
    .status-group::before {{
      content: attr(data-label);
      position: absolute;
      top: -8px;
      left: 14px;
      padding: 0 6px;
      background: var(--bg);
      color: var(--muted);
      font-size: 10.5px;
      font-weight: 700;
      letter-spacing: .1em;
      text-transform: uppercase;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow);
      transition: box-shadow .15s ease;
    }}
    .panel:hover {{ box-shadow: var(--shadow-soft); }}
    .metric {{
      position: relative;
      overflow: hidden;
      padding: 12px 14px;
      min-height: 76px;
      background: transparent;
      border: 0;
      border-radius: 0;
      transition: none;
    }}
    .metric + .metric {{ border-left: 1px solid var(--line-2); }}
    .status-group > .metric + .metric {{ border-left: 0; }}
    .metric span {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      font-size: 12.5px;
      line-height: 1.35;
      font-weight: 600;
    }}
    .metric strong {{
      display: block;
      margin-top: 8px;
      font-size: 28px;
      line-height: 1.1;
      letter-spacing: -0.025em;
      font-variant-numeric: tabular-nums;
      font-weight: 700;
      color: var(--text);
    }}
    .metric.warn strong {{ color: var(--accent); }}
    .metric.alert strong {{ color: var(--danger); }}
    .metric.ok strong {{ color: var(--primary-strong); }}
    .grid {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(320px, 380px);
      gap: 18px;
      align-items: start;
    }}
    .tabbar {{
      display: inline-flex;
      gap: 2px;
      padding: 4px;
      margin: 0 0 18px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(255, 255, 255, .72);
      box-shadow: var(--shadow);
      backdrop-filter: blur(8px);
    }}
    .tabbar button {{
      min-height: 32px;
      border: 1px solid transparent;
      background: transparent;
      color: var(--muted);
      padding: 6px 16px;
      box-shadow: none;
      border-radius: 999px;
      font-weight: 600;
      font-size: 13.5px;
      transition: color .15s ease, background .15s ease, box-shadow .15s ease;
    }}
    .tabbar button:hover {{
      background: rgba(255, 255, 255, .8);
      color: var(--text);
      transform: none;
      box-shadow: none;
    }}
    .tabbar button.active {{
      background: var(--primary);
      color: #fff;
      box-shadow: 0 1px 2px rgba(15, 23, 42, .1);
    }}
    .tab-view {{ display: none; }}
    .tab-view.active {{ display: block; animation: fadeIn .22s ease; }}
    @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(4px); }} to {{ opacity: 1; transform: none; }} }}
    aside {{
      position: sticky;
      top: 92px;
      align-self: start;
    }}
    .panel {{
      padding: 18px 18px 16px;
    }}
    .panel + .panel {{
      margin-top: 14px;
    }}
    .hero-panel {{
      padding: 20px 20px 18px;
    }}
    .panel-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 14px;
    }}
    .panel-head p {{
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }}
    h2 {{
      margin: 0;
      font-size: 17px;
      line-height: 1.25;
      letter-spacing: -0.01em;
      font-weight: 700;
      color: var(--text);
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }}
    h2 .h2-dot {{
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--primary);
    }}
    .section-subtitle {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
      white-space: nowrap;
      padding: 4px 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--panel-soft);
    }}
    .subtabs {{
      display: flex;
      flex-wrap: wrap;
      gap: 2px;
      padding: 4px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--panel-soft);
      margin-bottom: 14px;
    }}
    .subtabs button {{
      flex: 1;
      min-width: 84px;
      min-height: 32px;
      border: 1px solid transparent;
      background: transparent;
      color: var(--muted);
      padding: 6px 12px;
      box-shadow: none;
      border-radius: 7px;
      font-weight: 600;
      font-size: 13px;
    }}
    .subtabs button:hover {{
      background: #fff;
      color: var(--text);
      transform: none;
      box-shadow: none;
    }}
    .subtabs button.active {{
      background: #fff;
      color: var(--primary-strong);
      box-shadow: var(--shadow-soft);
      border-color: var(--line);
    }}
    .subtab-view {{ display: none; }}
    .subtab-view.active {{ display: block; animation: fadeIn .18s ease; }}
    .actions {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .control-grid {{
      display: grid;
      grid-template-columns: minmax(120px, .55fr) minmax(220px, 1fr) auto auto;
      gap: 10px;
      align-items: end;
      margin-bottom: 12px;
    }}
    .toolbar {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 0 0 14px;
    }}
    .toolbar button {{
      min-height: 32px;
      border-color: var(--line);
      background: rgba(255, 255, 255, .72);
      color: var(--text-2);
      padding: 4px 12px;
      font-size: 12.5px;
      font-weight: 600;
      border-radius: 999px;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }}
    .toolbar button:hover {{
      background: #fff;
      color: var(--text);
    }}
    .toolbar button.active {{
      border-color: var(--primary);
      background: var(--primary-soft);
      color: var(--primary-strong);
      box-shadow: inset 0 0 0 1px rgba(46, 111, 99, .18);
    }}
    .toolbar button .chip-count {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 18px;
      height: 18px;
      padding: 0 5px;
      font-size: 11px;
      font-weight: 700;
      line-height: 1;
      border-radius: 999px;
      background: #eef1f6;
      color: var(--muted);
      font-variant-numeric: tabular-nums;
    }}
    .toolbar button.active .chip-count {{
      background: #fff;
      color: var(--primary-strong);
    }}
    label {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.3;
      font-weight: 600;
    }}
    input, textarea, select {{
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      padding: 8px 10px;
      color: var(--text);
      background: #fff;
      font: inherit;
      font-size: 14px;
      outline: none;
      transition: border-color .15s ease, box-shadow .15s ease;
    }}
    input:hover, textarea:hover, select:hover {{
      border-color: var(--line-strong);
    }}
    input:focus, textarea:focus, select:focus {{
      border-color: var(--primary);
      box-shadow: 0 0 0 3px var(--primary-ring);
    }}
    textarea {{
      min-height: 92px;
      resize: vertical;
    }}
    button {{
      min-height: 38px;
      border: 1px solid var(--primary);
      border-radius: var(--radius-sm);
      padding: 8px 14px;
      background: var(--primary);
      color: #fff;
      font: inherit;
      font-size: 13.5px;
      font-weight: 600;
      cursor: pointer;
      transition: transform .14s ease, background .14s ease, box-shadow .14s ease, border-color .14s ease;
      white-space: nowrap;
    }}
    button.secondary {{
      border-color: var(--line);
      background: #fff;
      color: var(--text);
    }}
    button.ghost {{
      border-color: transparent;
      background: transparent;
      color: var(--muted);
      box-shadow: none;
    }}
    button:hover {{
      background: var(--primary-strong);
      border-color: var(--primary-strong);
      box-shadow: var(--shadow-soft);
    }}
    button.secondary:hover {{
      background: var(--panel-soft);
      border-color: var(--line-strong);
      color: var(--text);
    }}
    button.ghost:hover {{
      background: var(--panel-soft);
      color: var(--text);
    }}
    button:active {{
      transform: translateY(1px);
      box-shadow: none;
    }}
    button:disabled {{
      cursor: wait;
      opacity: .55;
      transform: none;
      box-shadow: none;
    }}
    .row {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      align-items: end;
    }}
    .stack {{
      display: grid;
      gap: 10px;
    }}
    .log {{
      min-height: 160px;
      max-height: 320px;
      overflow: auto;
      border-radius: var(--radius-sm);
      border: 1px solid #1f2a3d;
      background: #0e1422;
      color: #d6dfee;
      padding: 12px;
      font: 12px/1.55 ui-monospace, "SF Mono", Consolas, "Cascadia Mono", monospace;
      white-space: pre-wrap;
    }}
    .table-wrap {{
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: #fff;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 960px;
      table-layout: fixed;
      font-size: 13px;
    }}
    th, td {{
      padding: 11px 14px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      position: sticky;
      top: 0;
      z-index: 1;
      background: linear-gradient(180deg, #fafbfd, #f4f6fa);
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: .04em;
      text-transform: uppercase;
      white-space: nowrap;
    }}
    tr {{ transition: background .12s ease; }}
    tr:hover td {{
      background: rgba(46, 111, 99, .04);
    }}
    tr:last-child td {{ border-bottom: 0; }}
    td {{ font-variant-numeric: tabular-nums; }}
    .missing-table .col-student {{ width: 17%; }}
    .missing-table .col-lesson {{ width: 28%; }}
    .missing-table .col-phone {{ width: 15%; }}
    .missing-table .col-status {{ width: 16%; }}
    .missing-table .col-action {{ width: 24%; }}
    .notification-table .col-time {{ width: 18%; }}
    .notification-table .col-student {{ width: 18%; }}
    .notification-table .col-phone {{ width: 16%; }}
    .notification-table .col-status {{ width: 14%; }}
    .notification-table .col-message {{ width: 34%; }}
    .exam-preview {{
      display: grid;
      gap: 8px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, .66);
      font-size: 13px;
      line-height: 1.35;
    }}
    .exam-preview-head {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      color: var(--muted);
    }}
    .exam-preview-head strong {{
      color: var(--text);
      font-size: 14px;
    }}
    .exam-preview-list {{
      display: grid;
      gap: 6px;
      max-height: 176px;
      overflow: auto;
    }}
    .exam-preview-item {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
      padding: 8px 0;
      border-top: 1px solid var(--line);
    }}
    .performance-controls {{
      display: grid;
      grid-template-columns: auto minmax(180px, .8fr) minmax(220px, 1fr) minmax(120px, .42fr) auto;
      gap: 10px;
      align-items: end;
      margin-bottom: 14px;
    }}
    .segmented {{
      display: inline-grid;
      grid-template-columns: repeat(2, minmax(76px, 1fr));
      gap: 4px;
      padding: 4px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
    }}
    .segmented button {{
      min-height: 30px;
      border-color: transparent;
      background: transparent;
      color: var(--muted);
      padding: 5px 10px;
      box-shadow: none;
      white-space: nowrap;
    }}
    .segmented button:hover {{
      background: #fff;
      color: var(--text);
      transform: none;
      box-shadow: none;
    }}
    .segmented button.active {{
      border-color: rgba(47, 111, 100, .32);
      background: #fff;
      color: var(--primary-strong);
      box-shadow: var(--shadow-soft);
    }}
    .mode-field.hidden {{
      display: none;
    }}
    .dashboard-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1.45fr) minmax(280px, .75fr);
      gap: 14px;
      align-items: stretch;
    }}
    .hero-metrics {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }}
    .kpi-card {{
      min-height: 106px;
      padding: 13px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: linear-gradient(180deg, #fff, #f9fbfd);
    }}
    .kpi-card span {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
    }}
    .kpi-card strong {{
      display: block;
      margin-top: 8px;
      font-size: 28px;
      line-height: 1.05;
    }}
    .kpi-card small {{
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.3;
    }}
    .chart-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 12px;
    }}
    .chart-card {{
      min-height: 278px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 14px;
    }}
    .chart-head {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 8px;
    }}
    .chart-head h3 {{
      margin: 0;
      font-size: 15px;
      line-height: 1.2;
    }}
    .chart-head span {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 750;
    }}
    .chart-box {{
      height: 212px;
    }}
    .chart-box svg {{
      width: 100%;
      height: 100%;
      display: block;
    }}
    .chart-empty {{
      display: grid;
      place-items: center;
      height: 100%;
      color: var(--muted);
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: var(--panel-soft);
      font-size: 13px;
    }}
    .attention-panel {{
      display: grid;
      gap: 10px;
      align-content: start;
    }}
    .attention-list {{
      display: grid;
      gap: 8px;
      max-height: 302px;
      overflow: auto;
    }}
    .attention-item {{
      display: grid;
      gap: 5px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, .72);
      font-size: 13px;
    }}
    .attention-item.high {{
      border-color: rgba(180, 35, 24, .28);
      background: #fff8f7;
    }}
    .attention-item.medium {{
      border-color: #f3d2a1;
      background: #fffaf1;
    }}
    .attention-item strong {{
      font-size: 14px;
    }}
    .mini-bars {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
    }}
    .mini-bar {{
      display: grid;
      gap: 6px;
      font-size: 12px;
      color: var(--muted);
      font-weight: 750;
    }}
    .mini-track {{
      height: 8px;
      overflow: hidden;
      border-radius: 999px;
      background: #edf1f6;
    }}
    .mini-fill {{
      display: block;
      height: 100%;
      border-radius: inherit;
      background: var(--primary);
    }}
    .student-card-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 10px;
      margin-top: 14px;
    }}
    .student-card {{
      min-height: 214px;
      padding: 13px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      box-shadow: var(--shadow);
    }}
    .student-card-head {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: start;
      margin-bottom: 10px;
    }}
    .ring {{
      width: 54px;
      aspect-ratio: 1;
      display: grid;
      place-items: center;
      border-radius: 50%;
      background: conic-gradient(var(--primary) calc(var(--pct) * 1%), #e8edf5 0);
    }}
    .ring span {{
      width: 40px;
      aspect-ratio: 1;
      display: grid;
      place-items: center;
      border-radius: 50%;
      background: #fff;
      color: var(--text);
      font-size: 12px;
      font-weight: 850;
    }}
    .sparkline {{
      height: 56px;
      margin-top: 8px;
    }}
    .sparkline svg {{
      width: 100%;
      height: 100%;
      display: block;
    }}
    .cell-title {{
      color: var(--text);
      font-weight: 800;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }}
    .cell-sub {{
      margin-top: 3px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.28;
      overflow-wrap: anywhere;
    }}
    .cell-stack {{
      display: grid;
      gap: 5px;
      justify-items: start;
    }}
    .meta-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      align-items: center;
      margin-top: 5px;
    }}
    .action-copy {{
      display: block;
      margin-top: 3px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.32;
    }}
    .context-line {{
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
      margin-top: 5px;
    }}
    .context-badge {{
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      padding: 1px 6px;
      border: 1px solid #c9d8d1;
      border-radius: 999px;
      background: #eef6f3;
      color: var(--primary-strong);
      font-size: 11px;
      font-weight: 800;
      white-space: nowrap;
    }}
    .context-summary {{
      display: block;
      margin-top: 5px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.32;
    }}
    .empty {{
      display: grid;
      gap: 6px;
      justify-items: center;
      padding: 28px 18px;
      color: var(--muted);
      text-align: center;
      border: 1px dashed var(--line-strong);
      border-radius: var(--radius);
      background: linear-gradient(180deg, rgba(255,255,255,.7), var(--panel-soft));
      font-size: 13.5px;
    }}
    .empty::before {{
      content: "";
      width: 28px;
      height: 28px;
      border-radius: 50%;
      background:
        radial-gradient(circle at 50% 50%, var(--panel-soft) 0 36%, transparent 38%),
        conic-gradient(from 0deg, var(--line-strong), var(--line) 50%, var(--line-strong));
      opacity: .8;
    }}
    .status-pill {{
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      padding: 2px 7px;
      border-radius: 999px;
      border: 1px solid var(--line);
      white-space: nowrap;
      font-size: 12px;
      line-height: 1.2;
      font-weight: 800;
    }}
    .status-pill.pending {{ color: var(--accent); background: #fff8eb; border-color: #f3d2a1; }}
    .status-pill.dry_run {{ color: var(--blue); background: #edf7ff; border-color: #b7dcf3; }}
    .status-pill.sent {{ color: var(--ok); background: #effaf4; border-color: rgba(22, 120, 75, .25); }}
    .status-pill.failed {{ color: var(--danger); background: #fff1f0; border-color: rgba(180, 35, 24, .25); }}
    .action-list {{
      display: grid;
      gap: 8px;
    }}
    .action-item {{
      position: relative;
      display: grid;
      gap: 4px;
      padding: 12px 12px 12px 16px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: #fff;
      font-size: 13px;
      line-height: 1.45;
      transition: transform .14s ease, box-shadow .14s ease;
    }}
    .action-item::before {{
      content: "";
      position: absolute;
      top: 12px;
      bottom: 12px;
      left: 6px;
      width: 3px;
      border-radius: 999px;
      background: var(--line-strong);
    }}
    .action-item:hover {{
      transform: translateY(-1px);
      box-shadow: var(--shadow-soft);
    }}
    .action-item strong {{ font-size: 13.5px; font-weight: 700; }}
    .action-item span {{ color: var(--muted); }}
    .action-item.needs_phone {{ border-color: rgba(180, 35, 26, .25); background: var(--danger-soft); }}
    .action-item.needs_phone::before {{ background: var(--danger); }}
    .action-item.needs_message {{ border-color: #f3d2a1; background: var(--accent-soft); }}
    .action-item.needs_message::before {{ background: var(--accent); }}
    .action-item.needs_review {{ border-color: #b7dcf3; background: var(--blue-soft); }}
    .action-item.needs_review::before {{ background: var(--blue); }}
    .action-item.needs_retry {{ border-color: rgba(180, 35, 26, .25); background: var(--danger-soft); }}
    .action-item.needs_retry::before {{ background: var(--danger); }}
    .repeat-mark {{
      color: var(--accent);
      font-weight: 700;
      white-space: nowrap;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      padding: 2px 7px;
      border-radius: 999px;
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
      line-height: 1.2;
      font-weight: 750;
      background: rgba(255, 255, 255, .72);
      white-space: nowrap;
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
      grid-template-columns: 92px 1fr;
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
    @media (max-width: 1100px) {{
      .layout {{ flex-direction: column; }}
      aside.sidebar {{
        position: static;
        width: 100%;
        height: auto;
        flex: 0 0 auto;
        border-right: 0;
        border-bottom: 1px solid var(--line);
        flex-direction: row;
        align-items: center;
        gap: 12px;
        padding: 10px 16px;
      }}
      .sidebar-brand {{ padding: 0; border: 0; }}
      .brand-tagline {{ display: none; }}
      nav.sidenav {{
        flex-direction: row;
        padding: 0;
        gap: 6px;
        flex: 1;
        justify-content: flex-end;
      }}
      .sidenav button {{ width: auto; padding: 8px 12px; }}
      .sidenav button.active::before {{ display: none; }}
      .sidebar-profile {{
        padding: 0;
        border: 0;
      }}
      .profile-meta {{ display: none; }}
      .topbar {{ padding: 12px 20px; }}
      .topbar-search input {{ width: 200px; }}
      main.app-body {{ padding: 22px 20px 60px; }}
    }}
    @media (max-width: 1280px) {{
      .status-strip {{
        grid-template-columns: 1fr;
      }}
      .grid {{
        grid-template-columns: 1fr;
      }}
      .performance-controls {{
        grid-template-columns: 1fr 1fr;
      }}
      .dashboard-grid, .chart-grid {{
        grid-template-columns: 1fr;
      }}
      .hero-metrics {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      aside {{
        position: static;
      }}
    }}
    @media (max-width: 760px) {{
      header {{
        align-items: flex-start;
        flex-direction: column;
        padding: 14px 18px;
      }}
      .status-strip, .grid, .actions, .control-grid, .performance-controls, .hero-metrics {{
        grid-template-columns: 1fr;
      }}
      .tabbar {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        width: 100%;
        border-radius: var(--radius);
      }}
      .tabbar button {{ border-radius: 7px; }}
      main.app-body {{
        padding: 16px;
      }}
      .panel, .hero-panel {{
        padding: 14px;
        border-radius: var(--radius);
      }}
      .panel-head {{
        display: grid;
        gap: 6px;
      }}
      table {{
        min-width: 820px;
      }}
      th, td {{
        padding: 10px;
      }}
      .exam-preview-head, .exam-preview-item {{
        align-items: start;
        grid-template-columns: 1fr;
      }}
      .exam-preview-head {{
        display: grid;
      }}
      dl {{
        grid-template-columns: 1fr;
        gap: 3px;
      }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      *, *::before, *::after {{
        animation-duration: 0.01ms !important;
        transition-duration: 0.01ms !important;
      }}
    }}
  </style>
</head>
<body>
  <div class="layout">
    <aside class="sidebar" aria-label="기본 메뉴">
      <div class="sidebar-brand">
        <span class="brand-mark" aria-hidden="true">
          <svg width="20" height="20" viewBox="0 0 32 32" fill="none">
            <path d="M9 11.5C9 9.6 10.6 8 12.5 8h7A3.5 3.5 0 0123 11.5v6c0 1.4-1.1 2.5-2.5 2.5h-2L15 24v-4h-2.5A3.5 3.5 0 019 17.5v-6z" fill="#fff"/>
            <path d="M13.5 14h5M13.5 17h3" stroke="#16a34a" stroke-width="1.6" stroke-linecap="round"/>
          </svg>
        </span>
        <div>
          <div class="brand-wordmark">Classin<span class="accent">++</span></div>
          <div class="brand-tagline">API로 더 쾌적한 운영</div>
        </div>
      </div>
      <nav class="sidenav" aria-label="대시보드 탭">
        <button data-tab="operations" class="active" aria-current="page">
          <span class="nav-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/></svg></span>
          <span class="nav-label">홈</span>
          <span class="nav-badge" id="sideBadgeMissing" hidden>0</span>
        </button>
        <button data-tab="performance">
          <span class="nav-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20V8M10 20V4M16 20v-8M22 20H2"/></svg></span>
          <span class="nav-label">성과 대시보드</span>
        </button>
      </nav>
      <div class="sidebar-profile">
        <span class="profile-avatar" aria-hidden="true">{title_initial}</span>
        <div class="profile-meta">
          <div class="profile-name">{title}</div>
          <div class="profile-sub" id="profileSub">classin-toolkit</div>
        </div>
        <span id="configBadge" class="badge">config</span>
      </div>
    </aside>
    <section class="app-main">
      <div class="topbar" role="banner">
        <div class="crumb">
          <button class="crumb-home" data-tab="operations">홈</button>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>
          <span class="crumb-current" id="crumbCurrent">운영</span>
        </div>
        <div class="topbar-actions">
          <div class="topbar-search">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
            <input placeholder="학생, 클래스, 활동 검색..." aria-label="검색">
          </div>
          <button class="topbar-bell" aria-label="알림">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a6 6 0 1112 0c0 7 3 7 3 9H3c0-2 3-2 3-9z"/><path d="M10 21a2 2 0 004 0"/></svg>
            <span class="dot"></span>
          </button>
        </div>
      </div>
      <main class="app-body">
    <section class="status-strip">
      <div class="status-group" data-label="인입" style="--cols:1">
        <div class="metric"><span>Webhook 원본</span><strong id="incomingCount">0</strong></div>
      </div>
      <div class="status-group" data-label="필요 조치" style="--cols:2">
        <div class="metric warn"><span>숙제 미제출</span><strong id="missingCount">0</strong></div>
        <div class="metric alert"><span>연락처 없음</span><strong id="noPhoneCount">0</strong></div>
      </div>
      <div class="status-group" data-label="처리 결과" style="--cols:3">
        <div class="metric"><span>문구 생성</span><strong id="dryRunCount">0</strong></div>
        <div class="metric ok"><span>발송 완료</span><strong id="sentCount">0</strong></div>
        <div class="metric alert"><span>발송 실패</span><strong id="failedCount">0</strong></div>
      </div>
    </section>

    <section id="tab-operations" class="tab-view active">
    <header class="page-head">
      <div>
        <div class="page-eyebrow" id="todayLabel">{today_korean} · {title}</div>
        <h2 class="page-title">오늘 운영 현황 <span class="wave" aria-hidden="true">👋</span></h2>
        <div class="page-sub">미제출 알림 · 시험 · 리포트를 한 곳에서 관리합니다.</div>
      </div>
      <div class="page-actions">
        <button class="secondary" data-jump="exam">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4M6 10l6-6 6 6"/><path d="M4 20h16"/></svg>
          시험 결과 업로드
        </button>
        <button data-action="sweepMissing">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4z"/></svg>
          미제출 sweep 실행
        </button>
      </div>
    </header>

    <div class="quick-actions" role="list">
      <button class="quick-card" data-action="sweepMissing" role="listitem">
        <span class="quick-icon warn">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a6 6 0 1112 0c0 7 3 7 3 9H3c0-2 3-2 3-9z"/><path d="M10 21a2 2 0 004 0"/></svg>
        </span>
        <span class="quick-meta">
          <span class="quick-title">미제출 일괄 알림</span>
          <span class="quick-desc" id="qaMissingDesc">미제출 학생 일괄 sweep</span>
        </span>
        <svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>
      </button>
      <button class="quick-card" data-jump="exam" role="listitem">
        <span class="quick-icon">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 9h18M8 3v4M16 3v4"/></svg>
        </span>
        <span class="quick-meta">
          <span class="quick-title">시험 결과 import</span>
          <span class="quick-desc">CSV/JSON → 미응시 sweep</span>
        </span>
        <svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>
      </button>
      <button class="quick-card" data-jump="report" role="listitem">
        <span class="quick-icon">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H6a2 2 0 00-2 2v14a2 2 0 002 2h12a2 2 0 002-2V9z"/><path d="M14 3v6h6"/></svg>
        </span>
        <span class="quick-meta">
          <span class="quick-title">리포트 만들기</span>
          <span class="quick-desc">일일 · 주간 리포트</span>
        </span>
        <svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>
      </button>
      <button class="quick-card" data-tab="performance" role="listitem">
        <span class="quick-icon blue">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3.5"/><path d="M2 20c0-3.5 3-6 7-6s7 2.5 7 6"/><circle cx="17" cy="9" r="2.5"/><path d="M16 14c3 0 6 1.8 6 5"/></svg>
        </span>
        <span class="quick-meta">
          <span class="quick-title">성과 대시보드</span>
          <span class="quick-desc">학생/코스 KPI · 추이</span>
        </span>
        <svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>
      </button>
    </div>

    <section class="grid">
      <div>
        <div class="panel hero-panel">
          <div class="panel-head">
            <div>
              <h2><span class="h2-dot"></span>오늘 미제출 상황</h2>
              <p>반복 누락, 연락처 보완, 발송 실패를 먼저 볼 수 있게 정리했습니다.</p>
            </div>
            <span class="section-subtitle">운영 큐</span>
          </div>
          <div class="control-grid">
            <label>조회 시간<input id="windowHours" type="number" min="1" step="1" value="24"></label>
            <label>수업 ID<input id="lessonId" type="text" placeholder="예: LSN-2026-04-21-1"></label>
            <button data-action="refreshMissing" class="secondary">목록 새로고침</button>
            <button data-action="sweepMissing">미제출 sweep</button>
          </div>
          <div class="toolbar" id="missingFilters" role="tablist" aria-label="미제출 필터">
            <button data-filter="all" class="active">전체<span class="chip-count" data-count="all">0</span></button>
            <button data-filter="needs_message">문구 필요<span class="chip-count" data-count="needs_message">0</span></button>
            <button data-filter="needs_review">검토 필요<span class="chip-count" data-count="needs_review">0</span></button>
            <button data-filter="needs_phone">연락처 없음<span class="chip-count" data-count="needs_phone">0</span></button>
            <button data-filter="needs_retry">실패<span class="chip-count" data-count="needs_retry">0</span></button>
            <button data-filter="repeat">반복<span class="chip-count" data-count="repeat">0</span></button>
            <button data-filter="done">완료<span class="chip-count" data-count="done">0</span></button>
          </div>
          <div id="missingTable" style="margin-top:8px"></div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h2><span class="h2-dot" style="background:var(--blue)"></span>알림 발송 현황</h2>
            <span class="section-subtitle">최근 80건</span>
          </div>
          <div id="notificationTable"></div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <div>
              <h2><span class="h2-dot" style="background:var(--accent)"></span>도구</h2>
              <p>일일/주간 리포트, 시험 처리, 메모, AI 질문을 한 곳에서.</p>
            </div>
          </div>
          <nav class="subtabs" aria-label="도구 탭">
            <button data-subtab="report" class="active">리포트</button>
            <button data-subtab="exam">시험</button>
            <button data-subtab="memo">메모</button>
            <button data-subtab="agent">AI 질문</button>
          </nav>

          <div data-subtabview="report" class="subtab-view active">
            <div class="actions">
              <div class="row">
                <label>일자<input id="dailyDate" type="date" value="{today}"></label>
                <button data-action="renderDaily">일일 리포트 생성</button>
              </div>
              <div class="row">
                <label>주 시작일<input id="weekDate" type="date" value="{week_start}"></label>
                <button data-action="approveWeekly" class="secondary">주간 승인</button>
              </div>
              <button data-action="generateWeekly">주간 드래프트 생성</button>
              <button data-action="refreshStatus" class="secondary">상태 새로고침</button>
            </div>
          </div>

          <div data-subtabview="exam" class="subtab-view">
            <div class="stack">
              <div class="actions">
                <label>CSV/JSON path<input id="examPath" type="text" placeholder="samples/exam_results_sample.csv"></label>
                <label>시험명<input id="examName" type="text" placeholder="4월 월말평가"></label>
                <label>시험일<input id="examDate" type="date" value="{today}"></label>
                <label>반<input id="examClassName" type="text"></label>
                <label>출처<input id="examSource" type="text" value="academy-db"></label>
                <label style="align-self:end; flex-direction:row; display:flex; align-items:center; gap:8px; color:var(--text-2); font-weight:600;"><input id="examDryRun" type="checkbox" checked style="width:auto; min-height:0;"> dry-run 모드</label>
                <button data-action="importExam" class="secondary">시험 import</button>
                <button data-action="previewMissingExam" class="secondary">미응시 확인</button>
                <button data-action="sweepMissingExam">미응시 sweep</button>
              </div>
              <div id="examPreview" class="exam-preview empty">미응시 확인 결과 없음</div>
            </div>
          </div>

          <div data-subtabview="memo" class="subtab-view">
            <div class="stack">
              <div class="actions">
                <label>ClassIn ID<input id="memoClassinId" type="text"></label>
                <label>태그<input id="memoTag" type="text" placeholder="예: 상담"></label>
              </div>
              <label>내용<textarea id="memoText" placeholder="학생/학부모 관련 메모를 입력하세요"></textarea></label>
              <button data-action="writeMemo">메모 저장</button>
            </div>
          </div>

          <div data-subtabview="agent" class="subtab-view">
            <div class="stack">
              <label>질문<textarea id="agentQuestion" placeholder="예: 이번 주 미제출이 가장 많은 반은?"></textarea></label>
              <button data-action="askAgent">질문 보내기</button>
            </div>
          </div>
        </div>
      </div>

      <aside>
        <div class="panel">
          <div class="panel-head" style="margin-bottom:10px">
            <h2><span class="h2-dot" style="background:var(--danger)"></span>다음 액션</h2>
            <span class="section-subtitle" id="actionQueueMeta">우선순위 순</span>
          </div>
          <div id="actionQueue" class="action-list"></div>
        </div>
        <div class="panel">
          <h2 style="margin-bottom:10px">설정</h2>
          <dl id="settings"></dl>
        </div>
        <div class="panel">
          <div class="panel-head" style="margin-bottom:10px">
            <h2>실행 로그</h2>
            <span class="section-subtitle">최근 활동</span>
          </div>
          <div id="log" class="log"></div>
        </div>
      </aside>
    </section>
    </section>

    <section id="tab-performance" class="tab-view">
      <header class="page-head">
        <div>
          <div class="page-eyebrow">학생 · 코스 분석</div>
          <h2 class="page-title">성과 대시보드</h2>
          <div class="page-sub" id="performanceScope">전체 코스</div>
        </div>
        <span class="section-subtitle" id="performancePeriod">최근 90일</span>
      </header>
      <div class="panel hero-panel">
        <div class="performance-controls">
          <div class="segmented" id="dashboardMode">
            <button data-mode="course" class="active">코스</button>
            <button data-mode="student">학생</button>
          </div>
          <label class="mode-field" data-mode-field="course-search">코스 검색<input id="courseSearch" type="search" placeholder="코스명 또는 ID"></label>
          <label class="mode-field" data-mode-field="course-select">코스 선택<select id="courseSelect"></select></label>
          <label class="mode-field hidden" data-mode-field="student-search">학생 검색<input id="studentSearch" type="search" placeholder="학생명 또는 ID"></label>
          <label class="mode-field hidden" data-mode-field="student-select">학생 선택<select id="studentSelect"></select></label>
          <label>기간<select id="dashboardDays">
            <option value="30">30일</option>
            <option value="60">60일</option>
            <option value="90" selected>90일</option>
            <option value="180">180일</option>
            <option value="365">365일</option>
          </select></label>
          <button data-action="refreshDashboard">대시보드 갱신</button>
        </div>

        <div class="dashboard-grid">
          <div>
            <div class="hero-metrics">
              <div class="kpi-card">
                <span>학생</span>
                <strong id="perfStudentCount">0</strong>
                <small id="perfLessonCount">수업 0건</small>
              </div>
              <div class="kpi-card">
                <span>출석율</span>
                <strong id="perfAttendanceRate">-</strong>
                <small id="perfAttendanceHint">출석+지각 기준</small>
              </div>
              <div class="kpi-card">
                <span>평균 성적</span>
                <strong id="perfScoreAvg">-</strong>
                <small id="perfScoreDelta">추이 없음</small>
              </div>
              <div class="kpi-card">
                <span>관리 필요</span>
                <strong id="perfRiskCount">0</strong>
                <small id="perfMissingCount">미제출 0건</small>
              </div>
            </div>
            <div class="chart-grid">
              <div class="chart-card">
                <div class="chart-head">
                  <h3>성적 추이</h3>
                  <span id="scoreTrendMeta">-</span>
                </div>
                <div id="scoreTrendChart" class="chart-box"></div>
              </div>
              <div class="chart-card">
                <div class="chart-head">
                  <h3>출석율 추이</h3>
                  <span id="attendanceTrendMeta">-</span>
                </div>
                <div id="attendanceTrendChart" class="chart-box"></div>
              </div>
            </div>
          </div>

          <div class="attention-panel">
            <div class="chart-card">
              <div class="chart-head">
                <h3>집중 관리</h3>
                <span id="attentionMeta">0명</span>
              </div>
              <div id="attentionList" class="attention-list"></div>
            </div>
            <div class="chart-card">
              <div class="chart-head">
                <h3>상승 학생</h3>
                <span id="moverMeta">0명</span>
              </div>
              <div id="moverList" class="attention-list"></div>
            </div>
          </div>
        </div>

        <div id="studentCards" class="student-card-grid"></div>
      </div>
    </section>
      </main>
    </section>
  </div>

  <script id="initial-status" type="application/json">{status_json}</script>
  <script>
    const log = document.querySelector("#log");
    const statusNode = document.querySelector("#initial-status");
    let status = JSON.parse(statusNode.textContent);
    let missingState = {{ summary: {{}}, items: [] }};
    let activeMissingFilter = "all";
    let dashboardMode = "course";
    let courseOptions = [];
    let studentOptions = [];
    let optionSearchTimer = null;

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
      badge.textContent = status.mode === "demo" ? "demo mode" : (status.ok ? "config loaded" : "config needed");

      const output = status.output || {{}};
      const webhook = status.webhook || {{}};
      const rows = [
        ["mode", status.mode || "live"],
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
      }};
      return labels[status] || status || "-";
    }}

    function statusPill(status) {{
      const safe = escapeHtml(status || "pending");
      return `<span class="status-pill ${{safe}}">${{escapeHtml(statusLabel(status))}}</span>`;
    }}

    function actionLabel(action) {{
      const labels = {{
        needs_phone: "연락처 보완",
        needs_message: "문구 생성 필요",
        needs_review: "문구 검토",
        needs_retry: "재시도 필요",
        done: "처리 완료",
      }};
      return labels[action] || action || "-";
    }}

    function actionDetail(item) {{
      if (item.action_required === "needs_phone") return "학부모 연락처가 없어 발송할 수 없습니다.";
      if (item.action_required === "needs_message") return "sweep을 실행해 안내 문구를 생성하세요.";
      if (item.action_required === "needs_review") return "dry-run 문구가 생성되었습니다. 발송 전 확인 대상입니다.";
      if (item.action_required === "needs_retry") return "마지막 발송이 실패했습니다. 설정과 연락처를 확인하세요.";
      return "알림 처리가 완료된 학생입니다.";
    }}

    function contextHtml(item) {{
      const context = item.report_context || {{}};
      const badges = Array.isArray(context.badges) ? context.badges.slice(0, 2) : [];
      if (!context.has_context || !badges.length) return "";
      return `<div class="context-line">${{badges
        .map((badge) => `<span class="context-badge">${{escapeHtml(badge)}}</span>`)
        .join("")}}</div>`;
    }}

    function contextSummaryHtml(item) {{
      const context = item.report_context || {{}};
      if (!context.has_context || !context.summary) return "";
      return `<span class="context-summary">${{escapeHtml(context.summary)}}</span>`;
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

    async function loadDashboardOptions(kind, query) {{
      const path = kind === "student" ? "/api/course-dashboard/students" : "/api/course-dashboard/courses";
      const params = new URLSearchParams();
      if (query) params.set("q", query);
      params.set("limit", "40");
      const response = await fetch(`${{path}}?${{params.toString()}}`);
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || "dashboard options load failed");
      }}
      if (kind === "student") {{
        studentOptions = data.items || [];
        renderStudentSelect();
      }} else {{
        courseOptions = data.items || [];
        renderCourseSelect();
      }}
      return data;
    }}

    function renderCourseSelect(selected) {{
      const select = document.querySelector("#courseSelect");
      const current = selected ?? select.value;
      const rows = [
        '<option value="">전체 코스</option>',
        ...courseOptions.map((item) => `<option value="${{escapeHtml(item.course_id)}}">${{escapeHtml(item.label || item.course_id)}}</option>`),
      ];
      select.innerHTML = rows.join("");
      select.value = [...select.options].some((option) => option.value === current) ? current : "";
    }}

    function renderStudentSelect(selected) {{
      const select = document.querySelector("#studentSelect");
      const current = selected ?? select.value;
      const rows = [
        '<option value="">학생 선택</option>',
        ...studentOptions.map((item) => `<option value="${{escapeHtml(item.student_classin_id)}}">${{escapeHtml(item.label || item.student_name)}}</option>`),
      ];
      select.innerHTML = rows.join("");
      select.value = [...select.options].some((option) => option.value === current) ? current : "";
    }}

    function syncDashboardMode() {{
      document.querySelectorAll("#dashboardMode button").forEach((button) => {{
        button.classList.toggle("active", button.dataset.mode === dashboardMode);
      }});
      document.querySelectorAll("[data-mode-field]").forEach((field) => {{
        const isStudentField = field.dataset.modeField.startsWith("student");
        field.classList.toggle("hidden", dashboardMode === "course" ? isStudentField : !isStudentField);
      }});
    }}

    async function loadDashboard() {{
      if (!status.ok) {{
        renderDashboard({{ summary: {{}}, students: [], score_trend: [], attendance_trend: [], needs_attention: [], top_movers: [] }});
        return;
      }}
      const params = new URLSearchParams();
      params.set("days", document.querySelector("#dashboardDays").value || "90");
      if (dashboardMode === "student") {{
        const studentId = document.querySelector("#studentSelect").value;
        if (studentId) params.set("student_id", studentId);
      }} else {{
        const courseId = document.querySelector("#courseSelect").value;
        if (courseId) params.set("course_id", courseId);
      }}
      const response = await fetch(`/api/course-dashboard?${{params.toString()}}`);
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || "dashboard load failed");
      }}
      courseOptions = data.course_options || courseOptions;
      studentOptions = data.student_options || studentOptions;
      renderCourseSelect(data.filters?.course_id || document.querySelector("#courseSelect").value);
      renderStudentSelect(data.filters?.student_id || document.querySelector("#studentSelect").value);
      renderDashboard(data);
      return data;
    }}

    function renderDashboard(data) {{
      const summary = data.summary || {{}};
      document.querySelector("#performanceScope").textContent = summary.scope_label || "전체 코스";
      document.querySelector("#performancePeriod").textContent = `최근 ${{data.filters?.days || document.querySelector("#dashboardDays").value || 90}}일`;
      document.querySelector("#perfStudentCount").textContent = summary.student_count || 0;
      document.querySelector("#perfLessonCount").textContent = `수업 ${{summary.lesson_count || 0}}건`;
      document.querySelector("#perfAttendanceRate").textContent = percent(summary.attendance_rate);
      document.querySelector("#perfScoreAvg").textContent = scoreText(summary.avg_score);
      document.querySelector("#perfScoreDelta").textContent = deltaText(summary.score_delta);
      document.querySelector("#perfRiskCount").textContent = summary.risk_count || 0;
      document.querySelector("#perfMissingCount").textContent = `미제출 ${{summary.homework_missing || 0}}건`;

      const scoreTrend = data.score_trend || [];
      const attendanceTrend = data.attendance_trend || [];
      document.querySelector("#scoreTrendMeta").textContent = scoreTrend.length ? `${{scoreTrend.length}}주` : "-";
      document.querySelector("#attendanceTrendMeta").textContent = attendanceTrend.length ? `${{attendanceTrend.length}}주` : "-";
      document.querySelector("#scoreTrendChart").innerHTML = lineChart(scoreTrend, "avg_score", {{ suffix: "점", stroke: "#2f6f64", fill: "rgba(47,111,100,.12)", min: 0, max: 100 }});
      document.querySelector("#attendanceTrendChart").innerHTML = attendanceChart(attendanceTrend);
      renderAttentionList("#attentionList", data.needs_attention || [], false);
      renderAttentionList("#moverList", data.top_movers || [], true);
      document.querySelector("#attentionMeta").textContent = `${{(data.needs_attention || []).length}}명`;
      document.querySelector("#moverMeta").textContent = `${{(data.top_movers || []).length}}명`;
      renderStudentCards(data.students || []);
    }}

    function renderAttentionList(selector, items, movers) {{
      const target = document.querySelector(selector);
      if (!items.length) {{
        target.innerHTML = `<div class="empty">${{movers ? "상승 추이가 잡힌 학생이 없습니다." : "집중 관리 학생이 없습니다."}}</div>`;
        return;
      }}
      target.innerHTML = items.map((item) => `
        <div class="attention-item ${{escapeHtml(item.risk_level || "good")}}">
          <strong>${{escapeHtml(item.student_name || "미등록")}}</strong>
          <span class="cell-sub">${{escapeHtml(item.class_name || "-")}} · ${{escapeHtml(item.student_classin_id || "-")}}</span>
          <div class="mini-bars">
            <span class="mini-bar">출석 <span class="mini-track"><span class="mini-fill" style="width:${{rateWidth(item.attendance_rate)}}"></span></span></span>
            <span class="mini-bar">성적 <span class="mini-track"><span class="mini-fill" style="width:${{scoreWidth(item.score_avg)}}"></span></span></span>
            <span class="mini-bar">미제출 <span class="mini-track"><span class="mini-fill" style="width:${{Math.min((item.homework_missing || 0) * 24, 100)}}%; background: var(--accent)"></span></span></span>
          </div>
          <span class="cell-sub">${{movers ? deltaText(item.score_delta) : riskText(item)}}</span>
        </div>
      `).join("");
    }}

    function renderStudentCards(items) {{
      const target = document.querySelector("#studentCards");
      if (!items.length) {{
        target.innerHTML = `<div class="empty">표시할 학생 데이터가 없습니다.</div>`;
        return;
      }}
      target.innerHTML = items.map((item) => `
        <div class="student-card">
          <div class="student-card-head">
            <div>
              <div class="cell-title">${{escapeHtml(item.student_name || "미등록")}}</div>
              <div class="cell-sub">${{escapeHtml(item.class_name || "-")}} · ${{escapeHtml(item.student_classin_id || "-")}}</div>
              <div class="meta-row">
                <span class="status-pill ${{item.risk_level === "high" ? "failed" : item.risk_level === "medium" ? "pending" : "sent"}}">${{riskLabel(item.risk_level)}}</span>
                <span class="badge">수업 ${{item.lesson_count || 0}}건</span>
              </div>
            </div>
            <div class="ring" style="--pct:${{Math.round((item.attendance_rate || 0) * 100)}}"><span>${{percent(item.attendance_rate)}}</span></div>
          </div>
          <div class="mini-bars">
            <span class="mini-bar">성적 ${{scoreText(item.score_avg)}}<span class="mini-track"><span class="mini-fill" style="width:${{scoreWidth(item.score_avg)}}"></span></span></span>
            <span class="mini-bar">지각 ${{item.late_count || 0}}<span class="mini-track"><span class="mini-fill" style="width:${{Math.min((item.late_count || 0) * 20, 100)}}%; background: var(--blue)"></span></span></span>
            <span class="mini-bar">미제출 ${{item.homework_missing || 0}}<span class="mini-track"><span class="mini-fill" style="width:${{Math.min((item.homework_missing || 0) * 24, 100)}}%; background: var(--accent)"></span></span></span>
          </div>
          <div class="sparkline">${{sparkline(item.score_points || [])}}</div>
          <span class="cell-sub">${{deltaText(item.score_delta)}}</span>
        </div>
      `).join("");
    }}

    function lineChart(points, key, options) {{
      const usable = points.filter((point) => point[key] !== null && point[key] !== undefined);
      if (!usable.length) return chartEmpty("성적 데이터 없음");
      const width = 640;
      const height = 210;
      const pad = 28;
      const min = options.min ?? Math.min(...usable.map((point) => Number(point[key])));
      const max = options.max ?? Math.max(...usable.map((point) => Number(point[key])));
      const span = Math.max(1, max - min);
      const coords = usable.map((point, index) => {{
        const x = pad + (usable.length === 1 ? 0 : index * (width - pad * 2) / (usable.length - 1));
        const y = height - pad - ((Number(point[key]) - min) / span) * (height - pad * 2);
        return {{ x, y, point }};
      }});
      const d = coords.map((coord, index) => `${{index ? "L" : "M"}} ${{coord.x.toFixed(1)}} ${{coord.y.toFixed(1)}}`).join(" ");
      const area = `${{d}} L ${{coords.at(-1).x.toFixed(1)}} ${{height - pad}} L ${{coords[0].x.toFixed(1)}} ${{height - pad}} Z`;
      return `
        <svg viewBox="0 0 ${{width}} ${{height}}" role="img" aria-label="성적 추이">
          <defs>
            <linearGradient id="scoreFill" x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stop-color="${{options.fill || "rgba(47,111,100,.18)"}}"/>
              <stop offset="100%" stop-color="rgba(255,255,255,0)"/>
            </linearGradient>
          </defs>
          <path d="${{area}}" fill="url(#scoreFill)"></path>
          <path d="${{d}}" fill="none" stroke="${{options.stroke || "#2f6f64"}}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"></path>
          ${{coords.map((coord) => `<circle cx="${{coord.x.toFixed(1)}}" cy="${{coord.y.toFixed(1)}}" r="4.5" fill="#fff" stroke="${{options.stroke || "#2f6f64"}}" stroke-width="3"><title>${{escapeHtml(coord.point.label || coord.point.date)}} · ${{escapeHtml(coord.point[key])}}${{options.suffix || ""}}</title></circle>`).join("")}}
          ${{coords.map((coord, index) => index % Math.max(1, Math.ceil(coords.length / 5)) === 0 ? `<text x="${{coord.x.toFixed(1)}}" y="${{height - 6}}" text-anchor="middle" font-size="12" fill="#667085">${{escapeHtml(coord.point.label || "")}}</text>` : "").join("")}}
        </svg>
      `;
    }}

    function attendanceChart(points) {{
      if (!points.length) return chartEmpty("출석 데이터 없음");
      const width = 640;
      const height = 210;
      const pad = 28;
      const barWidth = Math.max(12, (width - pad * 2) / Math.max(points.length, 1) * .54);
      const coords = points.map((point, index) => {{
        const x = pad + (points.length === 1 ? 0 : index * (width - pad * 2) / (points.length - 1));
        const y = height - pad - (Number(point.attendance_rate || 0) * (height - pad * 2));
        return {{ x, y, point }};
      }});
      const d = coords.map((coord, index) => `${{index ? "L" : "M"}} ${{coord.x.toFixed(1)}} ${{coord.y.toFixed(1)}}`).join(" ");
      return `
        <svg viewBox="0 0 ${{width}} ${{height}}" role="img" aria-label="출석율 추이">
          ${{coords.map((coord) => `
            <rect x="${{(coord.x - barWidth / 2).toFixed(1)}}" y="${{coord.y.toFixed(1)}}" width="${{barWidth.toFixed(1)}}" height="${{(height - pad - coord.y).toFixed(1)}}" rx="6" fill="rgba(47,111,100,.18)"></rect>
            <circle cx="${{coord.x.toFixed(1)}}" cy="${{coord.y.toFixed(1)}}" r="4" fill="#2f6f64"><title>${{escapeHtml(coord.point.label)}} · ${{percent(coord.point.attendance_rate)}}</title></circle>
          `).join("")}}
          <path d="${{d}}" fill="none" stroke="#2f6f64" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"></path>
          ${{coords.map((coord, index) => index % Math.max(1, Math.ceil(coords.length / 5)) === 0 ? `<text x="${{coord.x.toFixed(1)}}" y="${{height - 6}}" text-anchor="middle" font-size="12" fill="#667085">${{escapeHtml(coord.point.label || "")}}</text>` : "").join("")}}
        </svg>
      `;
    }}

    function sparkline(points) {{
      const usable = points.filter((point) => point.value !== null && point.value !== undefined);
      if (!usable.length) return chartEmpty("성적 없음");
      const width = 240;
      const height = 56;
      const min = Math.min(0, ...usable.map((point) => Number(point.value)));
      const max = Math.max(100, ...usable.map((point) => Number(point.value)));
      const span = Math.max(1, max - min);
      const d = usable.map((point, index) => {{
        const x = usable.length === 1 ? 0 : index * width / (usable.length - 1);
        const y = height - ((Number(point.value) - min) / span) * height;
        return `${{index ? "L" : "M"}} ${{x.toFixed(1)}} ${{y.toFixed(1)}}`;
      }}).join(" ");
      return `<svg viewBox="0 0 ${{width}} ${{height}}" aria-hidden="true"><path d="${{d}}" fill="none" stroke="#2f6f64" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"></path></svg>`;
    }}

    function chartEmpty(message) {{
      return `<div class="chart-empty">${{escapeHtml(message)}}</div>`;
    }}

    function percent(value) {{
      return value === null || value === undefined ? "-" : `${{Math.round(Number(value) * 100)}}%`;
    }}

    function scoreText(value) {{
      return value === null || value === undefined ? "-" : `${{Number(value).toFixed(1)}}`;
    }}

    function deltaText(value) {{
      if (value === null || value === undefined) return "추이 없음";
      const sign = Number(value) > 0 ? "+" : "";
      return `최근 변화 ${{sign}}${{Number(value).toFixed(1)}}점`;
    }}

    function riskLabel(value) {{
      return value === "high" ? "위험" : value === "medium" ? "주의" : "안정";
    }}

    function riskText(item) {{
      return `출석 ${{percent(item.attendance_rate)}} · 성적 ${{scoreText(item.score_avg)}} · 미제출 ${{item.homework_missing || 0}}건`;
    }}

    function rateWidth(value) {{
      return `${{Math.max(0, Math.min(100, Math.round((value || 0) * 100)))}}%`;
    }}

    function scoreWidth(value) {{
      return `${{Math.max(0, Math.min(100, Math.round(value || 0)))}}%`;
    }}

    function examFormPayload() {{
      return {{
        path: document.querySelector("#examPath").value,
        exam_name: document.querySelector("#examName").value,
        exam_date: document.querySelector("#examDate").value,
        class_name: document.querySelector("#examClassName").value,
        source: document.querySelector("#examSource").value,
        dry_run: document.querySelector("#examDryRun").checked,
      }};
    }}

    async function loadMissingExamPreview() {{
      const payload = examFormPayload();
      const params = new URLSearchParams();
      params.set("exam_name", payload.exam_name || "");
      params.set("exam_date", payload.exam_date || "");
      if (payload.class_name) params.set("class_name", payload.class_name);

      const response = await fetch(`/api/missing-exam?${{params.toString()}}`);
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || "missing exam load failed");
      }}
      renderExamPreview(data);
      return data;
    }}

    function renderExamPreview(data) {{
      const target = document.querySelector("#examPreview");
      const summary = data.summary || {{}};
      const items = data.items || [];
      if (!items.length) {{
        target.className = "exam-preview empty";
        target.innerHTML = "미응시 확인 결과 없음";
        return;
      }}

      target.className = "exam-preview";
      target.innerHTML = `
        <div class="exam-preview-head">
          <strong>${{summary.total_missing || 0}}명 미응시</strong>
          <span>${{summary.with_parent_phone || 0}}명 연락 가능 · ${{summary.no_parent_phone || 0}}명 연락처 없음</span>
        </div>
        <div class="exam-preview-list">
          ${{items.slice(0, 12).map((item) => `
            <div class="exam-preview-item">
              <div>
                <div class="cell-title">${{escapeHtml(item.student_name || "미등록")}}</div>
                <div class="cell-sub">${{escapeHtml(item.class_name || "-")}} · ${{escapeHtml(item.student_classin_id || "-")}}</div>
              </div>
              ${{item.has_parent_phone
                ? `<span class="badge ok">${{escapeHtml(item.parent_phone)}}</span>`
                : '<span class="status-pill failed">연락처 없음</span>'}}
            </div>
          `).join("")}}
        </div>
      `;
    }}

    function renderMissing(data) {{
      missingState = data;
      const summary = data.summary || {{}};
      document.querySelector("#missingCount").textContent = summary.total_missing || 0;
      const sideBadge = document.querySelector("#sideBadgeMissing");
      if (sideBadge) {{
        const total = summary.total_missing || 0;
        sideBadge.textContent = total;
        sideBadge.hidden = total === 0;
      }}
      const qaDesc = document.querySelector("#qaMissingDesc");
      if (qaDesc) {{
        const total = summary.total_missing || 0;
        qaDesc.textContent = total ? `미제출 ${{total}}명 대기` : "미제출 학생 없음";
      }}
      document.querySelector("#noPhoneCount").textContent = summary.no_parent_phone || 0;
      document.querySelector("#dryRunCount").textContent = summary.dry_run || 0;
      document.querySelector("#sentCount").textContent = summary.sent || 0;
      document.querySelector("#failedCount").textContent = summary.failed || 0;

      renderActionQueue(data.items || []);
      updateFilterCounts(data.items || []);
      syncFilterButtons();

      const items = (data.items || []).filter(itemMatchesFilter);
      const target = document.querySelector("#missingTable");
      if (!items.length) {{
        target.innerHTML = `<div class="empty">선택한 조건에 해당하는 학생이 없습니다.</div>`;
        return;
      }}
      target.innerHTML = `
        <div class="table-wrap">
          <table class="missing-table">
            <colgroup>
              <col class="col-student">
              <col class="col-lesson">
              <col class="col-phone">
              <col class="col-status">
              <col class="col-action">
            </colgroup>
            <thead>
              <tr>
                <th>학생</th>
                <th>수업 / 출석</th>
                <th>학부모 연락처</th>
                <th>알림 상태</th>
                <th>다음 액션</th>
              </tr>
            </thead>
            <tbody>
              ${{items.map((item) => `
                <tr>
                  <td>
                    <div class="cell-title">${{escapeHtml(item.student_name)}}</div>
                    <div class="cell-sub">${{escapeHtml(item.class_name || "-")}}</div>
                    <div class="meta-row"><span class="badge">${{escapeHtml(item.student_classin_id)}}</span></div>
                    ${{contextHtml(item)}}
                  </td>
                  <td>
                    <div class="cell-title">${{escapeHtml(item.lesson_classin_id || "-")}}</div>
                    <div class="meta-row">
                      <span class="status-pill pending">${{escapeHtml(item.attendance || "-")}}</span>
                      ${{item.is_repeat ? `<span class="repeat-mark">반복 ${{escapeHtml(item.missing_count)}}건</span>` : ""}}
                    </div>
                    <div class="cell-sub">${{escapeHtml(formatDate(item.date))}}</div>
                  </td>
                  <td>
                    ${{item.has_parent_phone
                      ? `<div class="cell-title">${{escapeHtml(item.parent_phone)}}</div>`
                      : '<span class="status-pill failed">연락처 없음</span>'}}
                  </td>
                  <td>
                    <div class="cell-stack">
                      ${{statusPill(item.notification_status)}}
                      <span class="badge">${{escapeHtml(formatDate(item.notification_at))}}</span>
                    </div>
                  </td>
                  <td>
                    <div class="cell-title">${{escapeHtml(actionLabel(item.action_required))}}</div>
                    <span class="action-copy">${{escapeHtml(actionDetail(item))}}</span>
                  </td>
                </tr>
              `).join("")}}
            </tbody>
          </table>
        </div>`;
    }}

    function itemMatchesFilter(item) {{
      if (activeMissingFilter === "all") return true;
      if (activeMissingFilter === "repeat") return Boolean(item.is_repeat);
      return item.action_required === activeMissingFilter;
    }}

    function syncFilterButtons() {{
      document.querySelectorAll("#missingFilters button").forEach((button) => {{
        button.classList.toggle("active", button.dataset.filter === activeMissingFilter);
      }});
    }}

    function updateFilterCounts(items) {{
      const counts = {{
        all: items.length,
        needs_message: 0,
        needs_review: 0,
        needs_phone: 0,
        needs_retry: 0,
        repeat: 0,
        done: 0,
      }};
      items.forEach((item) => {{
        const key = item.action_required;
        if (key && counts[key] !== undefined) counts[key] += 1;
        if (item.is_repeat) counts.repeat += 1;
      }});
      document.querySelectorAll("#missingFilters .chip-count").forEach((node) => {{
        const key = node.dataset.count;
        node.textContent = counts[key] ?? 0;
      }});
    }}

    function renderActionQueue(items) {{
      const target = document.querySelector("#actionQueue");
      const meta = document.querySelector("#actionQueueMeta");
      const queue = items
        .filter((item) => item.action_required !== "done")
        .sort((a, b) => actionRank(a) - actionRank(b))
        .slice(0, 6);
      const pending = items.filter((item) => item.action_required !== "done").length;
      if (meta) meta.textContent = pending ? `처리 대기 ${{pending}}건` : "처리 완료";
      if (!queue.length) {{
        target.innerHTML = `<div class="empty">지금 바로 처리할 학생이 없습니다.</div>`;
        return;
      }}
      target.innerHTML = queue.map((item) => `
        <div class="action-item ${{escapeHtml(item.action_required)}}">
          <strong>${{escapeHtml(item.student_name)}} · ${{escapeHtml(actionLabel(item.action_required))}}</strong>
          <span>${{escapeHtml(item.class_name || "-")}} / ${{escapeHtml(item.lesson_classin_id || "-")}} / ${{item.is_repeat ? `반복 ${{item.missing_count}}건` : "단건"}}</span>
          <span>${{escapeHtml(actionDetail(item))}}</span>
          ${{contextSummaryHtml(item)}}
        </div>
      `).join("");
    }}

    function actionRank(item) {{
      const ranks = {{
        needs_retry: 1,
        needs_phone: 2,
        needs_message: 3,
        needs_review: 4,
        done: 9,
      }};
      const repeatBoost = item.is_repeat ? -0.5 : 0;
      return (ranks[item.action_required] || 8) + repeatBoost;
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
          <table class="notification-table">
            <colgroup>
              <col class="col-time">
              <col class="col-student">
              <col class="col-phone">
              <col class="col-status">
              <col class="col-message">
            </colgroup>
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
                  <td><div class="cell-sub">${{escapeHtml(formatDate(item.created_at))}}</div></td>
                  <td>
                    <div class="cell-title">${{escapeHtml(item.student_name || "미등록")}}</div>
                    <div class="meta-row"><span class="badge">${{escapeHtml(item.student_classin_id || "")}}</span></div>
                  </td>
                  <td><div class="cell-title">${{escapeHtml(item.parent_phone || "-")}}</div></td>
                  <td>
                    <div class="cell-stack">
                      ${{statusPill(item.status)}}
                      <span class="badge">${{escapeHtml(item.provider || "-")}}</span>
                    </div>
                  </td>
                  <td><span class="action-copy">${{escapeHtml(shorten(item.message || "", 120))}}</span></td>
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

    const actions = {{
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
      async importExam() {{
        const data = await callApi("/api/import-exam-results", examFormPayload());
        writeLog(data.message, data);
      }},
      async previewMissingExam() {{
        const data = await loadMissingExamPreview();
        writeLog("미응시자 확인을 갱신했습니다.", data.summary);
      }},
      async sweepMissingExam() {{
        const payload = examFormPayload();
        const data = await callApi("/api/sweep-missing-exam", {{
          exam_name: payload.exam_name,
          exam_date: payload.exam_date,
          class_name: payload.class_name,
        }});
        writeLog(data.message, data);
        await loadMissingExamPreview();
        await loadSituation();
      }},
      async refreshMissing() {{
        await loadSituation();
        writeLog("미제출/알림 상황을 갱신했습니다.");
      }},
      async refreshDashboard() {{
        const data = await loadDashboard();
        writeLog("성과 대시보드를 갱신했습니다.", data.summary);
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
        await loadSituation();
        writeLog("상태를 갱신했습니다.");
      }},
    }};

    document.addEventListener("click", async (event) => {{
      const button = event.target.closest("button[data-action]");
      const filterButton = event.target.closest("button[data-filter]");
      const tabButton = event.target.closest("button[data-tab]");
      const modeButton = event.target.closest("button[data-mode]");
      const subtabButton = event.target.closest("button[data-subtab]");
      const jumpButton = event.target.closest("button[data-jump]");
      if (jumpButton) {{
        const target = jumpButton.dataset.jump;
        document.querySelectorAll(".sidenav button[data-tab]").forEach((item) => {{
          item.classList.toggle("active", item.dataset.tab === "operations");
        }});
        document.querySelectorAll(".tab-view").forEach((view) => view.classList.toggle("active", view.id === "tab-operations"));
        const crumb = document.querySelector("#crumbCurrent");
        if (crumb) crumb.textContent = "운영";
        const subtab = document.querySelector(`button[data-subtab="${{target}}"]`);
        if (subtab) {{
          const scope = subtab.closest(".panel");
          if (scope) {{
            scope.querySelectorAll("button[data-subtab]").forEach((btn) => btn.classList.toggle("active", btn === subtab));
            scope.querySelectorAll("[data-subtabview]").forEach((view) => view.classList.toggle("active", view.dataset.subtabview === target));
            scope.scrollIntoView({{ behavior: "smooth", block: "start" }});
          }}
        }}
        return;
      }}
      if (subtabButton) {{
        const target = subtabButton.dataset.subtab;
        const scope = subtabButton.closest(".panel");
        if (scope) {{
          scope.querySelectorAll("button[data-subtab]").forEach((btn) => btn.classList.toggle("active", btn === subtabButton));
          scope.querySelectorAll("[data-subtabview]").forEach((view) => view.classList.toggle("active", view.dataset.subtabview === target));
        }}
        return;
      }}
      if (tabButton) {{
        const targetTab = tabButton.dataset.tab;
        document.querySelectorAll(".sidenav button[data-tab]").forEach((item) => {{
          item.classList.toggle("active", item.dataset.tab === targetTab);
        }});
        document.querySelectorAll(".tab-view").forEach((view) => view.classList.toggle("active", view.id === `tab-${{targetTab}}`));
        const crumb = document.querySelector("#crumbCurrent");
        if (crumb) crumb.textContent = targetTab === "performance" ? "성과 대시보드" : "운영";
        if (targetTab === "performance") {{
          try {{
            await loadDashboard();
          }} catch (error) {{
            writeLog(error.message);
          }}
        }}
        return;
      }}
      if (modeButton) {{
        dashboardMode = modeButton.dataset.mode || "course";
        syncDashboardMode();
        try {{
          await loadDashboard();
        }} catch (error) {{
          writeLog(error.message);
        }}
        return;
      }}
      if (filterButton) {{
        activeMissingFilter = filterButton.dataset.filter || "all";
        renderMissing(missingState);
        return;
      }}
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

    document.querySelector("#courseSearch").addEventListener("input", (event) => {{
      clearTimeout(optionSearchTimer);
      optionSearchTimer = setTimeout(() => {{
        loadDashboardOptions("course", event.target.value).catch((error) => writeLog(error.message));
      }}, 220);
    }});

    document.querySelector("#studentSearch").addEventListener("input", (event) => {{
      clearTimeout(optionSearchTimer);
      optionSearchTimer = setTimeout(() => {{
        loadDashboardOptions("student", event.target.value).catch((error) => writeLog(error.message));
      }}, 220);
    }});

    document.querySelector("#courseSelect").addEventListener("change", () => {{
      if (dashboardMode === "course") loadDashboard().catch((error) => writeLog(error.message));
    }});

    document.querySelector("#studentSelect").addEventListener("change", () => {{
      if (dashboardMode === "student") loadDashboard().catch((error) => writeLog(error.message));
    }});

    document.querySelector("#dashboardDays").addEventListener("change", () => {{
      loadDashboard().catch((error) => writeLog(error.message));
    }});

    renderStatus(status);
    syncDashboardMode();
    Promise.all([
      loadDashboardOptions("course", ""),
      loadDashboardOptions("student", ""),
    ]).then(() => loadDashboard()).catch((error) => writeLog(error.message));
    loadSituation().catch((error) => writeLog(error.message));
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
