"""로컬 운영 UI (FastAPI).

원장/컨설턴트가 반복 실행하는 CLI 작업을 브라우저 버튼으로 감싼 얇은 계층이다.
비즈니스 로직은 pipelines/와 intelligence/의 기존 함수를 그대로 호출한다.
"""
from __future__ import annotations

import html
import json
import re
from datetime import date as date_cls
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import yaml

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

    @app.get("/api/connections")
    async def api_connections() -> dict:
        if state.demo:
            payload = _demo_status_payload(state.config_path)
            return {"ok": True, "items": payload.get("connections", [])}
        cfg, error = state.load()
        if not cfg:
            return {"ok": False, "items": [], "error": error}
        return {"ok": True, "items": _connection_payload(cfg)}

    @app.get("/api/profile")
    async def api_profile_get() -> dict:
        return _profile_payload(state)

    @app.post("/api/profile")
    async def api_profile_post(request: Request) -> JSONResponse:
        payload = await _json_payload(request)
        saved = _save_profile(state, payload)
        return _ok("프로필이 저장되었습니다. 변경된 자격증명은 다음 실행부터 반영됩니다.", profile=saved)

    @app.get("/api/missing-homework")
    async def api_missing_homework(
        window_hours: int = 24,
        lesson_id: str | None = None,
        course_id: str | None = None,
    ) -> dict:
        if state.demo:
            return _demo_missing_homework_payload()
        cfg = _require_config(state)
        lesson_filter = (lesson_id or "").strip() or None
        course_filter = (course_id or "").strip() or None
        rows = query_missing_homework(
            cfg,
            window_hours=window_hours,
            lesson_id=lesson_filter,
        )
        if course_filter:
            rows = [
                row for row in rows
                if str(row.get("course_classin_id") or "") == course_filter
            ]
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

    @app.post("/api/schedule/parse")
    async def api_schedule_parse(request: Request) -> JSONResponse:
        payload = await _json_payload(request)
        text = (payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text 또는 image_base64 값이 필요합니다.")
        if state.demo:
            rows = _parse_schedule_heuristic(text)
            return _demo_ok(
                "Demo mode: 텍스트에서 스케줄을 추출했습니다.",
                items=rows,
                source="heuristic",
            )
        cfg = _require_config(state)
        ak = (cfg.anthropic.api_key or "").strip()
        if ak and not ak.lower().startswith(("test", "dummy", "your-")):
            try:
                from .intelligence.schedule_parser import parse_schedule_text

                rows = parse_schedule_text(cfg, text)
                return _ok("AI가 스케줄을 인식했습니다.", items=rows, source="claude")
            except Exception as exc:  # noqa: BLE001
                rows = _parse_schedule_heuristic(text)
                return _ok(
                    f"AI 호출 실패, 휴리스틱 파서로 폴백했습니다: {exc}",
                    items=rows,
                    source="heuristic",
                )
        rows = _parse_schedule_heuristic(text)
        return _ok("Claude API 키가 없어 휴리스틱 파서로 인식했습니다.", items=rows, source="heuristic")

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
        "connections": _connection_payload(cfg),
    }


PROFILE_FIELDS = (
    "academy_name",
    "classin_school_id",
    "classin_secret_key",
    "classin_webhook_secret",
    "notion_token",
    "anthropic_api_key",
    "aligo_api_key",
    "aligo_user_id",
    "aligo_sender",
    "notify_mode",
)
SECRET_FIELDS = {
    "classin_secret_key",
    "classin_webhook_secret",
    "notion_token",
    "anthropic_api_key",
    "aligo_api_key",
}


def _profile_path(state: "_ConfigState") -> Path:
    base = state.config_path.parent if state.config_path else Path.cwd()
    return base / "profile.local.yaml"


def _read_profile(state: "_ConfigState") -> dict[str, str]:
    path = _profile_path(state)
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: str(data.get(key) or "") for key in PROFILE_FIELDS if data.get(key)}


def _mask_secret(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return ""
    if len(s) <= 6:
        return "•" * len(s)
    return s[:3] + "•" * max(4, len(s) - 6) + s[-3:]


def _profile_defaults_from_config(cfg: AppConfig | None) -> dict[str, str]:
    if not cfg:
        return {}
    return {
        "academy_name": cfg.academy.name,
        "classin_school_id": cfg.classin.school_id,
        "classin_secret_key": cfg.classin.secret_key,
        "classin_webhook_secret": cfg.classin.webhook_secret,
        "notion_token": cfg.notion.token,
        "anthropic_api_key": cfg.anthropic.api_key,
        "aligo_api_key": cfg.notify.aligo.api_key,
        "aligo_user_id": cfg.notify.aligo.user_id,
        "aligo_sender": cfg.notify.aligo.sender,
        "notify_mode": cfg.notify.mode,
    }


def _profile_payload(state: "_ConfigState") -> dict[str, Any]:
    saved = _read_profile(state)
    if not saved:
        cfg, _ = state.load()
        saved = _profile_defaults_from_config(cfg)
    masked: dict[str, str] = {}
    filled: dict[str, bool] = {}
    for key in PROFILE_FIELDS:
        value = (saved.get(key) or "").strip()
        filled[key] = bool(value)
        if key in SECRET_FIELDS:
            masked[key] = _mask_secret(value)
        else:
            masked[key] = value
    return {
        "ok": True,
        "path": str(_profile_path(state)),
        "exists": _profile_path(state).exists(),
        "fields": masked,
        "filled": filled,
    }


def _save_profile(state: "_ConfigState", payload: dict[str, Any]) -> dict[str, Any]:
    path = _profile_path(state)
    current = _read_profile(state)
    next_values = dict(current)
    for key in PROFILE_FIELDS:
        if key not in payload:
            continue
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            next_values.pop(key, None)
            continue
        next_values[key] = text
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(next_values, allow_unicode=True, sort_keys=True), encoding="utf-8")
    return _profile_payload(state)


def _connection_payload(cfg: AppConfig | None) -> list[dict[str, Any]]:
    if not cfg:
        return []
    ak = (cfg.anthropic.api_key or "").strip()
    anthropic_ok = bool(ak) and not ak.lower().startswith(("test", "dummy", "your-"))
    aligo = cfg.notify.aligo
    aligo_ok = bool((aligo.api_key or "").strip() and (aligo.user_id or "").strip() and (aligo.sender or "").strip())
    notify_live = cfg.notify.mode == "live"
    classin_ok = bool((cfg.classin.school_id or "").strip() and (cfg.classin.secret_key or "").strip())
    notion_ok = bool((cfg.notion.token or "").strip() and (cfg.notion.databases.students or "").strip())
    return [
        {
            "key": "claude",
            "label": "Claude AI",
            "kind": "API",
            "connected": anthropic_ok,
            "detail": f"model · {cfg.anthropic.model}" if anthropic_ok else "ANTHROPIC_API_KEY 미설정",
        },
        {
            "key": "classin",
            "label": "ClassIn API",
            "kind": "API",
            "connected": classin_ok,
            "detail": "appid + secret 확인" if classin_ok else "appid/secret 미설정",
        },
        {
            "key": "notify",
            "label": "알림톡 / SMS (Aligo)",
            "kind": "발송",
            "connected": aligo_ok,
            "detail": (
                f"{'LIVE' if notify_live else 'DRY_RUN'} · sender {aligo.sender or '-'}"
                if aligo_ok else "api_key·user_id·sender 미설정"
            ),
        },
        {
            "key": "notion",
            "label": "Notion 저장소",
            "kind": "출력",
            "connected": notion_ok,
            "detail": "주간 리포트/메모 동기화" if notion_ok else "Notion api_key 미설정",
        },
    ]


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
        "connections": [
            {"key": "claude", "label": "Claude AI", "kind": "API", "connected": True,
             "detail": "demo · claude-sonnet-4-6"},
            {"key": "classin", "label": "ClassIn API", "kind": "API", "connected": True,
             "detail": "demo school_id"},
            {"key": "notify", "label": "알림톡 / SMS (Aligo)", "kind": "발송", "connected": False,
             "detail": "DRY_RUN · 연결 안됨"},
            {"key": "notion", "label": "Notion 저장소", "kind": "출력", "connected": False,
             "detail": "demo 모드 — 외부 쓰기 비활성"},
        ],
    }


def _count_files(path: Path, pattern: str) -> int:
    if not path.exists():
        return 0
    return sum(1 for p in path.glob(pattern) if p.is_file())


_DAY_TOKENS = ("월", "화", "수", "목", "금", "토", "일")
_SCHEDULE_LINE_RE = re.compile(
    r"^\s*(?P<day>[월화수목금토일])\s*[\.,/ -]*\s*"
    r"(?P<time>\d{1,2}[:시]\d{0,2}\s*[-~–]\s*\d{1,2}[:시]\d{0,2})\s+"
    r"(?P<rest>.+)$"
)


def _parse_schedule_heuristic(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _SCHEDULE_LINE_RE.match(line)
        if not m:
            continue
        day = m.group("day")
        time = m.group("time").replace("시", ":").replace(" ", "").replace("~", "-").replace("–", "-")
        if ":" not in time.split("-")[0]:
            continue
        tokens = m.group("rest").split()
        room = tokens[-1] if tokens and (tokens[-1].endswith("호") or tokens[-1].endswith("실")) else ""
        teacher = ""
        class_tokens = tokens
        if room:
            class_tokens = tokens[:-1]
        if class_tokens and len(class_tokens[-1]) <= 4 and not any(
            ch.isdigit() for ch in class_tokens[-1]
        ):
            teacher = class_tokens[-1]
            class_name = " ".join(class_tokens[:-1]).strip()
        else:
            class_name = " ".join(class_tokens).strip()
        rows.append(
            {
                "day": day,
                "time": time,
                "class_name": class_name or "(미지정)",
                "teacher": teacher,
                "room": room,
                "confidence": 0.92 if class_name and teacher else 0.78,
            }
        )
    return rows


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
    /* ============ Home dashboard (Classin++ design) ============ */
    .dash-greeting {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 24px;
    }}
    .dash-eyebrow {{ font-size: 13px; color: var(--muted); margin-bottom: 6px; }}
    .dash-title {{
      margin: 0;
      font-size: 28px;
      font-weight: 700;
      letter-spacing: -0.025em;
      color: var(--ink);
      line-height: 1.2;
    }}
    .dash-title .wave {{ display: inline-block; margin-left: 4px; }}
    .dash-sub {{ font-size: 14px; color: var(--muted-2); margin-top: 6px; }}
    .dash-actions {{ display: flex; gap: 8px; flex-shrink: 0; }}

    .kpi-hero {{
      display: grid;
      grid-template-columns: 1.4fr 1fr 1fr 1fr;
      gap: 16px;
      margin-bottom: 28px;
    }}
    .kpi-card {{
      background: var(--surface, #fff);
      border: 1px solid var(--line);
      border-radius: 14px;
      box-shadow: 0 1px 0 rgba(15,23,17,.04), 0 8px 24px -16px rgba(15,23,17,.12);
      padding: 20px 22px;
      display: flex;
      flex-direction: column;
      gap: 0;
      transition: transform .15s ease, box-shadow .15s ease;
    }}
    .kpi-card--main {{
      padding: 0;
      flex-direction: row;
      overflow: hidden;
    }}
    .kpi-card--main .kpi-body {{ padding: 20px 22px; flex: 1; }}
    .kpi-card--main .kpi-side {{
      padding: 20px 22px;
      display: flex;
      align-items: center;
      justify-content: center;
      border-left: 1px solid var(--line-2);
      background: var(--green-4);
    }}
    .kpi-label {{
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 600;
    }}
    .kpi-figure {{
      display: flex;
      align-items: baseline;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 14px;
    }}
    .kpi-num {{
      font-size: 32px;
      font-weight: 700;
      letter-spacing: -0.03em;
      color: var(--ink);
      font-variant-numeric: tabular-nums;
      line-height: 1.1;
    }}
    .kpi-card--main .kpi-num {{ font-size: 36px; }}
    .kpi-num-sub {{
      color: var(--muted-2);
      font-weight: 600;
      font-size: 20px;
      font-variant-numeric: tabular-nums;
    }}
    .kpi-num-unit {{
      font-size: 16px;
      color: var(--muted-2);
      font-weight: 600;
    }}
    .kpi-pill {{
      display: inline-flex;
      align-items: center;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      background: var(--warn-bg);
      color: #92520a;
    }}
    .kpi-pill.ok {{ background: var(--green-3); color: var(--green-ink); }}
    .kpi-bar {{
      margin-top: 14px;
      height: 8px;
      border-radius: 999px;
      background: var(--green-4);
      overflow: hidden;
    }}
    .kpi-bar-fill {{
      display: block;
      height: 100%;
      background: var(--green);
      border-radius: inherit;
      transition: width .3s ease;
    }}
    .kpi-cta {{
      margin-top: 14px;
      font-size: 13px;
      color: var(--green-2);
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      background: transparent;
      border: none;
      padding: 0;
      box-shadow: none;
      cursor: pointer;
      min-height: auto;
    }}
    .kpi-cta:hover {{ color: var(--ink); background: transparent; box-shadow: none; }}
    .kpi-foot {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
    .kpi-foot.up {{ color: var(--green-2); font-weight: 600; }}
    .kpi-spark {{ margin-top: 10px; height: 32px; }}
    .kpi-spark svg {{ width: 100%; height: 100%; display: block; }}
    .kpi-mini-bars {{
      margin-top: 10px;
      display: flex;
      gap: 3px;
      height: 32px;
    }}
    .kpi-mini-bars > div {{
      flex: 1;
      background: var(--green-4);
      border-radius: 4px;
      display: flex;
      align-items: flex-end;
      overflow: hidden;
    }}
    .kpi-mini-bars > div > span {{
      display: block;
      width: 100%;
      background: var(--green);
      border-radius: 4px;
    }}

    .section-title {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 12px;
      margin: 0 0 14px;
    }}
    .section-title h3 {{
      margin: 0;
      font-size: 18px;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: var(--ink);
    }}
    .section-title p {{
      margin: 4px 0 0;
      font-size: 13px;
      color: var(--muted);
    }}
    .section-title .link-btn {{
      font-size: 13px;
      font-weight: 600;
      color: var(--green-2);
      background: transparent;
      border: none;
      box-shadow: none;
      cursor: pointer;
      padding: 0;
      min-height: auto;
    }}

    .dash-2col {{
      display: grid;
      grid-template-columns: 1.6fr 1fr;
      gap: 20px;
      margin-top: 28px;
    }}
    /* Homework list rows on dashboard */
    .hw-today-row {{
      padding: 16px 20px;
      display: grid;
      grid-template-columns: 1fr auto auto;
      gap: 16px;
      align-items: center;
      border-bottom: 1px solid var(--line-2);
      cursor: pointer;
      transition: background .12s;
      background: transparent;
      width: 100%;
      text-align: left;
      border-left: 0;
      border-right: 0;
      border-top: 0;
      min-height: auto;
      font: inherit;
      color: inherit;
    }}
    .hw-today-row:last-child {{ border-bottom: none; }}
    .hw-today-row:hover {{ background: var(--green-4); transform: none; box-shadow: none; }}
    .hw-today-row-title {{ font-size: 14px; font-weight: 600; margin-bottom: 4px; color: var(--ink); }}
    .hw-today-row-meta {{ display: flex; gap: 8px; font-size: 12px; color: var(--muted); }}
    .hw-today-row-prog {{ min-width: 160px; }}
    .hw-today-row-prog-head {{ display: flex; justify-content: space-between; font-size: 12px; color: var(--muted); margin-bottom: 4px; }}
    .hw-today-row-prog-head .warn {{ color: #92520a; font-weight: 600; }}
    .hw-today-row-prog-head .ok {{ color: var(--green-2); font-weight: 600; }}
    .hw-today-row-bar {{ height: 6px; border-radius: 999px; background: var(--green-4); overflow: hidden; }}
    .hw-today-row-bar > span {{ display: block; height: 100%; background: var(--green); border-radius: inherit; }}
    .hw-today-row-bar.warn > span {{ background: var(--warn); }}

    /* Activity feed rows */
    .activity-row {{
      padding: 14px 18px;
      display: flex;
      gap: 12px;
      align-items: flex-start;
      border-bottom: 1px solid var(--line-2);
    }}
    .activity-row:last-child {{ border-bottom: none; }}
    .activity-icon {{
      width: 28px;
      height: 28px;
      border-radius: 8px;
      display: flex;
      align-items: center;
      justify-content: center;
      flex: 0 0 auto;
    }}
    .activity-icon.score {{ background: #dbeafe; color: #1e40af; }}
    .activity-icon.warn  {{ background: #fef3c7; color: #92520a; }}
    .activity-icon.ai    {{ background: var(--green-3); color: var(--green-ink); }}
    .activity-icon.ok    {{ background: var(--green-3); color: var(--green-ink); }}
    .activity-row-text {{ font-size: 13.5px; color: var(--ink-2); line-height: 1.5; }}
    .activity-row-time {{ font-size: 11px; color: var(--muted-2); margin-top: 4px; }}

    @media (max-width: 1100px) {{
      .kpi-hero {{ grid-template-columns: 1fr 1fr; }}
      .kpi-card--main {{ grid-column: 1 / -1; }}
      .dash-2col {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 600px) {{
      .kpi-hero {{ grid-template-columns: 1fr; }}
      .kpi-card--main {{ flex-direction: column; }}
      .kpi-card--main .kpi-side {{ border-left: 0; border-top: 1px solid var(--line-2); }}
      .dash-greeting {{ flex-direction: column; align-items: flex-start; }}
    }}

    /* ============ Students tab (Classin++ clean table) ============ */
    .student-toolbar {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      padding: 12px 14px;
      background: var(--surface, #fff);
      border: 1px solid var(--line);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow);
      margin-bottom: 14px;
    }}
    .student-toolbar .segmented {{ margin: 0; padding: 3px; background: var(--green-4); border: 1px solid var(--line-2); border-radius: 10px; }}
    .student-toolbar .segmented button {{ min-height: 30px; padding: 4px 12px; font-size: 13px; }}
    .student-toolbar .segmented button.active {{ background: #fff; box-shadow: var(--shadow-soft); color: var(--green-2); border-color: var(--line); }}
    .student-toolbar .mode-field {{ display: block; min-width: 180px; }}
    .student-toolbar .mode-field input, .student-toolbar .mode-field select {{ min-height: 34px; font-size: 13px; }}
    .student-toolbar .period-field {{ display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 12px; font-weight: 600; }}
    .student-toolbar .period-field select {{ min-height: 32px; min-width: 90px; padding: 4px 8px; font-size: 13px; }}

    .student-table-card {{
      background: var(--surface, #fff);
      border: 1px solid var(--line);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .student-table-head {{
      padding: 12px 20px;
      display: grid;
      grid-template-columns: 2.2fr 2fr 1fr 1.4fr 1fr 40px;
      gap: 16px;
      font-size: 11.5px;
      color: var(--muted);
      font-weight: 700;
      letter-spacing: .04em;
      text-transform: uppercase;
      border-bottom: 1px solid var(--line-2);
      background: linear-gradient(180deg, #fafdf9, #f4f8f1);
    }}
    .student-row {{
      padding: 14px 20px;
      display: grid;
      grid-template-columns: 2.2fr 2fr 1fr 1.4fr 1fr 40px;
      gap: 16px;
      align-items: center;
      border-bottom: 1px solid var(--line-2);
      cursor: pointer;
      transition: background .12s;
      background: #fff;
      border-left: 0;
      border-right: 0;
      border-top: 0;
      width: 100%;
      text-align: left;
      min-height: auto;
      font: inherit;
      color: inherit;
    }}
    .student-row:last-child {{ border-bottom: none; }}
    .student-row:hover {{ background: var(--green-4); transform: none; box-shadow: none; }}
    .student-row .student-row-name {{ display: flex; align-items: center; gap: 10px; min-width: 0; }}
    .student-row .student-avatar {{
      width: 32px; height: 32px; border-radius: 50%;
      display: inline-flex; align-items: center; justify-content: center;
      font-weight: 700; font-size: 13px; flex: 0 0 auto;
    }}
    .student-row .student-name-text {{ font-size: 14px; font-weight: 600; color: var(--ink); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .student-row .student-class-text {{ font-size: 13px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .student-row .student-num {{ font-size: 13px; font-weight: 600; font-variant-numeric: tabular-nums; }}
    .student-row .hw-cell {{ display: flex; align-items: center; gap: 8px; }}
    .student-row .hw-cell .hw-bar {{ flex: 1; height: 6px; border-radius: 999px; background: var(--green-4); overflow: hidden; min-width: 40px; }}
    .student-row .hw-cell .hw-bar > span {{ display: block; height: 100%; border-radius: inherit; }}
    .student-row .hw-cell .hw-pct {{ font-size: 12px; font-weight: 600; min-width: 32px; text-align: right; font-variant-numeric: tabular-nums; }}
    .student-row .chev {{ color: var(--muted-2); justify-self: end; }}

    .student-analytics {{
      margin-top: 14px;
      background: var(--surface, #fff);
      border: 1px solid var(--line);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow);
      padding: 14px 20px;
    }}
    .student-analytics summary {{
      cursor: pointer;
      font-size: 13px;
      font-weight: 700;
      color: var(--text-2);
      list-style: none;
      padding: 4px 0;
      user-select: none;
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .student-analytics summary::-webkit-details-marker {{ display: none; }}
    .student-analytics summary::after {{
      content: "▸";
      margin-left: auto;
      transition: transform .15s ease;
      color: var(--muted);
    }}
    .student-analytics[open] summary::after {{ transform: rotate(90deg); }}
    .student-analytics .kpi-card-mini {{
      padding: 12px 14px;
      background: var(--green-4);
      border: 1px solid var(--line-2);
      border-radius: var(--radius);
    }}
    .student-analytics .kpi-card-mini span {{ color: var(--muted); font-size: 11.5px; font-weight: 600; }}
    .student-analytics .kpi-card-mini strong {{ display: block; margin-top: 6px; font-size: 22px; font-weight: 700; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }}
    .student-analytics .kpi-card-mini small {{ display: block; margin-top: 4px; color: var(--muted); font-size: 11px; }}

    .student-empty {{
      padding: 48px 20px;
      text-align: center;
      color: var(--muted);
    }}
    @media (max-width: 900px) {{
      .student-table-head, .student-row {{ grid-template-columns: 2fr 1fr 1fr 40px; }}
      .student-table-head > div:nth-child(2),
      .student-row .student-class-cell {{ display: none; }}
    }}

    /* ============ Report builder (Classin++ A4 preview) ============ */
    .report-format-bar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 12px 14px;
      background: var(--surface, #fff);
      border: 1px solid var(--line);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow);
      margin-bottom: 16px;
    }}
    .report-format-left {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
    .report-format-label {{ font-size: 13px; font-weight: 600; color: var(--text); }}
    .report-format-seg {{ padding: 3px; background: #f1f5f0; border: 1px solid var(--line-2); border-radius: 8px; }}
    .report-format-seg button {{ min-height: 28px; padding: 6px 14px; font-size: 12.5px; font-weight: 600; color: var(--muted); background: transparent; border: 1px solid transparent; border-radius: 6px; box-shadow: none; }}
    .report-format-seg button.active {{ background: #fff; color: var(--green-2); box-shadow: 0 1px 2px rgba(0,0,0,0.06); }}
    .report-format-help {{ font-size: 12px; color: var(--muted); }}
    .report-format-pill {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 3px 10px;
      border-radius: 999px;
      background: var(--green-3);
      color: var(--green-ink);
      font-size: 11px;
      font-weight: 700;
    }}

    .report-grid {{
      display: grid;
      grid-template-columns: 260px 1fr;
      gap: 20px;
    }}
    .report-side {{ display: flex; flex-direction: column; gap: 16px; }}
    .report-side-card {{
      background: var(--surface, #fff);
      border: 1px solid var(--line);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow);
      padding: 16px;
    }}
    .report-side-title {{
      font-size: 13px;
      font-weight: 700;
      color: var(--ink);
      margin-bottom: 12px;
    }}
    .report-target {{
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 8px;
      border: 1px solid var(--green);
      border-radius: 8px;
      background: var(--green-4);
    }}
    .report-target-avatar {{
      width: 32px;
      height: 32px;
      border-radius: 50%;
      background: var(--green-3);
      color: var(--green-ink);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      font-size: 13px;
    }}
    .report-target-name {{ font-size: 13px; font-weight: 600; }}
    .report-target-class {{ font-size: 11px; color: var(--muted); margin-top: 2px; }}
    .report-target-hint {{ margin-top: 12px; font-size: 12px; color: var(--muted); }}

    .report-data-list {{ list-style: none; padding: 0; margin: 0; }}
    .report-data-list li {{ padding: 0; }}
    .report-data-list label {{
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 8px 0;
      font-size: 13px;
      color: var(--ink);
      font-weight: 500;
      cursor: pointer;
    }}
    .report-data-list label > span:first-of-type {{ flex: 1; }}
    .report-data-list input[type=checkbox] {{ width: 16px; height: 16px; min-height: 0; accent-color: var(--green); margin: 0; }}
    .src-pill {{
      font-size: 10px;
      padding: 2px 6px;
      border-radius: 4px;
      font-weight: 700;
      flex-shrink: 0;
    }}
    .src-pill.api  {{ background: var(--green-3); color: var(--green-ink); }}
    .src-pill.self {{ background: #e0e7ff; color: #3730a3; }}

    .report-comment {{ position: relative; }}
    .report-comment textarea {{
      width: 100%;
      min-height: 96px;
      padding: 10px 12px 36px;
      border: 1px solid var(--line);
      border-radius: 8px;
      font-size: 13px;
      font-family: inherit;
      resize: none;
      line-height: 1.5;
    }}
    .ai-polish-btn {{
      position: absolute;
      bottom: 10px;
      right: 10px;
      padding: 4px 10px;
      border-radius: 6px;
      background: var(--green-4);
      color: var(--green-2);
      border: 1px solid var(--green-3);
      font-size: 11px;
      font-weight: 700;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      min-height: auto;
      cursor: pointer;
    }}
    .ai-polish-btn:hover {{ background: var(--green-3); color: var(--green-ink); transform: none; box-shadow: none; }}

    .report-canvas {{
      background: #f4f7f1;
      border: 1px solid var(--line);
      border-radius: var(--radius-lg);
      overflow: hidden;
    }}
    .report-canvas-head {{
      padding: 12px 16px;
      border-bottom: 1px solid var(--line-2);
      background: #fff;
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 12px;
      color: var(--muted);
    }}
    .report-layout-pill {{
      padding: 3px 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #fff;
      color: var(--ink-2);
      font-size: 11px;
      font-weight: 600;
    }}
    .report-canvas-body {{
      padding: 24px;
      display: flex;
      justify-content: center;
    }}
    .report-paper {{
      width: 100%;
      max-width: 560px;
      background: #fff;
      border-radius: 8px;
      box-shadow: 0 8px 24px -8px rgba(15,23,17,.15);
      overflow: hidden;
      font-size: 11px;
      color: var(--ink);
    }}
    .report-paper-head {{
      padding: 16px 20px;
      background: #fff;
      border-bottom: 3px solid var(--green);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .report-paper-eyebrow {{
      font-size: 9px;
      opacity: .65;
      font-weight: 700;
      letter-spacing: .1em;
      margin-bottom: 3px;
    }}
    .report-paper-title {{
      font-size: 16px;
      font-weight: 700;
      letter-spacing: -0.02em;
    }}
    .report-paper-meta {{ font-size: 10px; opacity: .7; margin-top: 2px; }}
    .report-paper-brand {{ display: flex; align-items: center; gap: 6px; }}
    .report-paper-brand-name {{ font-size: 10px; font-weight: 700; color: var(--ink-2); }}
    .report-paper-brand-mark {{
      width: 18px;
      height: 18px;
      border-radius: 5px;
      background: var(--green);
      color: #fff;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 800;
      font-size: 10px;
    }}
    .report-paper-body {{ padding: 20px; }}
    .report-paper-section-title {{
      font-size: 10px;
      font-weight: 700;
      color: var(--muted);
      letter-spacing: .08em;
      text-transform: uppercase;
      margin: 18px 0 10px;
    }}
    .report-paper-section-title:first-child {{ margin-top: 0; }}
    .report-metric-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr 1fr 1fr;
      gap: 10px;
    }}
    .report-metric {{
      padding: 12px;
      border: 1px solid var(--line-2);
      border-radius: 8px;
      background: #fff;
    }}
    .report-metric .m-label {{ font-size: 9px; color: var(--muted); font-weight: 600; margin-bottom: 6px; }}
    .report-metric .m-value {{ font-size: 18px; font-weight: 700; color: var(--green); letter-spacing: -0.02em; line-height: 1; font-variant-numeric: tabular-nums; }}
    .report-metric .m-value span {{ font-size: 10px; color: var(--muted-2); font-weight: 600; margin-left: 2px; }}
    .report-metric .m-sub {{ font-size: 9px; color: var(--muted); margin-top: 5px; }}

    .report-score-bars {{ display: flex; flex-direction: column; gap: 9px; }}
    .score-bar .score-head {{ display: flex; justify-content: space-between; font-size: 10px; margin-bottom: 3px; }}
    .score-bar .score-head b {{ color: var(--green); }}
    .score-bar .bar-track {{
      position: relative;
      height: 6px;
      background: #f1f5f0;
      border-radius: 999px;
    }}
    .score-bar .bar-fill {{
      height: 100%;
      background: var(--green);
      border-radius: 999px;
    }}
    .score-bar .avg-mark {{
      position: absolute;
      top: -2px;
      bottom: -2px;
      width: 2px;
      background: var(--muted-2);
      border-radius: 1px;
    }}

    .report-tags {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    .tag-col {{ display: flex; flex-wrap: wrap; gap: 4px 6px; align-items: center; }}
    .tag-col-title {{
      width: 100%;
      font-size: 10px;
      font-weight: 700;
      margin-bottom: 4px;
    }}
    .tag-col-title.good {{ color: var(--green-2); }}
    .tag-col-title.warn {{ color: #92520a; }}
    .report-tag {{
      padding: 3px 8px;
      border-radius: 999px;
      font-size: 10px;
      font-weight: 600;
    }}
    .report-tag.good {{ background: var(--green-3); color: var(--green-ink); }}
    .report-tag.warn {{ background: var(--warn-bg); color: #92520a; }}

    .report-teacher-note {{
      padding: 12px 14px;
      background: #fafdf9;
      border: 1px solid var(--line-2);
      border-radius: 6px;
      font-size: 10.5px;
      line-height: 1.6;
      color: var(--ink-2);
    }}

    .report-advanced {{
      margin-top: 20px;
      background: var(--surface, #fff);
      border: 1px solid var(--line);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow);
      padding: 14px 20px;
    }}
    .report-advanced summary {{
      cursor: pointer;
      font-size: 13px;
      font-weight: 700;
      color: var(--text-2);
      list-style: none;
      padding: 4px 0;
      user-select: none;
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .report-advanced summary::-webkit-details-marker {{ display: none; }}
    .report-advanced summary::after {{ content: "▸"; margin-left: auto; transition: transform .15s ease; color: var(--muted); }}
    .report-advanced[open] summary::after {{ transform: rotate(90deg); }}
    .report-advanced-body {{ margin-top: 14px; }}

    @media (max-width: 1100px) {{
      .report-grid {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 600px) {{
      .report-format-bar {{ flex-direction: column; align-items: flex-start; }}
      .report-metric-grid {{ grid-template-columns: 1fr 1fr; }}
    }}
    .dropzone {{
      border: 2px dashed var(--primary);
      border-radius: var(--radius-lg);
      padding: 48px 32px;
      text-align: center;
      background: var(--primary-softer);
      display: grid;
      gap: 14px;
      justify-items: center;
    }}
    .dropzone-icon {{
      width: 64px;
      height: 64px;
      border-radius: 16px;
      background: #fff;
      color: var(--primary-strong);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      box-shadow: var(--shadow);
    }}
    .dropzone-title {{
      font-size: 18px;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: var(--text);
    }}
    .dropzone-desc {{
      font-size: 13.5px;
      color: var(--muted);
    }}
    .dropzone-btn {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 18px;
      border-radius: var(--radius-sm);
      background: var(--primary);
      color: #fff;
      border: 1px solid var(--primary);
      font-weight: 700;
      cursor: pointer;
    }}
    .dropzone-btn:hover {{
      background: var(--primary-strong);
      border-color: var(--primary-strong);
    }}
    .dropzone-alt {{
      font-size: 12px;
      color: var(--muted-2);
      margin-top: 4px;
    }}
    .schedule-hint {{
      margin-top: 20px;
      padding: 14px 16px;
      background: var(--panel-soft);
      border-radius: var(--radius);
      display: flex;
      align-items: center;
      gap: 12px;
      color: var(--text-2);
      font-size: 13px;
    }}
    .schedule-hint svg {{
      color: var(--primary-strong);
      flex: 0 0 auto;
    }}
    .schedule-hint > div {{ flex: 1; line-height: 1.5; }}
    .schedule-hint .badge {{
      background: var(--accent-soft);
      color: var(--accent-ink);
      border-color: rgba(180, 92, 25, .25);
    }}
    .schedule-paste {{
      margin-top: 18px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 12px 14px;
      background: #fff;
    }}
    .schedule-paste summary {{
      cursor: pointer;
      font-size: 13px;
      font-weight: 600;
      color: var(--text-2);
      list-style: none;
      padding: 4px 0;
      user-select: none;
    }}
    .schedule-paste summary::-webkit-details-marker {{ display: none; }}
    .schedule-paste summary::before {{
      content: "▸";
      display: inline-block;
      margin-right: 6px;
      transition: transform .15s ease;
    }}
    .schedule-paste[open] summary::before {{ transform: rotate(90deg); }}
    .schedule-paste textarea {{
      margin-top: 10px;
      min-height: 140px;
      font-family: ui-monospace, "SF Mono", Consolas, monospace;
      font-size: 12.5px;
      line-height: 1.55;
    }}
    .schedule-review {{
      margin-top: 18px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      overflow: hidden;
      background: #fff;
    }}
    .schedule-review-head {{
      padding: 12px 14px;
      background: var(--primary-softer);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      border-bottom: 1px solid var(--line-2);
    }}
    .schedule-review table {{ min-width: 0; }}
    .schedule-review td .low {{
      color: var(--accent-ink);
      font-weight: 700;
    }}
    .conn-list {{
      display: grid;
      gap: 8px;
      margin-bottom: 12px;
    }}
    .conn-item {{
      display: grid;
      grid-template-columns: auto minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: #fff;
      transition: border-color .15s ease, box-shadow .15s ease;
    }}
    .conn-item:hover {{
      border-color: var(--line-strong);
      box-shadow: var(--shadow-soft);
    }}
    .conn-led {{
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--muted-2);
      flex: 0 0 auto;
      box-shadow: 0 0 0 3px rgba(0,0,0,0);
      transition: background .2s ease, box-shadow .2s ease;
    }}
    .conn-item.on .conn-led {{
      background: var(--ok);
      box-shadow: 0 0 0 3px var(--ok-soft);
      animation: conn-pulse 2.4s ease-in-out infinite;
    }}
    .conn-item.off .conn-led {{
      background: var(--danger);
      box-shadow: 0 0 0 3px var(--danger-soft);
    }}
    @keyframes conn-pulse {{
      0%, 100% {{ box-shadow: 0 0 0 3px var(--ok-soft); }}
      50% {{ box-shadow: 0 0 0 5px rgba(31, 122, 82, .12); }}
    }}
    .conn-text {{
      min-width: 0;
      display: grid;
      gap: 2px;
    }}
    .conn-label {{
      font-size: 13px;
      font-weight: 700;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .conn-kind {{
      font-size: 10px;
      font-weight: 700;
      letter-spacing: .04em;
      padding: 1px 6px;
      border-radius: 999px;
      background: var(--panel-soft);
      color: var(--muted);
      text-transform: uppercase;
      border: 1px solid var(--line);
    }}
    .conn-detail {{
      font-size: 12px;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .conn-status {{
      font-size: 11.5px;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 999px;
    }}
    .conn-item.on .conn-status {{
      background: var(--ok-soft);
      color: var(--ok);
    }}
    .conn-item.off .conn-status {{
      background: var(--danger-soft);
      color: var(--danger-ink);
    }}
    .conn-details {{
      border-top: 1px solid var(--line-2);
      padding-top: 10px;
    }}
    .conn-details summary {{
      cursor: pointer;
      font-size: 12.5px;
      color: var(--muted);
      font-weight: 600;
      list-style: none;
      padding: 4px 0;
      user-select: none;
    }}
    .conn-details summary::-webkit-details-marker {{ display: none; }}
    .conn-details summary::before {{
      content: "▸";
      display: inline-block;
      margin-right: 6px;
      transition: transform .15s ease;
    }}
    .conn-details[open] summary::before {{
      transform: rotate(90deg);
    }}
    .conn-details dl {{ margin-top: 8px; }}
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
    .bulk-flow {{
      display: flex;
      flex-direction: column;
      gap: 16px;
      padding: 0;
      background: transparent;
      border: none;
      box-shadow: none;
    }}
    .step-card {{
      border: 1px solid var(--line);
      border-radius: var(--radius-lg);
      background: var(--panel);
      padding: 20px 22px;
      box-shadow: var(--shadow);
    }}
    .step-card + .step-card {{
      margin-top: 0;
    }}
    .step-head {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 14px;
    }}
    .step-head > div {{ flex: 1; min-width: 0; }}
    .step-head h3 {{
      margin: 0;
      font-size: 15px;
      font-weight: 700;
      color: var(--text);
      letter-spacing: -0.01em;
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }}
    .step-head h3::before {{
      content: "";
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--primary);
      flex-shrink: 0;
    }}
    .step-head p {{
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 12.5px;
      line-height: 1.45;
    }}
    .step-head code {{
      font-size: 11.5px;
      background: var(--panel-soft);
      border: 1px solid var(--line);
      border-radius: 4px;
      padding: 1px 5px;
      color: var(--text-2);
    }}
    .step-badge {{ display: none; }}
    /* Mode segment toggle for homework alert */
    .hw-mode-segment {{
      display: inline-flex;
      gap: 4px;
      padding: 4px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 12px;
      margin-bottom: 16px;
      box-shadow: var(--shadow);
    }}
    .hw-mode-segment button {{
      min-height: 38px;
      padding: 8px 18px;
      border-radius: 8px;
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-2);
      font-size: 13.5px;
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      box-shadow: none;
    }}
    .hw-mode-segment button:hover {{
      background: var(--panel-soft);
      color: var(--text);
      transform: none;
      box-shadow: none;
    }}
    .hw-mode-segment button.active {{
      background: var(--primary);
      color: #fff;
      border-color: var(--primary);
      box-shadow: 0 1px 2px rgba(15, 23, 42, .1);
    }}
    .hw-mode-segment .mode-count {{
      display: inline-flex;
      align-items: center;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      background: var(--accent-soft);
      color: var(--accent-ink);
    }}
    .hw-mode-segment button.active .mode-count {{
      background: rgba(255, 255, 255, .25);
      color: #fff;
    }}
    /* Class-grouped student list */
    .hw-class-group + .hw-class-group {{ margin-top: 8px; }}
    .hw-class-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 16px;
      background: var(--primary-softer);
      border-bottom: 1px solid var(--line-2);
      border-top: 1px solid var(--line-2);
      font-size: 12.5px;
    }}
    .hw-class-header label {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-weight: 700;
      color: var(--text);
      cursor: pointer;
    }}
    .hw-class-header label input {{
      width: 16px;
      height: 16px;
      min-height: 0;
      margin: 0;
      accent-color: var(--primary);
    }}
    .hw-class-meta {{
      font-size: 11.5px;
      color: var(--muted);
      font-weight: 600;
    }}
    /* AI hint banner in compose card */
    .ai-hint-banner {{
      margin-top: 14px;
      padding: 12px 14px;
      background: var(--primary-softer);
      border: 1px solid var(--primary-soft);
      border-radius: var(--radius);
      display: flex;
      align-items: center;
      gap: 12px;
      font-size: 13px;
      color: var(--primary-strong);
    }}
    .ai-hint-banner svg {{ color: var(--primary); flex-shrink: 0; }}
    .ai-hint-banner > div {{ flex: 1; line-height: 1.5; }}
    .ai-hint-banner b {{ color: var(--text); }}
    .ai-hint-banner .badge {{
      background: var(--primary);
      color: #fff;
      border: none;
      font-weight: 700;
    }}
    /* AI ON pill in header */
    .ai-on-pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 12px;
      border-radius: 999px;
      background: linear-gradient(135deg, var(--primary-softer), #fff);
      border: 1px solid var(--primary-soft);
      color: var(--primary-strong);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: -0.01em;
    }}
    .ai-on-pill::before {{
      content: "";
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--primary);
      animation: conn-pulse 2.4s ease-in-out infinite;
    }}
    .template-presets button {{
      border-radius: 999px;
    }}
    .template-tokens {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px;
      margin-bottom: 10px;
    }}
    .token-label {{
      font-size: 12px;
      color: var(--muted);
      font-weight: 600;
      margin-right: 2px;
    }}
    .token-chip {{
      min-height: 28px;
      padding: 3px 10px;
      font-size: 12px;
      font-weight: 600;
      border-radius: 999px;
      border: 1px dashed var(--line-strong);
      background: var(--panel-soft);
      color: var(--text-2);
      cursor: pointer;
    }}
    .token-chip:hover {{
      background: #fff;
      color: var(--primary-strong);
      border-color: var(--primary);
    }}
    .template-label {{
      display: grid;
      gap: 6px;
      margin-bottom: 10px;
    }}
    .template-preview {{
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--panel-soft);
      padding: 10px 12px;
    }}
    .preview-label {{
      display: block;
      font-size: 11.5px;
      color: var(--muted);
      font-weight: 600;
      margin-bottom: 4px;
      letter-spacing: 0.02em;
    }}
    .preview-body {{
      white-space: pre-wrap;
      color: var(--text);
      font-size: 13.5px;
      line-height: 1.55;
      min-height: 20px;
    }}
    .preview-body.empty {{
      color: var(--muted);
      font-style: italic;
    }}
    .send-card {{
      border: 1px solid var(--primary-soft, var(--line));
      background: linear-gradient(180deg, rgba(46,111,99,.04), rgba(255,255,255,.6));
    }}
    .send-summary {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }}
    .send-stat {{
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: #fff;
      padding: 10px 12px;
      display: flex;
      flex-direction: column;
      gap: 2px;
    }}
    .send-stat span {{
      font-size: 11.5px;
      color: var(--muted);
      font-weight: 600;
    }}
    .send-stat strong {{
      font-size: 20px;
      font-weight: 700;
      color: var(--text);
      font-variant-numeric: tabular-nums;
    }}
    .send-stat.warn strong {{ color: var(--danger, #b45309); }}
    .send-stat.ok strong {{ color: var(--primary-strong); }}
    .send-actions {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }}
    .send-toggle {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 12.5px;
      color: var(--text-2);
      font-weight: 600;
    }}
    .send-toggle input {{
      width: auto;
      min-height: 0;
      margin: 0;
    }}
    .bulk-send-btn {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 18px;
      font-weight: 700;
    }}
    .bulk-send-btn[disabled] {{
      opacity: .55;
      cursor: not-allowed;
    }}
    .btn-count {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 22px;
      height: 22px;
      padding: 0 7px;
      margin-left: 8px;
      border-radius: 999px;
      background: rgba(255,255,255,.22);
      color: inherit;
      font-size: 12px;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
    }}
    @media (max-width: 720px) {{
      .send-summary {{ grid-template-columns: 1fr 1fr; }}
      .send-actions {{ flex-direction: column; align-items: stretch; }}
      .send-actions .bulk-send-btn {{ justify-content: center; }}
    }}
    /* Missing-row list (Step 1 results) */
    .missing-list-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 8px 4px 6px;
      border-bottom: 1px dashed var(--line);
      margin-bottom: 4px;
    }}
    .select-all {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      font-weight: 600;
      color: var(--text);
      cursor: pointer;
    }}
    .select-all input {{ width: 16px; height: 16px; min-height: 0; margin: 0; accent-color: var(--primary); }}
    .missing-list-hint {{
      font-size: 11.5px;
      color: var(--muted);
    }}
    .missing-list {{
      display: flex;
      flex-direction: column;
    }}
    .missing-row {{
      display: grid;
      grid-template-columns: 24px 36px 1fr auto;
      align-items: center;
      gap: 12px;
      padding: 12px 6px;
      border-bottom: 1px solid var(--line);
      cursor: pointer;
      transition: background .12s ease;
    }}
    .missing-row:last-child {{ border-bottom: none; }}
    .missing-row:hover {{ background: var(--panel-soft); }}
    .missing-row--locked {{ opacity: .55; cursor: not-allowed; }}
    .missing-row-check {{
      width: 18px;
      height: 18px;
      min-height: 0;
      margin: 0;
      accent-color: var(--primary);
    }}
    .missing-row-avatar {{
      width: 32px;
      height: 32px;
      border-radius: 50%;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      color: #fff;
      font-size: 13.5px;
      letter-spacing: -0.02em;
    }}
    .missing-row-meta {{
      display: flex;
      flex-direction: column;
      gap: 2px;
      min-width: 0;
    }}
    .missing-row-name {{
      font-weight: 700;
      font-size: 14.5px;
      color: var(--text);
    }}
    .missing-row-sub {{
      font-size: 12.5px;
      color: var(--muted);
      font-variant-numeric: tabular-nums;
    }}
    .missing-row-warn {{ color: var(--danger, #b45309); font-weight: 600; }}
    .missing-row-stats {{
      display: inline-flex;
      align-items: center;
      gap: 14px;
      white-space: nowrap;
    }}
    .missing-row-last {{
      font-size: 12.5px;
      color: var(--muted);
    }}
    .streak-badge {{
      display: inline-flex;
      align-items: center;
      padding: 3px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
    }}
    .streak-badge.mute {{ background: #f1f3ef; color: var(--muted); border: 1px solid var(--line); }}
    .streak-badge.warn {{ background: #fff3df; color: #b45309; }}
    .streak-badge.danger {{ background: #fde2e1; color: #b42318; }}
    @media (max-width: 720px) {{
      .missing-row {{ grid-template-columns: 22px 32px 1fr; }}
      .missing-row-stats {{ grid-column: 2 / -1; justify-content: space-between; }}
    }}

    /* Step 2 — message composition */
    .msg-card .step-head {{ align-items: center; }}
    .ai-suggest {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-height: 30px;
      padding: 4px 12px;
      font-size: 12.5px;
      font-weight: 600;
      border-radius: 999px;
      border: 1px solid var(--primary);
      background: var(--primary-soft);
      color: var(--primary-strong);
    }}
    .ai-suggest:hover {{ background: #fff; }}
    .link-btn {{
      background: transparent;
      border: none;
      box-shadow: none;
      padding: 0;
      min-height: 0;
      color: var(--primary-strong);
      font-weight: 600;
      font-size: inherit;
      text-decoration: underline;
      cursor: pointer;
    }}
    .link-btn:hover {{ background: transparent; color: var(--primary); }}
    .tone-chips {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 14px;
    }}
    .tone-chips button {{
      min-height: 36px;
      padding: 6px 18px;
      border-radius: 8px;
      border: 1.5px solid var(--line-strong);
      background: #fff;
      color: var(--text-2);
      font-weight: 600;
      font-size: 13px;
    }}
    .tone-chips button:hover {{ border-color: var(--primary); color: var(--primary-strong); }}
    .tone-chips button.active {{
      border-color: var(--primary);
      color: var(--primary-strong);
      background: var(--primary-soft);
      box-shadow: 0 0 0 3px var(--primary-ring);
    }}
    .msg-grid {{
      display: grid;
      grid-template-columns: minmax(280px, 1fr) minmax(280px, 1.1fr);
      gap: 18px;
      margin-bottom: 8px;
    }}
    .msg-col {{ display: flex; flex-direction: column; gap: 8px; min-width: 0; }}
    .msg-col-label {{
      font-size: 12.5px;
      font-weight: 700;
      color: var(--text-2);
    }}
    .channel-list {{ display: flex; flex-direction: column; gap: 8px; }}
    .channel-option {{
      display: grid;
      grid-template-columns: 20px 1fr auto;
      align-items: center;
      gap: 10px;
      padding: 12px 14px;
      border: 1.5px solid var(--line);
      border-radius: var(--radius-sm);
      background: #fff;
      cursor: pointer;
    }}
    .channel-option input[type=radio] {{
      width: 16px;
      height: 16px;
      min-height: 0;
      margin: 0;
      accent-color: var(--primary);
    }}
    .channel-option.active {{
      border-color: var(--primary);
      background: var(--primary-soft);
      box-shadow: 0 0 0 3px var(--primary-ring);
    }}
    .channel-option.disabled {{ cursor: default; opacity: .85; }}
    .channel-meta {{ display: flex; flex-direction: column; gap: 2px; min-width: 0; }}
    .channel-title {{
      font-weight: 700;
      font-size: 13.5px;
      color: var(--text);
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }}
    .channel-desc {{ font-size: 12px; color: var(--muted); }}
    .channel-pill {{
      font-size: 11px;
      font-weight: 700;
      padding: 2px 7px;
      border-radius: 999px;
      background: #ffe5e1;
      color: #b42318;
    }}
    .channel-connect {{
      min-height: 28px;
      padding: 4px 12px;
      font-size: 12px;
      font-weight: 600;
      border-radius: 8px;
      border: 1px solid var(--line-strong);
      background: #fff;
      color: var(--primary-strong);
    }}
    .channel-connect:hover {{ border-color: var(--primary); background: var(--primary-soft); }}
    .msg-preview {{
      flex: 1;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--panel-soft);
      padding: 14px 16px;
      min-height: 140px;
    }}
    .msg-preview .preview-body {{ white-space: pre-wrap; font-size: 13.5px; line-height: 1.6; color: var(--text); }}
    .msg-preview .preview-body.empty {{ color: var(--muted); font-style: italic; }}
    .bulk-editor {{
      margin-top: 12px;
      padding-top: 12px;
      border-top: 1px dashed var(--line);
    }}
    .template-char-meta {{
      text-align: right;
      font-size: 11.5px;
      color: var(--muted);
      margin-top: 4px;
    }}
    @media (max-width: 760px) {{
      .msg-grid {{ grid-template-columns: 1fr; }}
    }}

    /* Course combobox */
    .combobox-label {{ position: relative; }}
    .combobox {{
      position: relative;
      display: block;
    }}
    .combobox-clear {{
      position: absolute;
      top: 50%;
      right: 8px;
      transform: translateY(-50%);
      background: transparent;
      border: none;
      box-shadow: none;
      padding: 0;
      width: 22px;
      height: 22px;
      min-height: 0;
      color: var(--muted);
      font-size: 16px;
      line-height: 1;
      cursor: pointer;
      border-radius: 50%;
    }}
    .combobox-clear:hover {{ background: var(--panel-soft); color: var(--text); }}
    .combobox-menu {{
      position: absolute;
      top: calc(100% + 4px);
      left: 0;
      right: 0;
      max-height: 280px;
      overflow-y: auto;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      box-shadow: var(--shadow-soft);
      z-index: 40;
      padding: 4px;
    }}
    .combobox-item {{
      display: flex;
      flex-direction: column;
      gap: 2px;
      width: 100%;
      padding: 8px 10px;
      border: 1px solid transparent;
      border-radius: 6px;
      background: transparent;
      color: var(--text);
      text-align: left;
      cursor: pointer;
      box-shadow: none;
      min-height: 0;
    }}
    .combobox-item:hover {{ background: var(--panel-soft); }}
    .combobox-item.selected {{ background: var(--primary-soft); border-color: var(--primary); }}
    .combobox-item-title {{ font-size: 13.5px; font-weight: 600; }}
    .combobox-item-sub {{ font-size: 11.5px; color: var(--muted); }}
    .combobox-empty {{
      padding: 12px;
      text-align: center;
      color: var(--muted);
      font-size: 12.5px;
    }}

    /* Profile page */
    .profile-panel {{ display: flex; flex-direction: column; gap: 16px; }}
    .profile-status {{
      padding: 10px 12px;
      border-radius: var(--radius-sm);
      font-size: 13px;
      font-weight: 600;
    }}
    .profile-status.ok {{ background: var(--primary-soft); color: var(--primary-strong); border: 1px solid var(--primary); }}
    .profile-status.warn {{ background: #fff7e6; color: #92400e; border: 1px solid #f4c270; }}
    .profile-section {{
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: rgba(255,255,255,.7);
      padding: 16px 18px;
    }}
    .profile-section-head {{ margin-bottom: 12px; }}
    .profile-section-head h3 {{
      margin: 0 0 4px;
      font-size: 14px;
      font-weight: 700;
      color: var(--text);
    }}
    .profile-section-head p {{
      margin: 0;
      color: var(--muted);
      font-size: 12.5px;
      line-height: 1.45;
    }}
    .profile-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    @media (max-width: 720px) {{
      .profile-grid {{ grid-template-columns: 1fr; }}
    }}
    .input-with-toggle {{
      position: relative;
      display: block;
    }}
    .input-with-toggle input {{ padding-right: 38px; }}
    .input-toggle {{
      position: absolute;
      top: 50%;
      right: 6px;
      transform: translateY(-50%);
      width: 26px;
      height: 26px;
      min-height: 0;
      padding: 0;
      background: transparent;
      border: none;
      box-shadow: none;
      color: var(--muted);
      font-size: 14px;
      cursor: pointer;
      border-radius: 50%;
    }}
    .input-toggle:hover {{ background: var(--panel-soft); color: var(--text); }}
    .input-toggle.active {{ color: var(--primary-strong); }}
    .profile-footnote {{
      color: var(--muted);
      font-size: 12px;
      padding: 6px 4px 0;
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
        <button data-tab="home" class="active" aria-current="page">
          <span class="nav-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/></svg></span>
          <span class="nav-label">홈</span>
        </button>
        <button data-tab="homework">
          <span class="nav-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a6 6 0 1112 0c0 7 3 7 3 9H3c0-2 3-2 3-9z"/><path d="M10 21a2 2 0 004 0"/></svg></span>
          <span class="nav-label">숙제 미제출 알림</span>
          <span class="nav-badge" id="sideBadgeMissing" hidden>0</span>
        </button>
        <button data-tab="schedule">
          <span class="nav-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 9h18M8 3v4M16 3v4"/></svg></span>
          <span class="nav-label">스케줄 자동 등록</span>
        </button>
        <button data-tab="report">
          <span class="nav-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20V8M10 20V4M16 20v-8M22 20H2"/></svg></span>
          <span class="nav-label">리포트 빌더</span>
        </button>
        <button data-tab="students">
          <span class="nav-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3.5"/><path d="M2 20c0-3.5 3-6 7-6s7 2.5 7 6"/><circle cx="17" cy="9" r="2.5"/><path d="M16 14c3 0 6 1.8 6 5"/></svg></span>
          <span class="nav-label">학생</span>
        </button>
        <button data-tab="profile">
          <span class="nav-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z"/></svg></span>
          <span class="nav-label">내 설정</span>
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
          <button class="crumb-home" data-tab="home">홈</button>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>
          <span class="crumb-current" id="crumbCurrent">홈</span>
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

    <section id="tab-home" class="tab-view active">
      <header class="dash-greeting">
        <div>
          <div class="dash-eyebrow" id="todayLabel">{today_korean} · {title}</div>
          <h1 class="dash-title">안녕하세요, 원장님 <span class="wave" aria-hidden="true">👋</span></h1>
          <div class="dash-sub" id="dashSub">오늘 운영 중인 클래스 현황을 한눈에 확인하세요.</div>
        </div>
        <div class="dash-actions">
          <button class="secondary" data-tab="schedule">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4M6 10l6-6 6 6"/><path d="M4 20h16"/></svg>
            스케줄 업로드
          </button>
          <button data-tab="homework">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>
            활동 만들기
          </button>
        </div>
      </header>

      <section class="kpi-hero">
        <article class="kpi-card kpi-card--main">
          <div class="kpi-body">
            <div class="kpi-label">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a6 6 0 1112 0c0 7 3 7 3 9H3c0-2 3-2 3-9z"/><path d="M10 21a2 2 0 004 0"/></svg>
              오늘 숙제 제출 현황
            </div>
            <div class="kpi-figure">
              <span class="kpi-num" id="hwSubmitted">0</span><span class="kpi-num-sub" id="hwTotal"> / 0</span>
              <span class="kpi-pill warn" id="hwMissingPill">미제출 0명</span>
            </div>
            <div class="kpi-bar"><span class="kpi-bar-fill" id="hwBarFill" style="width:0%"></span></div>
            <button class="kpi-cta" data-tab="homework">
              미제출자에게 한 번에 알림 보내기
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>
            </button>
          </div>
          <div class="kpi-side">
            <div class="kpi-donut" id="hwDonut" aria-hidden="true"></div>
          </div>
        </article>

        <article class="kpi-card">
          <div class="kpi-label"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3.5"/><path d="M2 20c0-3.5 3-6 7-6s7 2.5 7 6"/><circle cx="17" cy="9" r="2.5"/><path d="M16 14c3 0 6 1.8 6 5"/></svg> 출석</div>
          <div class="kpi-figure"><span class="kpi-num" id="kpiAttendance">96.4</span><span class="kpi-num-unit">%</span></div>
          <div class="kpi-foot">지난 7일 평균</div>
          <div class="kpi-spark" id="kpiAttendanceSpark"></div>
        </article>

        <article class="kpi-card">
          <div class="kpi-label"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20V8M10 20V4M16 20v-8M22 20H2"/></svg> 평균 성적</div>
          <div class="kpi-figure"><span class="kpi-num" id="kpiScore">82.7</span><span class="kpi-num-unit">점</span></div>
          <div class="kpi-foot up" id="kpiScoreDelta">+3.2 지난달 대비</div>
          <div class="kpi-spark" id="kpiScoreSpark"></div>
        </article>

        <article class="kpi-card">
          <div class="kpi-label"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg> 이번주 수업</div>
          <div class="kpi-figure"><span class="kpi-num" id="kpiClasses">34</span><span class="kpi-num-unit">개</span></div>
          <div class="kpi-foot" id="kpiClassesFoot">오늘 6개 예정</div>
          <div class="kpi-mini-bars" id="kpiClassesBars"></div>
        </article>
      </section>

      <div class="section-title">
        <h3>빠른 작업</h3>
      </div>
      <div class="quick-actions" role="list">
        <button class="quick-card" data-tab="homework" role="listitem">
          <span class="quick-icon warn">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4z"/></svg>
          </span>
          <span class="quick-meta">
            <span class="quick-title">미제출 일괄 알림</span>
            <span class="quick-desc" id="qaMissingDesc">전체 클래스 미제출자에게</span>
          </span>
          <svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>
        </button>
        <button class="quick-card" data-tab="schedule" role="listitem">
          <span class="quick-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 9h18M8 3v4M16 3v4"/></svg>
          </span>
          <span class="quick-meta">
            <span class="quick-title">스케줄 사진 등록</span>
            <span class="quick-desc">AI가 자동 인식</span>
          </span>
          <svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>
        </button>
        <button class="quick-card" data-tab="report" role="listitem">
          <span class="quick-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H6a2 2 0 00-2 2v14a2 2 0 002 2h12a2 2 0 002-2V9z"/><path d="M14 3v6h6"/></svg>
          </span>
          <span class="quick-meta">
            <span class="quick-title">리포트 만들기</span>
            <span class="quick-desc">이번달 자동 생성</span>
          </span>
          <svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>
        </button>
        <button class="quick-card" data-tab="students" role="listitem">
          <span class="quick-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3.5"/><path d="M2 20c0-3.5 3-6 7-6s7 2.5 7 6"/><circle cx="17" cy="9" r="2.5"/><path d="M16 14c3 0 6 1.8 6 5"/></svg>
          </span>
          <span class="quick-meta">
            <span class="quick-title">학생 관리</span>
            <span class="quick-desc" id="qaStudentsDesc">전체 학생 명단</span>
          </span>
          <svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>
        </button>
      </div>

      <div class="dash-2col">
        <div>
          <div class="section-title">
            <div>
              <h3>오늘 마감 숙제</h3>
              <p>미제출자가 있는 클래스부터 확인하세요</p>
            </div>
            <button class="link-btn" data-tab="homework">전체 보기 →</button>
          </div>
          <div class="panel" style="padding:0">
            <div id="todayHomeworkList"></div>
          </div>
        </div>
        <div>
          <div class="section-title">
            <div>
              <h3>최근 활동</h3>
              <p>AI가 정리한 학원 운영 알림</p>
            </div>
          </div>
          <div class="panel" style="padding:0">
            <div id="activityFeedList"></div>
          </div>
        </div>
      </div>

      <!-- hidden mounts: existing JS still updates these to keep state live -->
      <div hidden aria-hidden="true">
        <strong id="incomingCount">0</strong>
        <strong id="missingCount">0</strong>
        <strong id="noPhoneCount">0</strong>
        <strong id="dryRunCount">0</strong>
        <strong id="sentCount">0</strong>
        <strong id="failedCount">0</strong>
        <div id="notificationTable"></div>
        <div id="actionQueue" class="action-list"></div>
        <span id="actionQueueMeta"></span>
        <dl id="settings"></dl>
        <div id="connList" class="conn-list"></div>
        <span id="connSummary"></span>
        <div id="log" class="log"></div>
      </div>
    </section>

    <section id="tab-homework" class="tab-view">
      <header class="page-head">
        <div>
          <div class="page-eyebrow">숙제 관리</div>
          <h2 class="page-title">미제출자에게 알림 보내기</h2>
          <div class="page-sub">특정 코스만, 또는 전체 미제출자에게 한 번에 보낼 수 있어요.</div>
        </div>
        <div class="page-actions">
          <span class="ai-on-pill" title="AI가 문구 추천 · 학원 상담 안내를 자동 적용합니다">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.8 4.6L18 9l-4.2 1.4L12 15l-1.8-4.6L6 9l4.2-1.4z"/></svg>
            AI 문구 추천 ON
          </span>
          <button class="secondary" data-action="refreshMissing" aria-label="새로고침">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 11-3-6.7"/><path d="M21 5v5h-5"/></svg>
            새로고침
          </button>
        </div>
      </header>

      <nav class="hw-mode-segment" id="hwModeSegment" role="tablist" aria-label="발송 범위">
        <button type="button" data-hw-mode="all" class="active" role="tab" aria-selected="true">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4z"/></svg>
          전체 미제출 일괄 발송
          <span class="mode-count" id="hwModeCount">0명</span>
        </button>
        <button type="button" data-hw-mode="single" role="tab" aria-selected="false">특정 코스만</button>
      </nav>

      <div class="bulk-flow">
        <div class="step-card">
          <div class="step-head">
            <div>
              <h3>대상 학생</h3>
              <p id="hwSubjectSub">전체 미제출 학생을 한 번에 선택할 수 있습니다.</p>
            </div>
            <span class="section-subtitle" id="missingResultMeta">대기</span>
          </div>
          <div class="control-grid" id="hwFilterRow" hidden>
            <label class="combobox-label" style="grid-column: 1 / 3;">코스
              <div class="combobox" id="lessonCombobox">
                <input id="lessonComboboxInput" type="search" placeholder="코스명 또는 ID로 검색" autocomplete="off">
                <input id="lessonId" type="hidden">
                <button type="button" class="combobox-clear" id="lessonComboboxClear" aria-label="선택 해제" hidden>×</button>
                <div class="combobox-menu" id="lessonComboboxMenu" hidden role="listbox"></div>
              </div>
            </label>
            <button data-action="refreshMissing" class="secondary">조건으로 검색</button>
          </div>
          <details class="conn-details" id="hwAdvanced" style="margin-bottom:10px;">
            <summary>고급 필터 · 상태별 추리기</summary>
            <div style="margin-top:10px;">
              <div class="control-grid" style="grid-template-columns: minmax(120px, 200px) auto;">
                <label>조회 시간(시간)<input id="windowHours" type="number" min="1" step="1" value="24"></label>
                <button data-action="resetBulkFilters" class="secondary">초기화</button>
              </div>
              <div class="toolbar" id="missingFilters" role="tablist" aria-label="미제출 필터" style="margin-top:10px;">
                <button data-filter="all" class="active">전체<span class="chip-count" data-count="all">0</span></button>
                <button data-filter="needs_message">문구 필요<span class="chip-count" data-count="needs_message">0</span></button>
                <button data-filter="needs_review">검토 필요<span class="chip-count" data-count="needs_review">0</span></button>
                <button data-filter="needs_phone">연락처 없음<span class="chip-count" data-count="needs_phone">0</span></button>
                <button data-filter="needs_retry">실패<span class="chip-count" data-count="needs_retry">0</span></button>
                <button data-filter="repeat">반복<span class="chip-count" data-count="repeat">0</span></button>
                <button data-filter="done">완료<span class="chip-count" data-count="done">0</span></button>
              </div>
            </div>
          </details>
          <div id="missingTable" style="margin-top:8px"></div>
        </div>

        <div class="step-card msg-card">
          <div class="step-head">
            <div>
              <h3>메시지 작성</h3>
              <p>톤을 고르면 문구가 자동 적용됩니다. 필요하면 <button type="button" class="link-btn" data-action="toggleBulkEditor">직접 편집</button>도 가능해요.</p>
            </div>
            <button type="button" class="ai-suggest" data-action="aiSuggestTemplate">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.8 4.6L18 9l-4.2 1.4L12 15l-1.8-4.6L6 9l4.2-1.4z"/></svg>
              AI 추천
            </button>
          </div>
          <div class="tone-chips" id="templatePresets" role="tablist" aria-label="문구 톤">
            <button type="button" data-preset="soft" class="active">부드럽게</button>
            <button type="button" data-preset="firm">단호하게</button>
            <button type="button" data-preset="brief">간결하게</button>
          </div>
          <div class="msg-grid">
            <div class="msg-col">
              <div class="msg-col-label">전송 채널</div>
              <div class="channel-list" id="channelList" role="radiogroup" aria-label="전송 채널">
                <label class="channel-option active" data-channel="classin">
                  <input type="radio" name="bulkChannel" value="classin" checked>
                  <span class="channel-meta">
                    <span class="channel-title">Classin 푸시 알림</span>
                    <span class="channel-desc">앱 사용 학부모에게 즉시</span>
                  </span>
                </label>
                <label class="channel-option disabled" data-channel="kakao">
                  <input type="radio" name="bulkChannel" value="kakao" disabled>
                  <span class="channel-meta">
                    <span class="channel-title">카카오톡 알림톡<span class="channel-pill">미연결</span></span>
                    <span class="channel-desc">카카오 비즈니스 채널 연결이 필요합니다</span>
                  </span>
                  <button type="button" class="channel-connect" data-action="connectChannelKakao">연결하기 →</button>
                </label>
                <label class="channel-option disabled" data-channel="sms">
                  <input type="radio" name="bulkChannel" value="sms" disabled>
                  <span class="channel-meta">
                    <span class="channel-title">SMS<span class="channel-pill">미연결</span></span>
                    <span class="channel-desc">SMS 발신번호 등록이 필요합니다</span>
                  </span>
                  <button type="button" class="channel-connect" data-action="connectChannelSms">연결하기 →</button>
                </label>
              </div>
            </div>
            <div class="msg-col">
              <div class="msg-col-label">미리보기</div>
              <div class="msg-preview" id="msgPreviewBox">
                <div class="preview-body" id="templatePreviewBody">대상 학생이 검색되면 미리보기가 표시됩니다.</div>
              </div>
            </div>
          </div>
          <div class="bulk-editor" id="bulkEditor" hidden>
            <div class="template-tokens" aria-label="변수 삽입">
              <span class="token-label">변수 삽입</span>
              <button type="button" class="token-chip" data-token="{{student_name}}">학생 이름</button>
              <button type="button" class="token-chip" data-token="{{class_name}}">반</button>
              <button type="button" class="token-chip" data-token="{{date}}">날짜</button>
              <button type="button" class="token-chip" data-token="{{missing_count}}">연속 횟수</button>
              <button type="button" class="token-chip" data-token="{{academy_name}}">학원명</button>
            </div>
            <label class="template-label">메시지 (직접 편집)
              <textarea id="bulkTemplate" rows="5" placeholder="안녕하세요 학부모님 😊&#10;[{{student_name}}] 학생이 {{date}} 마감 숙제를 아직 제출하지 않았어요."></textarea>
            </label>
            <div class="template-char-meta"><span id="templateCharMeta">0자</span></div>
          </div>
          <div class="ai-hint-banner" id="aiHintBanner" hidden>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.8 4.6L18 9l-4.2 1.4L12 15l-1.8-4.6L6 9l4.2-1.4z"/></svg>
            <div>연속 미제출 3회 이상 학생 <b id="aiHintRepeatCount">0</b>명에게는 학원 상담 자동 안내가 메시지에 포함됩니다.</div>
            <span class="badge">자동 적용</span>
          </div>
        </div>

        <div class="step-card send-card">
          <div class="step-head">
            <div>
              <h3>단체 발송</h3>
              <p>위에서 작성한 문구로 대상 학생에게 한 번에 발송합니다.</p>
            </div>
            <span class="section-subtitle" id="bulkSendMeta">대기</span>
          </div>
          <div class="send-summary">
            <div class="send-stat"><span>발송 대상</span><strong id="bulkSendCount">0</strong>명</div>
            <div class="send-stat warn"><span>연락처 누락</span><strong id="bulkNoPhone">0</strong>명</div>
            <div class="send-stat ok"><span>최근 발송 완료</span><strong id="bulkRecentSent">0</strong>건</div>
          </div>
          <div class="send-actions">
            <label class="send-toggle"><input type="checkbox" id="bulkDryRun" checked> 미리 보기(dry-run)로 확인</label>
            <button data-action="sweepMissing" id="bulkSendBtn" class="bulk-send-btn">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4z"/></svg>
              <span id="bulkSendBtnLabel">대상 0명에게 단체 발송</span>
            </button>
          </div>
        </div>
      </div>
    </section>

    <section id="tab-schedule" class="tab-view">
      <header class="page-head">
        <div>
          <div class="page-eyebrow">스케줄 관리</div>
          <h2 class="page-title">스케줄표 사진으로 한 번에 등록</h2>
          <div class="page-sub">종이 시간표를 찍어 올리면 AI가 수업·강사·시간을 인식해 ClassIn 일정으로 변환합니다.</div>
        </div>
      </header>

      <div class="panel hero-panel">
        <div class="dropzone" id="scheduleDropzone">
          <div class="dropzone-icon">
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 17l-5-5L5 20"/></svg>
          </div>
          <div class="dropzone-title">스케줄표 이미지를 끌어다 놓으세요</div>
          <div class="dropzone-desc">JPG, PNG, HEIC · 최대 10MB · 종이 시간표 사진도 인식돼요</div>
          <button class="dropzone-btn" data-action="scheduleUpload">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4M6 10l6-6 6 6"/><path d="M4 20h16"/></svg>
            이미지 선택
          </button>
          <div class="dropzone-alt">또는 텍스트로 붙여넣기 · 엑셀(CSV) 업로드</div>
        </div>

        <details class="schedule-paste">
          <summary>텍스트로 붙여넣기</summary>
          <textarea id="schedulePaste" placeholder="월 16:00-17:30 중2 수학 심화 A 박정현 301호&#10;월 19:00-20:30 중3 영어 RC 이수민 302호..."></textarea>
          <div class="actions" style="margin-top:10px">
            <button data-action="parseSchedule">AI로 인식하기</button>
            <button data-action="parseScheduleReset" class="secondary">지우기</button>
          </div>
        </details>

        <div id="scheduleReview" class="schedule-review" hidden></div>

        <div class="schedule-hint">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.8 4.6L18 9l-4.2 1.4L12 15l-1.8-4.6L6 9l4.2-1.4z"/></svg>
          <div>인식 후 <b>요일·시간·강사·교실</b>이 자동 분류되며, 기존 클래스에 매칭됩니다. 확인 단계에서 한 번에 수정할 수 있어요.</div>
          <span class="badge">이미지 OCR — 곧 제공</span>
        </div>
      </div>
    </section>

    <section id="tab-report" class="tab-view">
      <header class="dash-greeting">
        <div>
          <div class="dash-eyebrow">리포트 빌더</div>
          <h1 class="dash-title">학원 브랜드 성적 리포트</h1>
          <div class="dash-sub">Classin 성적 + 학원 자체 데이터로 학부모용 리포트를 생성합니다.</div>
        </div>
        <div class="dash-actions">
          <button class="secondary" data-action="openTemplateModal">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z"/></svg>
            템플릿 설정
          </button>
          <button class="secondary" data-action="renderDaily" id="reportDownloadBtn">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>
            <span id="reportDownloadLabel">PDF 다운로드</span>
          </button>
          <button data-action="generateWeekly">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4z"/></svg>
            학부모에게 발송
          </button>
        </div>
      </header>

      <div class="report-format-bar">
        <div class="report-format-left">
          <div class="report-format-label">출력 형식</div>
          <div class="segmented report-format-seg" id="reportFormatSeg">
            <button data-report-format="pdf" class="active">PDF 리포트</button>
            <button data-report-format="csv">CSV (엑셀)</button>
          </div>
          <span class="report-format-pill" id="reportTemplatePill" hidden>
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>
            <span id="reportTemplateName"></span> 적용됨
          </span>
        </div>
        <div class="report-format-help" id="reportFormatHelp">디자인된 PDF — 학부모 발송에 적합</div>
      </div>

      <div class="report-grid">
        <aside class="report-side">
          <div class="report-side-card">
            <div class="report-side-title">대상</div>
            <div class="report-target">
              <span class="report-target-avatar">김</span>
              <div>
                <div class="report-target-name">김도윤</div>
                <div class="report-target-class">중2 수학 심화 A</div>
              </div>
            </div>
            <div class="report-target-hint">전체 클래스 18명에 동일 양식으로 일괄 생성됩니다.</div>
          </div>

          <div class="report-side-card">
            <div class="report-side-title">포함 데이터</div>
            <ul class="report-data-list">
              <li><label><input type="checkbox" checked><span>Classin 출석 데이터</span><span class="src-pill api">API</span></label></li>
              <li><label><input type="checkbox" checked><span>숙제 제출 이력</span><span class="src-pill api">API</span></label></li>
              <li><label><input type="checkbox" checked><span>OMR 시험 점수</span><span class="src-pill api">API</span></label></li>
              <li><label><input type="checkbox" checked><span>단원평가 (자체)</span><span class="src-pill self">자체</span></label></li>
              <li><label><input type="checkbox" checked><span>강사 코멘트</span><span class="src-pill self">자체</span></label></li>
              <li><label><input type="checkbox"><span>출결 사유</span><span class="src-pill self">자체</span></label></li>
            </ul>
          </div>

          <div class="report-side-card">
            <div class="report-side-title">강사 코멘트</div>
            <div class="report-comment">
              <textarea id="reportComment" placeholder="학생의 강점과 보완할 점을 자유롭게 작성하세요">기본 개념은 안정적입니다. 부등식 영역에서 부호 처리에 실수가 반복돼 다음 주 보충 권장.</textarea>
              <button class="ai-polish-btn" data-action="aiPolishComment" type="button">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.8 4.6L18 9l-4.2 1.4L12 15l-1.8-4.6L6 9l4.2-1.4z"/></svg>
                AI 다듬기
              </button>
            </div>
          </div>
        </aside>

        <section class="report-canvas">
          <div class="report-canvas-head">
            <span>미리보기 · A4 1페이지</span>
            <span class="report-layout-pill">레이아웃: 데이터 카드</span>
          </div>
          <div class="report-canvas-body">
            <div class="report-paper">
              <div class="report-paper-head">
                <div>
                  <div class="report-paper-eyebrow">MONTHLY LEARNING REPORT</div>
                  <div class="report-paper-title">김도윤 학생 학습 리포트</div>
                  <div class="report-paper-meta">중2 수학 심화 A · 2026년 4월 — 2개 단원</div>
                </div>
                <div class="report-paper-brand">
                  <div class="report-paper-brand-name">GreenEdu</div>
                  <div class="report-paper-brand-mark">G</div>
                </div>
              </div>
              <div class="report-paper-body">
                <div class="report-metric-grid">
                  <div class="report-metric"><div class="m-label">출석률</div><div class="m-value">96<span>%</span></div><div class="m-sub">14/15회</div></div>
                  <div class="report-metric"><div class="m-label">숙제 제출</div><div class="m-value">88<span>%</span></div><div class="m-sub">목표 90%</div></div>
                  <div class="report-metric"><div class="m-label">평균 점수</div><div class="m-value">84<span>점</span></div><div class="m-sub">+6점 전월</div></div>
                  <div class="report-metric"><div class="m-label">반 내 순위</div><div class="m-value">상위 22%</div><div class="m-sub">총 18명 중</div></div>
                </div>

                <div class="report-paper-section-title">단원평가 · 시험 성적</div>
                <div class="report-score-bars">
                  <div class="score-bar"><div class="score-head"><span>단원평가 1</span><span><b>72</b> · 반평균 68</span></div><div class="bar-track"><div class="avg-mark" style="left:68%"></div><div class="bar-fill" style="width:72%"></div></div></div>
                  <div class="score-bar"><div class="score-head"><span>OMR 모의 #1</span><span><b>81</b> · 반평균 74</span></div><div class="bar-track"><div class="avg-mark" style="left:74%"></div><div class="bar-fill" style="width:81%"></div></div></div>
                  <div class="score-bar"><div class="score-head"><span>단원평가 2</span><span><b>88</b> · 반평균 71</span></div><div class="bar-track"><div class="avg-mark" style="left:71%"></div><div class="bar-fill" style="width:88%"></div></div></div>
                  <div class="score-bar"><div class="score-head"><span>OMR 모의 #2</span><span><b>84</b> · 반평균 76</span></div><div class="bar-track"><div class="avg-mark" style="left:76%"></div><div class="bar-fill" style="width:84%"></div></div></div>
                  <div class="score-bar"><div class="score-head"><span>종합평가</span><span><b>92</b> · 반평균 78</span></div><div class="bar-track"><div class="avg-mark" style="left:78%"></div><div class="bar-fill" style="width:92%"></div></div></div>
                </div>

                <div class="report-paper-section-title">강점 · 보완</div>
                <div class="report-tags">
                  <div class="tag-col">
                    <div class="tag-col-title good">강점</div>
                    <span class="report-tag good">도형의 성질</span>
                    <span class="report-tag good">방정식 응용</span>
                  </div>
                  <div class="tag-col">
                    <div class="tag-col-title warn">보완 필요</div>
                    <span class="report-tag warn">부등식 영역</span>
                    <span class="report-tag warn">확률</span>
                  </div>
                </div>

                <div class="report-paper-section-title">강사 코멘트</div>
                <div class="report-teacher-note" id="reportPaperComment">기본 개념은 안정적입니다. 부등식 영역에서 부호 처리에 실수가 반복돼 다음 주 보충 권장.</div>
              </div>
            </div>
          </div>
        </section>
      </div>

      <details class="report-advanced">
        <summary>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>
          고급 도구 — 일일/주간 리포트, 시험 import, 메모, AI 질문
        </summary>
        <div class="report-advanced-body">
          <nav class="subtabs" aria-label="고급 도구 탭">
            <button data-subtab="report" class="active">일일/주간</button>
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
      </details>
    </section>

    <section id="tab-students" class="tab-view">
      <header class="dash-greeting">
        <div>
          <div class="dash-eyebrow">학생 관리</div>
          <h1 class="dash-title">전체 학생</h1>
          <div class="dash-sub" id="performanceScope"><span id="studentCountLabel">0명</span> · Classin API + 학원 자체 데이터 통합</div>
        </div>
        <div class="dash-actions">
          <button class="secondary" data-action="toggleStudentAnalytics" id="analyticsToggle">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20V8M10 20V4M16 20v-8M22 20H2"/></svg>
            분석 보기
          </button>
          <button data-action="refreshDashboard">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 11-3-6.7"/><path d="M21 5v5h-5"/></svg>
            새로고침
          </button>
        </div>
      </header>

      <div class="student-toolbar">
        <div class="segmented" id="dashboardMode">
          <button data-mode="course" class="active">코스 보기</button>
          <button data-mode="student">학생 보기</button>
        </div>
        <label class="mode-field" data-mode-field="course-search"><input id="courseSearch" type="search" placeholder="코스 검색..."></label>
        <label class="mode-field" data-mode-field="course-select"><select id="courseSelect"></select></label>
        <label class="mode-field hidden" data-mode-field="student-search"><input id="studentSearch" type="search" placeholder="학생 검색..."></label>
        <label class="mode-field hidden" data-mode-field="student-select"><select id="studentSelect"></select></label>
        <label class="period-field">기간 <select id="dashboardDays">
          <option value="30">30일</option>
          <option value="60">60일</option>
          <option value="90" selected>90일</option>
          <option value="180">180일</option>
          <option value="365">365일</option>
        </select></label>
        <span class="section-subtitle" id="performancePeriod" style="margin-left:auto">최근 90일</span>
      </div>

      <div class="student-table-card">
        <div class="student-table-head">
          <div>학생</div>
          <div>클래스</div>
          <div>출석률</div>
          <div>숙제 제출</div>
          <div>평균</div>
          <div></div>
        </div>
        <div id="studentTable"></div>
      </div>

      <details class="student-analytics" id="studentAnalytics">
        <summary>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20V8M10 20V4M16 20v-8M22 20H2"/></svg>
          심층 분석 — 성적·출석 추이, 집중 관리 학생
        </summary>
        <div class="hero-metrics" style="margin-top:14px">
          <div class="kpi-card-mini"><span>학생</span><strong id="perfStudentCount">0</strong><small id="perfLessonCount">수업 0건</small></div>
          <div class="kpi-card-mini"><span>출석율</span><strong id="perfAttendanceRate">-</strong><small id="perfAttendanceHint">출석+지각 기준</small></div>
          <div class="kpi-card-mini"><span>평균 성적</span><strong id="perfScoreAvg">-</strong><small id="perfScoreDelta">추이 없음</small></div>
          <div class="kpi-card-mini"><span>관리 필요</span><strong id="perfRiskCount">0</strong><small id="perfMissingCount">미제출 0건</small></div>
        </div>
        <div class="chart-grid" style="margin-top:14px">
          <div class="chart-card">
            <div class="chart-head"><h3>성적 추이</h3><span id="scoreTrendMeta">-</span></div>
            <div id="scoreTrendChart" class="chart-box"></div>
          </div>
          <div class="chart-card">
            <div class="chart-head"><h3>출석율 추이</h3><span id="attendanceTrendMeta">-</span></div>
            <div id="attendanceTrendChart" class="chart-box"></div>
          </div>
        </div>
        <div class="attention-panel" style="margin-top:14px">
          <div class="chart-card">
            <div class="chart-head"><h3>집중 관리</h3><span id="attentionMeta">0명</span></div>
            <div id="attentionList" class="attention-list"></div>
          </div>
          <div class="chart-card">
            <div class="chart-head"><h3>상승 학생</h3><span id="moverMeta">0명</span></div>
            <div id="moverList" class="attention-list"></div>
          </div>
        </div>
        <div id="studentCards" class="student-card-grid" style="margin-top:14px"></div>
      </details>
    </section>

    <section id="tab-profile" class="tab-view">
      <header class="page-head">
        <div>
          <div class="page-eyebrow">개인 설정</div>
          <h2 class="page-title">내 ClassIn 자격증명</h2>
          <div class="page-sub">UID·시크릿·연동 키를 로컬에 저장합니다. <code id="profilePath">profile.local.yaml</code></div>
        </div>
        <div class="page-actions">
          <button class="secondary" data-action="loadProfile">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 11-3-6.7"/><path d="M21 5v5h-5"/></svg>
            다시 불러오기
          </button>
          <button data-action="saveProfile" id="saveProfileBtn">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 01-2-2V5a2 2 0 012-2h11l5 5v11a2 2 0 01-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/></svg>
            저장
          </button>
        </div>
      </header>

      <div class="panel hero-panel profile-panel">
        <div class="profile-status" id="profileStatus">불러오는 중...</div>

        <div class="profile-section">
          <div class="profile-section-head">
            <h3>학원 정보</h3>
            <p>리포트·메시지 서명에 사용됩니다.</p>
          </div>
          <div class="profile-grid">
            <label>학원 이름<input id="pf_academy_name" type="text" placeholder="예: 수원 그린에듀 학원"></label>
          </div>
        </div>

        <div class="profile-section">
          <div class="profile-section-head">
            <h3>ClassIn API 자격증명</h3>
            <p>eeo.cn 관리자 콘솔의 SID와 v2 서명 시크릿을 입력하세요.</p>
          </div>
          <div class="profile-grid">
            <label>School ID (UID)<input id="pf_classin_school_id" type="text" placeholder="예: 123456" autocomplete="off"></label>
            <label>Secret Key
              <span class="input-with-toggle">
                <input id="pf_classin_secret_key" type="password" placeholder="저장된 값은 가려져 표시됩니다" autocomplete="off">
                <button type="button" class="input-toggle" data-toggle-target="pf_classin_secret_key" aria-label="비밀 표시 토글">👁</button>
              </span>
            </label>
            <label>Webhook Secret (SafeKey)
              <span class="input-with-toggle">
                <input id="pf_classin_webhook_secret" type="password" placeholder="Datasub SafeKey" autocomplete="off">
                <button type="button" class="input-toggle" data-toggle-target="pf_classin_webhook_secret" aria-label="비밀 표시 토글">👁</button>
              </span>
            </label>
          </div>
        </div>

        <div class="profile-section">
          <div class="profile-section-head">
            <h3>외부 연동 (선택)</h3>
            <p>비워두면 기존 config.yaml 값이 그대로 사용됩니다.</p>
          </div>
          <div class="profile-grid">
            <label>Notion 토큰
              <span class="input-with-toggle">
                <input id="pf_notion_token" type="password" placeholder="secret_..." autocomplete="off">
                <button type="button" class="input-toggle" data-toggle-target="pf_notion_token">👁</button>
              </span>
            </label>
            <label>Anthropic API Key
              <span class="input-with-toggle">
                <input id="pf_anthropic_api_key" type="password" placeholder="sk-ant-..." autocomplete="off">
                <button type="button" class="input-toggle" data-toggle-target="pf_anthropic_api_key">👁</button>
              </span>
            </label>
            <label>알림 모드<select id="pf_notify_mode">
              <option value="dry_run">dry_run (미발송)</option>
              <option value="live">live (실발송)</option>
            </select></label>
            <label>알리고 API Key
              <span class="input-with-toggle">
                <input id="pf_aligo_api_key" type="password" placeholder="알리고 인증키" autocomplete="off">
                <button type="button" class="input-toggle" data-toggle-target="pf_aligo_api_key">👁</button>
              </span>
            </label>
            <label>알리고 User ID<input id="pf_aligo_user_id" type="text" placeholder="알리고 가입 ID" autocomplete="off"></label>
            <label>알리고 발신번호<input id="pf_aligo_sender" type="text" placeholder="01012345678" autocomplete="off"></label>
          </div>
        </div>

        <div class="profile-footnote">
          저장 위치: <code id="profilePathFoot">profile.local.yaml</code> · 비밀 필드는 가려서 표시되며 입력값이 비어 있으면 기존 값을 유지합니다.
        </div>
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
    let hwMode = "all";
    let courseOptions = [];
    let studentOptions = [];
    let optionSearchTimer = null;
    let lessonCourseValue = "";
    let lessonComboboxOpen = false;
    const PROFILE_FIELDS = [
      "academy_name",
      "classin_school_id",
      "classin_secret_key",
      "classin_webhook_secret",
      "notion_token",
      "anthropic_api_key",
      "aligo_api_key",
      "aligo_user_id",
      "aligo_sender",
      "notify_mode",
    ];
    const PROFILE_SECRET_FIELDS = new Set([
      "classin_secret_key",
      "classin_webhook_secret",
      "notion_token",
      "anthropic_api_key",
      "aligo_api_key",
    ]);
    let profileFilled = {{}};

    const ACADEMY_NAME = (status && status.academy) ? status.academy : "Classin++ 학원";
    const BULK_TEMPLATES = {{
      soft: "안녕하세요 학부모님 😊\\n[{{student_name}}] 학생이 오늘 마감 숙제를 아직 제출하지 않았어요.\\n마감 전 한 번만 확인 부탁드려요.\\n\\n- {{academy_name}}",
      firm: "[{{student_name}}] 학생 학부모님께 안내드립니다.\\n{{class_name}} ({{date}}) 숙제 미제출이 확인되었습니다. 최근 {{missing_count}}회 누적되었으니 오늘 안에 제출 확인 부탁드립니다.\\n\\n- {{academy_name}}",
      brief: "[{{student_name}}] {{class_name}} 숙제 미제출. {{date}} 안에 제출 확인 부탁드립니다.\\n- {{academy_name}}",
      custom: "",
    }};
    const AVATAR_PALETTE = ["#6ea8fe", "#f7a8c0", "#f4c270", "#86d6a5", "#bda7f0", "#f29b8a"];
    let bulkPreset = "soft";
    let bulkTemplate = BULK_TEMPLATES.soft;
    let bulkChannel = "classin";
    const bulkSelection = new Map();

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

      renderConnections(status.connections || []);
    }}

    function renderScheduleReview(data) {{
      const target = document.querySelector("#scheduleReview");
      if (!target) return;
      const items = data.items || [];
      if (!items.length) {{
        target.hidden = false;
        target.innerHTML = `<div class="schedule-review-head"><div><strong>인식 결과 없음</strong></div></div><div class="empty">텍스트 형식을 확인하거나 더 많은 줄을 추가해 주세요.</div>`;
        return;
      }}
      const low = items.filter((i) => (i.confidence || 0) < 0.85).length;
      const sourceLabel = data.source === "claude" ? "Claude AI" : "휴리스틱 파서";
      target.hidden = false;
      target.innerHTML = `
        <div class="schedule-review-head">
          <div><strong>${{items.length}}개 수업 인식됨</strong>
            <span class="cell-sub" style="margin-left:8px">소스: ${{escapeHtml(sourceLabel)}}${{low ? ` · 확인 필요 ${{low}}건` : ""}}</span>
          </div>
          <span class="badge ok">검토 후 등록</span>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>요일</th><th>시간</th><th>클래스</th><th>강사</th><th>교실</th><th>신뢰도</th></tr></thead>
            <tbody>
              ${{items.map((it) => {{
                const lowItem = (it.confidence || 0) < 0.85;
                return `<tr>
                  <td>${{escapeHtml(it.day || "-")}}</td>
                  <td>${{escapeHtml(it.time || "-")}}</td>
                  <td>${{escapeHtml(it.class_name || "-")}}</td>
                  <td>${{escapeHtml(it.teacher || "-")}}</td>
                  <td>${{escapeHtml(it.room || "-")}}</td>
                  <td><span class="${{lowItem ? "low" : ""}}">${{Math.round((it.confidence || 0) * 100)}}%</span></td>
                </tr>`;
              }}).join("")}}
            </tbody>
          </table>
        </div>
      `;
    }}

    function renderConnections(items) {{
      const target = document.querySelector("#connList");
      const summary = document.querySelector("#connSummary");
      if (!target) return;
      if (!items.length) {{
        target.innerHTML = `<div class="empty">연결 상태를 확인할 수 없습니다.</div>`;
        if (summary) summary.textContent = "-";
        return;
      }}
      const on = items.filter((i) => i.connected).length;
      if (summary) summary.textContent = `${{on}} / ${{items.length}} 연결됨`;
      target.innerHTML = items.map((item) => `
        <div class="conn-item ${{item.connected ? "on" : "off"}}">
          <span class="conn-led" aria-hidden="true"></span>
          <div class="conn-text">
            <span class="conn-label">${{escapeHtml(item.label)}} <span class="conn-kind">${{escapeHtml(item.kind || "")}}</span></span>
            <span class="conn-detail" title="${{escapeHtml(item.detail || "")}}">${{escapeHtml(item.detail || "")}}</span>
          </div>
          <span class="conn-status">${{item.connected ? "연결됨" : "미연결"}}</span>
        </div>
      `).join("");
    }}

    function escapeHtml(value) {{
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }}

    /* ---- Dashboard helpers (donut / sparkline / mini bars) ---- */
    function svgDonut(value, total, size=92, stroke=10) {{
      const r = (size - stroke) / 2;
      const c = 2 * Math.PI * r;
      const pct = total === 0 ? 0 : Math.min(1, value / total);
      return `<svg width="${{size}}" height="${{size}}" viewBox="0 0 ${{size}} ${{size}}" role="img" aria-label="${{Math.round(pct*100)}}% 제출 완료">
        <circle cx="${{size/2}}" cy="${{size/2}}" r="${{r}}" fill="none" stroke="#eef2eb" stroke-width="${{stroke}}"/>
        <circle cx="${{size/2}}" cy="${{size/2}}" r="${{r}}" fill="none" stroke="var(--green)" stroke-width="${{stroke}}"
          stroke-dasharray="${{(c*pct).toFixed(2)}} ${{c.toFixed(2)}}" stroke-linecap="round"
          transform="rotate(-90 ${{size/2}} ${{size/2}})"/>
        <text x="50%" y="52%" text-anchor="middle" dominant-baseline="middle"
          style="font-size:${{size*0.26}}px; font-weight:700; fill:var(--ink); font-variant-numeric:tabular-nums">${{Math.round(pct*100)}}%</text>
      </svg>`;
    }}

    function svgSparkline(values, opts={{}}) {{
      const w = opts.width || 160, h = opts.height || 32;
      if (!values || !values.length) return "";
      const min = Math.min(...values), max = Math.max(...values);
      const range = max - min || 1;
      const pts = values.map((v, i) => [
        (i / (values.length - 1)) * w,
        h - 4 - ((v - min) / range) * (h - 8),
      ]);
      const d = pts.map((p, i) => `${{i ? "L" : "M"}}${{p[0].toFixed(1)}} ${{p[1].toFixed(1)}}`).join(" ");
      const fillD = `${{d}} L${{w}} ${{h}} L0 ${{h}} Z`;
      return `<svg viewBox="0 0 ${{w}} ${{h}}" preserveAspectRatio="none" aria-hidden="true">
        <path d="${{fillD}}" fill="var(--green-3)" opacity=".7"/>
        <path d="${{d}}" fill="none" stroke="var(--green)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>`;
    }}

    function renderMiniBars(target, values) {{
      const max = Math.max(...values, 1);
      target.innerHTML = values.map((n) => `<div><span style="height:${{Math.round(n/max*100)}}%"></span></div>`).join("");
    }}

    function renderHomeDashboard(missingSummary) {{
      // Bell card: 오늘 숙제 제출 현황
      const totalMissing = missingSummary.total_missing || 0;
      // Pretend "total due today" — we don't have a HW master list yet, so estimate from sent + missing or use a stable default
      const totalDue = Math.max(totalMissing, (missingSummary.sent || 0) + totalMissing + 12);
      const submitted = Math.max(0, totalDue - totalMissing);
      const setText = (sel, v) => {{ const el = document.querySelector(sel); if (el) el.textContent = v; }};
      setText("#hwSubmitted", submitted);
      setText("#hwTotal", ` / ${{totalDue}}`);
      const pill = document.querySelector("#hwMissingPill");
      if (pill) {{
        pill.textContent = totalMissing ? `미제출 ${{totalMissing}}명` : "모두 제출";
        pill.classList.toggle("ok", totalMissing === 0);
        pill.classList.toggle("warn", totalMissing > 0);
      }}
      const bar = document.querySelector("#hwBarFill");
      if (bar) bar.style.width = `${{Math.round((submitted/totalDue)*100)}}%`;
      const donut = document.querySelector("#hwDonut");
      if (donut) donut.innerHTML = svgDonut(submitted, totalDue);

      // Static demo sparklines/bars (real data hookup later)
      const aSpark = document.querySelector("#kpiAttendanceSpark");
      if (aSpark) aSpark.innerHTML = svgSparkline([92, 94, 95, 93, 97, 96, 96.4], {{ width: 160, height: 32 }});
      const sSpark = document.querySelector("#kpiScoreSpark");
      if (sSpark) sSpark.innerHTML = svgSparkline([76, 78, 77, 80, 79, 82, 82.7], {{ width: 160, height: 32 }});
      const cBars = document.querySelector("#kpiClassesBars");
      if (cBars) renderMiniBars(cBars, [6, 5, 7, 4, 6, 4, 2]);

      // Update sub line
      const sub = document.querySelector("#dashSub");
      if (sub) {{
        if (totalMissing) sub.textContent = `오늘 미제출 ${{totalMissing}}명이 있어요. 미제출자에게 한 번에 알림을 보낼 수 있어요.`;
        else sub.textContent = "오늘 미제출 학생이 없습니다. 좋은 하루 보내세요.";
      }}
    }}

    function renderHomeworkToday(items) {{
      const target = document.querySelector("#todayHomeworkList");
      if (!target) return;
      // Group missing rows by lesson to make "today's homework" entries
      const byLesson = new Map();
      (items || []).forEach((it) => {{
        const k = it.lesson_classin_id || it.course_classin_id || "lesson";
        if (!byLesson.has(k)) {{
          byLesson.set(k, {{
            id: k,
            title: it.lesson_title || it.lesson_classin_id || "오늘 과제",
            className: it.class_name || "",
            missing: 0,
            students: new Set(),
          }});
        }}
        const row = byLesson.get(k);
        row.missing += 1;
        row.students.add(it.student_classin_id);
      }});
      const list = Array.from(byLesson.values()).slice(0, 6);
      if (!list.length) {{
        target.innerHTML = `<div class="empty" style="margin:20px">오늘 마감 미제출 항목이 없습니다.</div>`;
        return;
      }}
      target.innerHTML = list.map((row) => {{
        // Assume total per lesson = missing + 12 baseline; submitted = total - missing
        const total = row.missing + 12;
        const submitted = total - row.missing;
        const pct = Math.round((submitted / total) * 100);
        return `
          <button class="hw-today-row" data-tab="homework">
            <div>
              <div class="hw-today-row-title">${{escapeHtml(row.title)}}</div>
              <div class="hw-today-row-meta"><span>${{escapeHtml(row.className || "-")}}</span><span>·</span><span>오늘 마감</span></div>
            </div>
            <div class="hw-today-row-prog">
              <div class="hw-today-row-prog-head">
                <span>제출 ${{submitted}}/${{total}}</span>
                ${{row.missing > 0 ? `<span class="warn">미제출 ${{row.missing}}</span>` : `<span class="ok">완료</span>`}}
              </div>
              <div class="hw-today-row-bar ${{row.missing > 0 ? "warn" : ""}}"><span style="width:${{pct}}%"></span></div>
            </div>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="color:var(--muted-2)"><path d="M9 6l6 6-6 6"/></svg>
          </button>`;
      }}).join("");
    }}

    function syncReportComment() {{
      const ta = document.querySelector("#reportComment");
      const paper = document.querySelector("#reportPaperComment");
      if (ta && paper) paper.textContent = ta.value;
    }}

    function syncReportFormat(value) {{
      document.querySelectorAll("#reportFormatSeg button").forEach((btn) => {{
        btn.classList.toggle("active", btn.dataset.reportFormat === value);
      }});
      const label = document.querySelector("#reportDownloadLabel");
      if (label) label.textContent = value === "csv" ? "CSV 다운로드" : "PDF 다운로드";
      const help = document.querySelector("#reportFormatHelp");
      if (help) help.textContent = value === "csv"
        ? "엑셀로 열기 가능 — 학원 내부 분석용"
        : "디자인된 PDF — 학부모 발송에 적합";
    }}

    function renderActivityFeed(notifications) {{
      const target = document.querySelector("#activityFeedList");
      if (!target) return;
      const items = (notifications || []).slice(0, 6).map((n) => {{
        const kind = n.status === "sent" ? "ok" : n.status === "failed" ? "warn" : "ai";
        const text = n.status === "sent"
          ? `${{n.student_name || "학생"}}에게 알림 발송 완료 (${{n.provider || "Classin"}})`
          : n.status === "failed"
          ? `${{n.student_name || "학생"}} 발송 실패 — 재시도가 필요합니다`
          : `${{n.student_name || "학생"}}의 알림 문구를 AI가 준비했어요`;
        return {{ kind, text, t: formatDate(n.created_at) }};
      }});
      if (!items.length) {{
        items.push(
          {{ kind: "ai", text: "Claude AI가 오늘 미제출자 안내 문구를 준비했어요.", t: "방금" }},
          {{ kind: "ok", text: "demo 모드로 운영 중입니다. 실제 발송은 설정에서 LIVE로 전환하세요.", t: "오늘" }},
        );
      }}
      const icons = {{
        score: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20V8M10 20V4M16 20v-8M22 20H2"/></svg>`,
        warn:  `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4M12 17h.01M10.3 3.86l-8.4 14.6A1.5 1.5 0 003.2 21h17.6a1.5 1.5 0 001.3-2.54l-8.4-14.6a1.5 1.5 0 00-2.6 0z"/></svg>`,
        ai:    `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.8 4.6L18 9l-4.2 1.4L12 15l-1.8-4.6L6 9l4.2-1.4z"/></svg>`,
        ok:    `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>`,
      }};
      target.innerHTML = items.map((a) => `
        <div class="activity-row">
          <span class="activity-icon ${{a.kind}}">${{icons[a.kind] || icons.ai}}</span>
          <div style="flex:1; min-width:0;">
            <div class="activity-row-text">${{escapeHtml(a.text)}}</div>
            <div class="activity-row-time">${{escapeHtml(a.t)}}</div>
          </div>
        </div>
      `).join("");
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
      const courseId = (document.querySelector("#lessonId").value || "").trim();
      if (courseId) params.set("course_id", courseId);

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
      syncLessonComboboxFromState();
    }}

    function lessonOptionLabel(item) {{
      if (!item) return "";
      return item.label || item.course_name || item.course_id || "";
    }}

    function findCourseOption(id) {{
      if (!id) return null;
      return courseOptions.find((opt) => String(opt.course_id) === String(id)) || null;
    }}

    function syncLessonComboboxFromState() {{
      const input = document.querySelector("#lessonComboboxInput");
      const hidden = document.querySelector("#lessonId");
      const clear = document.querySelector("#lessonComboboxClear");
      if (!input || !hidden) return;
      hidden.value = lessonCourseValue;
      const opt = findCourseOption(lessonCourseValue);
      if (lessonCourseValue) {{
        input.value = opt ? lessonOptionLabel(opt) : lessonCourseValue;
        if (clear) clear.hidden = false;
      }} else {{
        if (document.activeElement !== input) input.value = "";
        if (clear) clear.hidden = true;
      }}
    }}

    function setLessonCourse(courseId) {{
      lessonCourseValue = courseId || "";
      lessonComboboxOpen = false;
      const menu = document.querySelector("#lessonComboboxMenu");
      if (menu) menu.hidden = true;
      syncLessonComboboxFromState();
    }}

    function clearLessonCourse() {{
      setLessonCourse("");
    }}

    function renderLessonComboboxMenu(query) {{
      const menu = document.querySelector("#lessonComboboxMenu");
      if (!menu) return;
      const needle = (query || "").trim().toLowerCase();
      const matches = (courseOptions || []).filter((opt) => {{
        if (!needle) return true;
        const hay = `${{opt.course_id || ""}} ${{lessonOptionLabel(opt)}}`.toLowerCase();
        return hay.includes(needle);
      }}).slice(0, 12);
      if (!matches.length) {{
        menu.innerHTML = `<div class="combobox-empty">${{courseOptions.length ? "일치하는 코스가 없습니다." : "아직 동기화된 코스가 없습니다."}}</div>`;
      }} else {{
        menu.innerHTML = matches.map((opt) => {{
          const selected = String(opt.course_id) === String(lessonCourseValue);
          const sub = [opt.lesson_count ? `${{opt.lesson_count}}회 수업` : "", opt.latest_date ? `최근 ${{escapeHtml(formatDate(opt.latest_date))}}` : ""].filter(Boolean).join(" · ");
          return `
            <button type="button" class="combobox-item ${{selected ? "selected" : ""}}" role="option" data-course-id="${{escapeHtml(opt.course_id)}}">
              <span class="combobox-item-title">${{escapeHtml(lessonOptionLabel(opt))}}</span>
              ${{sub ? `<span class="combobox-item-sub">${{sub}}</span>` : ""}}
            </button>`;
        }}).join("");
      }}
      menu.hidden = false;
      lessonComboboxOpen = true;
    }}

    async function loadProfile() {{
      const response = await fetch("/api/profile");
      const data = await response.json();
      if (!response.ok || data.ok === false) {{
        throw new Error(data.detail || "프로필을 불러오지 못했습니다.");
      }}
      applyProfileToForm(data);
      return data;
    }}

    function applyProfileToForm(data) {{
      const fields = data.fields || {{}};
      profileFilled = data.filled || {{}};
      PROFILE_FIELDS.forEach((field) => {{
        const el = document.querySelector(`#pf_${{field}}`);
        if (!el) return;
        const value = fields[field] || "";
        if (PROFILE_SECRET_FIELDS.has(field)) {{
          el.value = "";
          el.placeholder = profileFilled[field] ? `저장된 값: ${{value}} (비워두면 유지)` : el.dataset.placeholderDefault || el.placeholder;
        }} else {{
          el.value = value;
        }}
      }});
      const status = document.querySelector("#profileStatus");
      if (status) {{
        if (data.exists) {{
          status.textContent = `${{data.path}} 에 저장된 값으로 채워졌습니다.`;
          status.className = "profile-status ok";
        }} else {{
          status.textContent = "아직 저장된 프로필이 없습니다. 값을 입력하고 [저장]을 눌러주세요.";
          status.className = "profile-status warn";
        }}
      }}
      const path = document.querySelector("#profilePath");
      const foot = document.querySelector("#profilePathFoot");
      if (path) path.textContent = data.path || "profile.local.yaml";
      if (foot) foot.textContent = data.path || "profile.local.yaml";
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
      renderStudentTable(data.students || []);
      const label = document.querySelector("#studentCountLabel");
      if (label) label.textContent = `${{(data.students || []).length}}명`;
    }}

    function renderStudentTable(items) {{
      const target = document.querySelector("#studentTable");
      if (!target) return;
      if (!items.length) {{
        target.innerHTML = `<div class="student-empty">표시할 학생이 없습니다. 코스를 선택하거나 데이터를 동기화하세요.</div>`;
        return;
      }}
      const palette = ["#dcf2e3","#e0e7ff","#fde4cf","#fce7f3","#dbeafe","#e9d5ff","#fed7aa"];
      const ink     = ["#0b3d20","#3730a3","#92400e","#9d174d","#1e3a8a","#581c87","#9a3412"];
      const hash = (s) => {{ let h = 0; for (const ch of String(s)) h = (h*31 + ch.charCodeAt(0)) & 0xffffffff; return Math.abs(h); }};
      target.innerHTML = items.map((it) => {{
        const name = it.student_name || "미등록";
        const initial = name.slice(0, 1);
        const i = hash(name) % palette.length;
        const att = it.attendance_rate == null ? "-" : `${{Math.round(it.attendance_rate * 100)}}%`;
        const score = it.score_avg == null ? "-" : `${{Number(it.score_avg).toFixed(1)}}점`;
        // Derive a "homework submission" % from missing count (heuristic): 100 - min(100, missing * 12)
        const hwPct = it.homework_submission_rate != null
          ? Math.round(it.homework_submission_rate * 100)
          : Math.max(0, 100 - Math.min(100, (it.homework_missing || 0) * 12));
        const hwColor = hwPct >= 85 ? "var(--green)" : hwPct >= 70 ? "var(--warn)" : "var(--danger)";
        return `
          <button class="student-row" data-student-id="${{escapeHtml(it.student_classin_id || "")}}">
            <span class="student-row-name">
              <span class="student-avatar" style="background:${{palette[i]}}; color:${{ink[i]}}">${{escapeHtml(initial)}}</span>
              <span class="student-name-text">${{escapeHtml(name)}}</span>
            </span>
            <span class="student-class-cell student-class-text">${{escapeHtml(it.class_name || "-")}}</span>
            <span class="student-num">${{att}}</span>
            <span class="hw-cell">
              <span class="hw-bar"><span style="width:${{hwPct}}%; background:${{hwColor}}"></span></span>
              <span class="hw-pct">${{hwPct}}%</span>
            </span>
            <span class="student-num">${{score}}</span>
            <svg class="chev" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>
          </button>`;
      }}).join("");
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
      const modeCount = document.querySelector("#hwModeCount");
      if (modeCount) modeCount.textContent = `${{summary.total_missing || 0}}명`;
      renderHomeDashboard(summary);
      renderHomeworkToday(data.items || []);
      document.querySelector("#noPhoneCount").textContent = summary.no_parent_phone || 0;
      document.querySelector("#dryRunCount").textContent = summary.dry_run || 0;
      document.querySelector("#sentCount").textContent = summary.sent || 0;
      document.querySelector("#failedCount").textContent = summary.failed || 0;

      renderActionQueue(data.items || []);
      updateFilterCounts(data.items || []);
      syncFilterButtons();

      const items = (data.items || []).filter(itemMatchesFilter);
      updateBulkSendUi(items, summary);
      const target = document.querySelector("#missingTable");
      if (!items.length) {{
        target.innerHTML = `<div class="empty">선택한 조건에 해당하는 학생이 없습니다.</div>`;
        return;
      }}
      const allSendable = items.filter((it) => it.has_parent_phone && it.action_required !== "done");
      const allChecked = allSendable.length > 0 && allSendable.every(isItemSelected);

      const renderRow = (item) => {{
        const initial = String(item.student_name || "").slice(0, 1);
        const canSelect = item.has_parent_phone && item.action_required !== "done";
        const checked = canSelect && isItemSelected(item);
        const phoneLine = item.has_parent_phone
          ? `학부모 ${{escapeHtml(maskPhone(item.parent_phone))}}`
          : `<span class="missing-row-warn">연락처 없음</span>`;
        const last = relativeLabel(item.date);
        return `
          <label class="missing-row ${{canSelect ? "" : "missing-row--locked"}}" data-key="${{escapeHtml(itemKey(item))}}">
            <input type="checkbox" class="missing-row-check" ${{checked ? "checked" : ""}} ${{canSelect ? "" : "disabled"}}>
            <span class="missing-row-avatar" style="background:${{avatarBg(item.student_name)}}">${{escapeHtml(initial)}}</span>
            <span class="missing-row-meta">
              <span class="missing-row-name">${{escapeHtml(item.student_name)}}</span>
              <span class="missing-row-sub">${{phoneLine}}</span>
            </span>
            <span class="missing-row-stats">
              <span class="missing-row-last">마지막 제출 ${{escapeHtml(last)}}</span>
              ${{streakBadge(item.missing_count)}}
            </span>
          </label>`;
      }};

      // Group by class when 2+ distinct classes present and not in single mode
      const classes = Array.from(new Set(items.map((i) => i.class_name || "기타")));
      let body;
      if (classes.length >= 2 && hwMode === "all") {{
        const grouped = classes.map((cls) => {{
          const list = items.filter((i) => (i.class_name || "기타") === cls);
          const groupSendable = list.filter((it) => it.has_parent_phone && it.action_required !== "done");
          const groupAllSel = groupSendable.length > 0 && groupSendable.every(isItemSelected);
          return `
            <div class="hw-class-group">
              <div class="hw-class-header">
                <label>
                  <input type="checkbox" class="hw-class-check" data-class="${{escapeHtml(cls)}}" ${{groupAllSel ? "checked" : ""}} ${{groupSendable.length ? "" : "disabled"}}>
                  ${{escapeHtml(cls)}}
                </label>
                <span class="hw-class-meta">${{list.length}}명 미제출</span>
              </div>
              ${{list.map(renderRow).join("")}}
            </div>`;
        }}).join("");
        body = `<div class="missing-list" id="missingList">${{grouped}}</div>`;
      }} else {{
        body = `<div class="missing-list" id="missingList">${{items.map(renderRow).join("")}}</div>`;
      }}

      target.innerHTML = `
        <div class="missing-list-head">
          <label class="select-all">
            <input type="checkbox" id="bulkSelectAll" ${{allChecked ? "checked" : ""}} ${{allSendable.length ? "" : "disabled"}}>
            <span>전체 선택 (${{allSendable.length}}명)</span>
          </label>
          <span class="missing-list-hint">발송 가능한 학생만 체크할 수 있습니다.</span>
        </div>
        ${{body}}`;

      // Update AI hint banner
      const repeatCount = items.filter((i) => (i.missing_count || 0) >= 3).length;
      const hint = document.querySelector("#aiHintBanner");
      const hintCount = document.querySelector("#aiHintRepeatCount");
      if (hint && hintCount) {{
        hint.hidden = repeatCount === 0;
        hintCount.textContent = repeatCount;
      }}
    }}

    function itemMatchesFilter(item) {{
      if (activeMissingFilter === "all") return true;
      if (activeMissingFilter === "repeat") return Boolean(item.is_repeat);
      return item.action_required === activeMissingFilter;
    }}

    function applyTemplate(tpl, item) {{
      const vars = {{
        student_name: (item && item.student_name) || "(학생)",
        class_name: (item && item.class_name) || "(반)",
        lesson_id: (item && item.lesson_classin_id) || "(수업)",
        date: item ? formatDate(item.date) : "(날짜)",
        missing_count: (item && item.missing_count) || 1,
        academy_name: ACADEMY_NAME,
      }};
      return String(tpl || "").replace(/\\u007b(\\w+)\\u007d/g, (m, key) => (
        vars[key] !== undefined ? vars[key] : m
      ));
    }}

    function itemKey(item) {{
      return `${{item.student_classin_id || ""}}::${{item.lesson_classin_id || ""}}::${{item.date || ""}}`;
    }}

    function isItemSelected(item) {{
      if (!item.has_parent_phone || item.action_required === "done") return false;
      const key = itemKey(item);
      if (bulkSelection.has(key)) return bulkSelection.get(key);
      return true;
    }}

    function setItemSelected(item, value) {{
      bulkSelection.set(itemKey(item), Boolean(value));
    }}

    function avatarBg(name) {{
      const text = String(name || "");
      let hash = 0;
      for (let i = 0; i < text.length; i++) {{
        hash = (hash * 31 + text.charCodeAt(i)) >>> 0;
      }}
      return AVATAR_PALETTE[hash % AVATAR_PALETTE.length];
    }}

    function maskPhone(phone) {{
      if (!phone) return "";
      const digits = String(phone).replace(/[^0-9]/g, "");
      if (digits.length < 8) return phone;
      const head = digits.slice(0, 3);
      const mid = digits.slice(3, 7);
      return `${{head}}-${{mid}}-****`;
    }}

    function relativeLabel(value) {{
      if (!value) return "기록 없음";
      const target = new Date(String(value).slice(0, 10));
      if (Number.isNaN(target.getTime())) return formatDate(value);
      const now = new Date();
      const day = 24 * 60 * 60 * 1000;
      const diffDays = Math.floor((Date.UTC(now.getFullYear(), now.getMonth(), now.getDate())
        - Date.UTC(target.getFullYear(), target.getMonth(), target.getDate())) / day);
      if (diffDays <= 0) return "오늘 새벽";
      if (diffDays === 1) return "어제";
      return `${{diffDays}}일 전`;
    }}

    function streakBadge(count) {{
      const n = Number(count) || 0;
      if (n >= 4) return `<span class="streak-badge danger">연속 ${{n}} 회</span>`;
      if (n >= 2) return `<span class="streak-badge warn">연속 ${{n}} 회</span>`;
      return `<span class="streak-badge mute">${{Math.max(n, 1)}}회</span>`;
    }}

    function updateBulkSendUi(filteredItems, summary) {{
      summary = summary || {{}};
      const items = filteredItems || [];
      const eligible = items.filter((item) => (
        item.has_parent_phone && item.action_required !== "done"
      ));
      const selected = eligible.filter(isItemSelected);
      const noPhone = items.filter((item) => !item.has_parent_phone).length;
      const selectedCount = selected.length;

      const setText = (sel, value) => {{ const el = document.querySelector(sel); if (el) el.textContent = value; }};
      setText("#bulkSendCount", selectedCount);
      setText("#bulkNoPhone", noPhone);
      setText("#bulkRecentSent", summary.sent || 0);
      setText("#bulkSendBtnLabel", `${{selectedCount}}명에게 단체 발송`);
      setText("#missingResultMeta", items.length
        ? `검색 ${{items.length}}명 · 선택 ${{selectedCount}}`
        : "결과 없음");
      setText("#bulkSendMeta", selectedCount ? `선택 ${{selectedCount}}명` : "선택된 학생이 없습니다");

      const btn = document.querySelector("#bulkSendBtn");
      if (btn) btn.disabled = selectedCount === 0;

      const previewItem = selected[0] || eligible[0] || items[0] || null;
      const preview = document.querySelector("#templatePreviewBody");
      if (preview) {{
        if (!bulkTemplate.trim()) {{
          preview.textContent = "문구를 입력하면 미리보기가 표시됩니다.";
          preview.classList.add("empty");
        }} else if (!previewItem) {{
          preview.textContent = "대상 학생이 검색되면 미리보기가 표시됩니다.";
          preview.classList.add("empty");
        }} else {{
          preview.textContent = applyTemplate(bulkTemplate, previewItem);
          preview.classList.remove("empty");
        }}
      }}
    }}

    function syncChannelUi() {{
      document.querySelectorAll("#channelList .channel-option").forEach((node) => {{
        const enabled = !node.classList.contains("disabled");
        node.classList.toggle("active", enabled && node.dataset.channel === bulkChannel);
        const input = node.querySelector("input[type=radio]");
        if (input && enabled) input.checked = node.dataset.channel === bulkChannel;
      }});
    }}

    function syncTemplateUi() {{
      const textarea = document.querySelector("#bulkTemplate");
      if (textarea && textarea.value !== bulkTemplate) textarea.value = bulkTemplate;
      const meta = document.querySelector("#templateCharMeta");
      if (meta) meta.textContent = `${{bulkTemplate.length}}자`;
      document.querySelectorAll("#templatePresets button[data-preset]").forEach((btn) => {{
        btn.classList.toggle("active", btn.dataset.preset === bulkPreset);
      }});
    }}

    function applyBulkPreset(preset) {{
      bulkPreset = preset;
      if (preset !== "custom") {{
        bulkTemplate = BULK_TEMPLATES[preset] !== undefined ? BULK_TEMPLATES[preset] : bulkTemplate;
      }}
      syncTemplateUi();
      const items = (missingState.items || []).filter(itemMatchesFilter);
      updateBulkSendUi(items, missingState.summary || {{}});
    }}

    function insertBulkToken(token) {{
      const textarea = document.querySelector("#bulkTemplate");
      if (!textarea) return;
      const start = textarea.selectionStart ?? textarea.value.length;
      const end = textarea.selectionEnd ?? textarea.value.length;
      const value = textarea.value;
      const next = value.slice(0, start) + token + value.slice(end);
      bulkTemplate = next;
      bulkPreset = "custom";
      syncTemplateUi();
      textarea.focus();
      const pos = start + token.length;
      textarea.setSelectionRange(pos, pos);
      const items = (missingState.items || []).filter(itemMatchesFilter);
      updateBulkSendUi(items, missingState.summary || {{}});
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
      renderActivityFeed(items);
      const target = document.querySelector("#notificationTable");
      if (!target) return;
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
        const dryRunEl = document.querySelector("#bulkDryRun");
        const template = (bulkTemplate || "").trim();
        if (!template) {{
          writeLog("문구가 비어 있습니다. 톤을 고르거나 직접 편집에서 작성해 주세요.");
          return;
        }}
        const items = (missingState.items || []).filter(itemMatchesFilter);
        const selectedItems = items.filter((it) => it.has_parent_phone && it.action_required !== "done" && isItemSelected(it));
        if (!selectedItems.length) {{
          writeLog("선택된 학생이 없습니다. 학생 목록에서 체크 후 다시 시도해 주세요.");
          return;
        }}
        const payload = {{
          window_hours: document.querySelector("#windowHours").value,
          course_id: document.querySelector("#lessonId").value,
          template,
          preset: bulkPreset,
          channel: bulkChannel,
          filter: activeMissingFilter,
          dry_run: dryRunEl ? dryRunEl.checked : true,
          recipients: selectedItems.map((it) => ({{
            student_classin_id: it.student_classin_id,
            lesson_classin_id: it.lesson_classin_id,
            date: it.date,
          }})),
        }};
        const data = await callApi("/api/sweep-missing-homework", payload);
        writeLog(data.message, data);
        await loadSituation();
      }},
      async resetBulkFilters() {{
        const win = document.querySelector("#windowHours");
        if (win) win.value = 24;
        clearLessonCourse();
        activeMissingFilter = "all";
        bulkSelection.clear();
        renderMissing(missingState);
      }},
      async loadProfile() {{
        await loadProfile();
      }},
      async saveProfile() {{
        const payload = {{}};
        PROFILE_FIELDS.forEach((field) => {{
          const el = document.querySelector(`#pf_${{field}}`);
          if (!el) return;
          const value = (el.value || "").trim();
          if (PROFILE_SECRET_FIELDS.has(field) && !value) return;
          payload[field] = value;
        }});
        const data = await callApi("/api/profile", payload);
        writeLog(data.message, data);
        if (data.profile) applyProfileToForm(data.profile);
      }},
      async aiSuggestTemplate() {{
        const items = (missingState.items || []).filter(itemMatchesFilter);
        const repeats = items.filter((it) => (it.missing_count || 0) >= 2).length;
        const recommended = repeats >= Math.max(2, Math.ceil(items.length / 2)) ? "firm" : "soft";
        applyBulkPreset(recommended);
        const label = recommended === "firm" ? "단호하게" : "부드럽게";
        writeLog(`AI 추천: 반복 누락 ${{repeats}}건 기준으로 '${{label}}' 톤을 적용했습니다.`);
      }},
      async toggleBulkEditor() {{
        const editor = document.querySelector("#bulkEditor");
        if (!editor) return;
        editor.hidden = !editor.hidden;
        if (!editor.hidden) {{
          const ta = document.querySelector("#bulkTemplate");
          if (ta) ta.focus();
        }}
      }},
      async connectChannelKakao() {{
        writeLog("카카오톡 알림톡 연동은 곧 제공됩니다. 카카오 비즈니스 채널 인증서를 등록해 주세요.");
      }},
      async connectChannelSms() {{
        writeLog("SMS 연동은 곧 제공됩니다. 발신번호 등록 후 활성화됩니다.");
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
      async parseSchedule() {{
        const text = (document.querySelector("#schedulePaste").value || "").trim();
        if (!text) {{
          writeLog("스케줄 텍스트를 먼저 붙여넣어 주세요.");
          return;
        }}
        const data = await callApi("/api/schedule/parse", {{ text }});
        renderScheduleReview(data);
        writeLog(data.message, {{ source: data.source, items: (data.items || []).length }});
      }},
      async parseScheduleReset() {{
        document.querySelector("#schedulePaste").value = "";
        const review = document.querySelector("#scheduleReview");
        if (review) {{ review.hidden = true; review.innerHTML = ""; }}
      }},
      async scheduleUpload() {{
        writeLog("이미지 OCR은 곧 제공됩니다. 우선 '텍스트로 붙여넣기'에서 AI 인식을 시도해 보세요.");
        const details = document.querySelector(".schedule-paste");
        if (details) details.open = true;
      }},
      async refreshDashboard() {{
        const data = await loadDashboard();
        writeLog("성과 대시보드를 갱신했습니다.", data.summary);
      }},
      async toggleStudentAnalytics() {{
        const det = document.querySelector("#studentAnalytics");
        if (det) det.open = !det.open;
      }},
      async openTemplateModal() {{
        writeLog("템플릿 설정 모달은 곧 제공됩니다. 현재는 기본 '데이터 카드' 레이아웃이 적용됩니다.");
      }},
      async aiPolishComment() {{
        const ta = document.querySelector("#reportComment");
        if (!ta) return;
        const original = (ta.value || "").trim();
        if (!original) {{
          writeLog("강사 코멘트를 먼저 입력해 주세요.");
          return;
        }}
        // demo polish (real impl would call /api/agent)
        const polished = original
          .replace(/권장\.?$/, "지도해 주시면 더 좋아질 것 같습니다.")
          .replace(/실수가/, "사소한 실수가")
          .replace(/안정적입니다/, "튼튼하게 잡혀 있습니다");
        ta.value = polished;
        syncReportComment();
        writeLog("AI가 코멘트를 다듬었습니다.", {{ chars: polished.length }});
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
      const reportFmtButton = event.target.closest("button[data-report-format]");
      if (reportFmtButton) {{
        syncReportFormat(reportFmtButton.dataset.reportFormat);
        return;
      }}
      const hwModeButton = event.target.closest("button[data-hw-mode]");
      if (hwModeButton) {{
        hwMode = hwModeButton.dataset.hwMode;
        document.querySelectorAll("#hwModeSegment button").forEach((btn) => {{
          const on = btn.dataset.hwMode === hwMode;
          btn.classList.toggle("active", on);
          btn.setAttribute("aria-selected", on ? "true" : "false");
        }});
        const filterRow = document.querySelector("#hwFilterRow");
        if (filterRow) filterRow.hidden = hwMode !== "single";
        const sub = document.querySelector("#hwSubjectSub");
        if (sub) sub.textContent = hwMode === "all"
          ? "전체 미제출 학생을 한 번에 선택할 수 있습니다."
          : "코스를 선택하면 해당 코스의 미제출자만 표시됩니다.";
        // Clear lesson filter when switching to "all"
        if (hwMode === "all") {{
          const input = document.querySelector("#lessonComboboxInput");
          const hidden = document.querySelector("#lessonId");
          if (input) input.value = "";
          if (hidden) hidden.value = "";
          loadSituation().catch((error) => writeLog(error.message));
        }}
        if (typeof renderMissing === "function") renderMissing(missingState);
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
        const crumbMap = {{
          home: "홈",
          homework: "숙제 미제출 알림",
          schedule: "스케줄 자동 등록",
          report: "리포트 빌더",
          students: "학생",
          profile: "내 설정",
        }};
        document.querySelectorAll(".sidenav button[data-tab]").forEach((item) => {{
          item.classList.toggle("active", item.dataset.tab === targetTab);
        }});
        document.querySelectorAll(".tab-view").forEach((view) => view.classList.toggle("active", view.id === `tab-${{targetTab}}`));
        const crumb = document.querySelector("#crumbCurrent");
        if (crumb) crumb.textContent = crumbMap[targetTab] || "홈";
        if (window.scrollTo) window.scrollTo({{ top: 0, behavior: "smooth" }});
        if (targetTab === "students") {{
          try {{
            await loadDashboard();
          }} catch (error) {{
            writeLog(error.message);
          }}
        }}
        if (targetTab === "profile") {{
          try {{
            await loadProfile();
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

    const lessonComboboxInput = document.querySelector("#lessonComboboxInput");
    const lessonComboboxMenu = document.querySelector("#lessonComboboxMenu");
    const lessonComboboxClear = document.querySelector("#lessonComboboxClear");
    if (lessonComboboxInput) {{
      lessonComboboxInput.addEventListener("focus", () => {{
        renderLessonComboboxMenu(lessonComboboxInput.value);
      }});
      lessonComboboxInput.addEventListener("input", (event) => {{
        renderLessonComboboxMenu(event.target.value);
        clearTimeout(optionSearchTimer);
        optionSearchTimer = setTimeout(() => {{
          loadDashboardOptions("course", event.target.value).then(() => {{
            if (lessonComboboxOpen) renderLessonComboboxMenu(lessonComboboxInput.value);
          }}).catch((error) => writeLog(error.message));
        }}, 220);
      }});
      lessonComboboxInput.addEventListener("blur", () => {{
        setTimeout(() => {{
          const menu = document.querySelector("#lessonComboboxMenu");
          if (menu) menu.hidden = true;
          lessonComboboxOpen = false;
          syncLessonComboboxFromState();
        }}, 150);
      }});
      lessonComboboxInput.addEventListener("keydown", (event) => {{
        if (event.key === "Escape") {{
          event.target.blur();
        }} else if (event.key === "Enter") {{
          event.preventDefault();
          const first = document.querySelector("#lessonComboboxMenu .combobox-item");
          if (first) first.click();
        }}
      }});
    }}
    if (lessonComboboxMenu) {{
      lessonComboboxMenu.addEventListener("mousedown", (event) => {{
        event.preventDefault();
      }});
      lessonComboboxMenu.addEventListener("click", (event) => {{
        const item = event.target.closest(".combobox-item");
        if (!item) return;
        setLessonCourse(item.dataset.courseId);
        const refreshAction = actions.refreshMissing;
        if (refreshAction) refreshAction().catch((error) => writeLog(error.message));
      }});
    }}
    if (lessonComboboxClear) {{
      lessonComboboxClear.addEventListener("click", () => {{
        clearLessonCourse();
        const refreshAction = actions.refreshMissing;
        if (refreshAction) refreshAction().catch((error) => writeLog(error.message));
      }});
    }}

    document.addEventListener("click", (event) => {{
      const toggle = event.target.closest("[data-toggle-target]");
      if (!toggle) return;
      const target = document.querySelector(`#${{toggle.dataset.toggleTarget}}`);
      if (!target) return;
      target.type = target.type === "password" ? "text" : "password";
      toggle.classList.toggle("active", target.type === "text");
    }});

    const templatePresetsEl = document.querySelector("#templatePresets");
    if (templatePresetsEl) {{
      templatePresetsEl.addEventListener("click", (event) => {{
        const btn = event.target.closest("button[data-preset]");
        if (!btn) return;
        applyBulkPreset(btn.dataset.preset);
      }});
    }}
    document.querySelectorAll(".token-chip[data-token]").forEach((chip) => {{
      chip.addEventListener("click", () => insertBulkToken(chip.dataset.token));
    }});
    const bulkTemplateEl = document.querySelector("#bulkTemplate");
    if (bulkTemplateEl) {{
      bulkTemplateEl.value = bulkTemplate;
      bulkTemplateEl.addEventListener("input", (event) => {{
        bulkTemplate = event.target.value;
        bulkPreset = "custom";
        syncTemplateUi();
        const items = (missingState.items || []).filter(itemMatchesFilter);
        updateBulkSendUi(items, missingState.summary || {{}});
      }});
    }}
    const channelListEl = document.querySelector("#channelList");
    if (channelListEl) {{
      channelListEl.addEventListener("click", (event) => {{
        const option = event.target.closest(".channel-option");
        if (!option || option.classList.contains("disabled")) return;
        if (event.target.closest(".channel-connect")) return;
        bulkChannel = option.dataset.channel;
        syncChannelUi();
      }});
    }}
    document.addEventListener("change", (event) => {{
      const row = event.target.closest(".missing-row");
      if (row && event.target.classList.contains("missing-row-check")) {{
        const key = row.dataset.key;
        const all = (missingState.items || []);
        const item = all.find((it) => itemKey(it) === key);
        if (item) setItemSelected(item, event.target.checked);
        renderMissing(missingState);
        return;
      }}
      if (event.target.id === "bulkSelectAll") {{
        const checked = event.target.checked;
        const items = (missingState.items || []).filter(itemMatchesFilter);
        items.forEach((it) => {{
          if (it.has_parent_phone && it.action_required !== "done") setItemSelected(it, checked);
        }});
        renderMissing(missingState);
        return;
      }}
      if (event.target.classList.contains("hw-class-check")) {{
        const cls = event.target.dataset.class;
        const checked = event.target.checked;
        const items = (missingState.items || []).filter(itemMatchesFilter);
        items.forEach((it) => {{
          if ((it.class_name || "기타") === cls && it.has_parent_phone && it.action_required !== "done") {{
            setItemSelected(it, checked);
          }}
        }});
        renderMissing(missingState);
      }}
    }});
    syncTemplateUi();
    syncChannelUi();

    const reportCommentEl = document.querySelector("#reportComment");
    if (reportCommentEl) {{
      reportCommentEl.addEventListener("input", syncReportComment);
      syncReportComment();
    }}

    renderStatus(status);
    syncDashboardMode();
    Promise.all([
      loadDashboardOptions("course", ""),
      loadDashboardOptions("student", ""),
    ]).then(() => {{
      syncLessonComboboxFromState();
      return loadDashboard();
    }}).catch((error) => writeLog(error.message));
    loadSituation().catch((error) => writeLog(error.message));
    syncLessonComboboxFromState();
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
