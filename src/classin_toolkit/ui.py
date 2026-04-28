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
from .pipelines.exams import import_exam_results, sweep_missing_exam
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
      --bg: #f6f8fb;
      --panel: #ffffff;
      --panel-soft: #f8fafc;
      --line: #d8dee8;
      --line-strong: #b8c2d1;
      --text: #182033;
      --muted: #667085;
      --primary: #2f6f64;
      --primary-strong: #21574f;
      --accent: #a85c19;
      --danger: #a83a2f;
      --ok: #21744f;
      --blue: #1d5f8f;
      --shadow: 0 8px 24px rgba(15, 23, 42, .06);
      --shadow-soft: 0 4px 12px rgba(15, 23, 42, .08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-width: 320px;
      background: var(--bg);
      color: var(--text);
      font-family: "Pretendard Variable", Pretendard, "Noto Sans KR", "Segoe UI", system-ui, sans-serif;
      letter-spacing: 0;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 10;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 16px max(24px, calc((100vw - 1640px) / 2 + 20px));
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, .9);
      backdrop-filter: blur(18px);
    }}
    h1 {{
      margin: 0;
      font-size: 24px;
      line-height: 1.2;
      font-weight: 800;
      letter-spacing: 0;
    }}
    main {{
      width: min(1640px, calc(100vw - 40px));
      margin: 18px auto 40px;
    }}
    .status-strip {{
      display: grid;
      grid-template-columns: repeat(6, minmax(136px, 1fr));
      gap: 10px;
      margin-bottom: 16px;
    }}
    .metric, .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }}
    .metric {{
      position: relative;
      overflow: hidden;
      padding: 12px 14px;
      min-height: 78px;
    }}
    .metric span {{
      display: block;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.4;
      font-weight: 650;
    }}
    .metric strong {{
      display: block;
      margin-top: 6px;
      font-size: 30px;
      line-height: 1.1;
      letter-spacing: 0;
    }}
    .metric.warn strong {{ color: var(--accent); }}
    .metric.alert strong {{ color: var(--danger); }}
    .grid {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(320px, 380px);
      gap: 18px;
      align-items: start;
    }}
    aside {{
      position: sticky;
      top: 96px;
      align-self: start;
    }}
    .panel {{
      padding: 18px;
    }}
    .panel + .panel {{
      margin-top: 14px;
    }}
    .hero-panel {{
      padding: 18px;
    }}
    .panel-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 14px;
    }}
    .panel-head p {{
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.35;
    }}
    h2 {{
      margin: 0;
      font-size: 20px;
      line-height: 1.2;
      letter-spacing: 0;
    }}
    .section-subtitle {{
      color: var(--muted);
      font-size: 13px;
      font-weight: 650;
      white-space: nowrap;
    }}
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
      margin: 0 0 12px;
    }}
    .toolbar button {{
      min-height: 34px;
      border-color: var(--line);
      background: rgba(255, 255, 255, .72);
      color: var(--text);
      padding: 6px 10px;
      font-size: 13px;
      font-weight: 750;
    }}
    .toolbar button.active {{
      border-color: var(--primary);
      background: #e6f1ee;
      color: var(--primary-strong);
      box-shadow: inset 0 0 0 1px rgba(47, 111, 100, .18);
    }}
    label {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.3;
      font-weight: 650;
    }}
    input, textarea {{
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      color: var(--text);
      background: rgba(255, 255, 255, .76);
      font: inherit;
      font-size: 14px;
      outline: none;
      transition: border-color .16s ease, box-shadow .16s ease, background .16s ease;
    }}
    input:focus, textarea:focus {{
      border-color: rgba(47, 111, 100, .72);
      box-shadow: 0 0 0 4px rgba(47, 111, 100, .12);
      background: #fff;
    }}
    textarea {{
      min-height: 92px;
      resize: vertical;
    }}
    button {{
      min-height: 38px;
      border: 1px solid var(--primary);
      border-radius: 8px;
      padding: 8px 12px;
      background: var(--primary);
      color: #fff;
      font: inherit;
      font-weight: 800;
      cursor: pointer;
      transition: transform .14s ease, background .14s ease, box-shadow .14s ease;
    }}
    button.secondary {{
      border-color: var(--line);
      background: rgba(255, 255, 255, .78);
      color: var(--text);
    }}
    button:hover {{
      background: var(--primary-strong);
      transform: translateY(-1px);
      box-shadow: var(--shadow-soft);
    }}
    button.secondary:hover {{
      background: #fff;
    }}
    button:disabled {{
      cursor: wait;
      opacity: .62;
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
      min-height: 180px;
      max-height: 360px;
      overflow: auto;
      border-radius: 8px;
      border: 1px solid #233047;
      background: #121826;
      color: #dbe4f0;
      padding: 12px;
      font: 12px/1.55 Consolas, "Cascadia Mono", monospace;
      white-space: pre-wrap;
    }}
    .table-wrap {{
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
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
      padding: 9px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      position: sticky;
      top: 0;
      z-index: 1;
      background: #f8fafc;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0;
      white-space: nowrap;
    }}
    tr:hover td {{
      background: rgba(47, 111, 100, .035);
    }}
    tr:last-child td {{ border-bottom: 0; }}
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
      padding: 18px;
      color: var(--muted);
      text-align: center;
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, .55);
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
      display: grid;
      gap: 4px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, .62);
      font-size: 13px;
      line-height: 1.34;
    }}
    .action-item strong {{ font-size: 14px; }}
    .action-item span {{ color: var(--muted); }}
    .action-item.needs_phone {{ border-color: rgba(180, 35, 24, .35); background: #fff8f7; }}
    .action-item.needs_message {{ border-color: #f3d2a1; background: #fffaf1; }}
    .action-item.needs_review {{ border-color: #b7dcf3; background: #f3faff; }}
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
    @media (max-width: 1280px) {{
      main {{
        width: min(100vw - 32px, 1120px);
      }}
      .status-strip {{
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }}
      .grid {{
        grid-template-columns: 1fr;
      }}
      aside {{
        position: static;
      }}
    }}
    @media (max-width: 760px) {{
      header {{
        align-items: flex-start;
        flex-direction: column;
        padding: 16px 18px;
      }}
      .status-strip, .grid, .actions, .control-grid {{
        grid-template-columns: 1fr;
      }}
      main {{
        width: min(100vw - 20px, 720px);
        margin-top: 12px;
      }}
      .panel, .hero-panel {{
        padding: 14px;
        border-radius: 8px;
      }}
      .panel-head {{
        display: grid;
        gap: 6px;
      }}
      table {{
        min-width: 820px;
      }}
      th, td {{
        padding: 9px;
      }}
      dl {{
        grid-template-columns: 1fr;
        gap: 3px;
      }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <span id="configBadge" class="badge">config</span>
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

    <section class="grid">
      <div>
        <div class="panel hero-panel">
          <div class="panel-head">
            <div>
              <h2>오늘 미제출 상황</h2>
              <p>반복 누락, 연락처 보완, 발송 실패를 먼저 볼 수 있게 정리했습니다.</p>
            </div>
            <span class="section-subtitle">운영 큐</span>
          </div>
          <div class="control-grid">
            <label>조회 시간<input id="windowHours" type="number" min="1" step="1" value="24"></label>
            <label>수업 ID<input id="lessonId" type="text"></label>
            <button data-action="refreshMissing" class="secondary">목록 새로고침</button>
            <button data-action="sweepMissing">미제출 sweep</button>
          </div>
          <div class="toolbar" id="missingFilters">
            <button data-filter="all" class="active">전체</button>
            <button data-filter="needs_message">문구 필요</button>
            <button data-filter="needs_review">검토 필요</button>
            <button data-filter="needs_phone">연락처 없음</button>
            <button data-filter="needs_retry">실패</button>
            <button data-filter="repeat">반복</button>
            <button data-filter="done">완료</button>
          </div>
          <div id="missingTable" style="margin-top:8px"></div>
        </div>

        <div class="panel">
          <h2>알림 발송 현황</h2>
          <div id="notificationTable"></div>
        </div>

        <div class="panel">
          <h2>리포트</h2>
          <div class="actions">
            <div class="row">
              <label>일자<input id="dailyDate" type="date" value="{today}"></label>
              <button data-action="renderDaily">생성</button>
            </div>
            <div class="row">
              <label>주 시작일<input id="weekDate" type="date" value="{week_start}"></label>
              <button data-action="approveWeekly" class="secondary">승인</button>
            </div>
            <button data-action="generateWeekly">주간 드래프트 생성</button>
            <button data-action="refreshStatus" class="secondary">상태 새로고침</button>
          </div>
        </div>

        <div class="panel">
          <h2>시험</h2>
          <div class="stack">
            <div class="actions">
              <label>CSV/JSON path<input id="examPath" type="text" placeholder="samples/exam_results_sample.csv"></label>
              <label>시험명<input id="examName" type="text" placeholder="4월 월말평가"></label>
              <label>시험일<input id="examDate" type="date" value="{today}"></label>
              <label>반<input id="examClassName" type="text"></label>
              <label>출처<input id="examSource" type="text" value="academy-db"></label>
              <label><input id="examDryRun" type="checkbox" checked> dry-run</label>
              <button data-action="importExam" class="secondary">시험 import</button>
              <button data-action="sweepMissingExam">미응시 sweep</button>
            </div>
          </div>
        </div>

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

      <aside>
        <div class="panel">
          <h2>다음 액션</h2>
          <div id="actionQueue" class="action-list"></div>
        </div>
        <div class="panel">
          <h2>설정</h2>
          <dl id="settings"></dl>
        </div>
        <div class="panel">
          <h2>실행 로그</h2>
          <div id="log" class="log"></div>
        </div>
      </aside>
    </section>
  </main>

  <script id="initial-status" type="application/json">{status_json}</script>
  <script>
    const log = document.querySelector("#log");
    const statusNode = document.querySelector("#initial-status");
    let status = JSON.parse(statusNode.textContent);
    let missingState = {{ summary: {{}}, items: [] }};
    let activeMissingFilter = "all";

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

    function renderMissing(data) {{
      missingState = data;
      const summary = data.summary || {{}};
      document.querySelector("#missingCount").textContent = summary.total_missing || 0;
      document.querySelector("#noPhoneCount").textContent = summary.no_parent_phone || 0;
      document.querySelector("#dryRunCount").textContent = summary.dry_run || 0;
      document.querySelector("#sentCount").textContent = summary.sent || 0;
      document.querySelector("#failedCount").textContent = summary.failed || 0;

      renderActionQueue(data.items || []);
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

    function renderActionQueue(items) {{
      const target = document.querySelector("#actionQueue");
      const queue = items
        .filter((item) => item.action_required !== "done")
        .sort((a, b) => actionRank(a) - actionRank(b))
        .slice(0, 6);
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
        const data = await callApi("/api/import-exam-results", {{
          path: document.querySelector("#examPath").value,
          exam_name: document.querySelector("#examName").value,
          exam_date: document.querySelector("#examDate").value,
          class_name: document.querySelector("#examClassName").value,
          source: document.querySelector("#examSource").value,
          dry_run: document.querySelector("#examDryRun").checked,
        }});
        writeLog(data.message, data);
      }},
      async sweepMissingExam() {{
        const data = await callApi("/api/sweep-missing-exam", {{
          exam_name: document.querySelector("#examName").value,
          exam_date: document.querySelector("#examDate").value,
          class_name: document.querySelector("#examClassName").value,
        }});
        writeLog(data.message, data);
        await loadSituation();
      }},
      async refreshMissing() {{
        await loadSituation();
        writeLog("미제출/알림 상황을 갱신했습니다.");
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

    renderStatus(status);
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
