"""FastAPI Webhook 수신기 (Layer 1).

- 단일 엔드포인트 `/classin/webhook` (1기관 1개 제약)
- SafeKey 필드 검증 (신뢰 대상일 때만 통과)
- Cmd 디스패처 테이블로 이벤트별 파이프라인 호출
- 원본 JSON 은 항상 dump_dir 에 저장 → 스키마 디버깅·재전송 대응
- 파싱 실패해도 200 반환 (재전송 폭주 방지)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from .classin.signing import verify_webhook_safekey
from .classin.webhook_schemas import (
    AttendanceEvent,
    EndEvent,
    HomeworkScoreEvent,
    HomeworkSubmitEvent,
    parse_event,
)
from .config import AppConfig, load_config
from .pipelines.ingest import (
    ingest_attendance,
    ingest_end_summary,
    ingest_homework_score,
    ingest_homework_submit,
)

log = logging.getLogger(__name__)

Dispatcher = Callable[[object, AppConfig], Awaitable[None]]


def create_app(config: AppConfig | None = None) -> FastAPI:
    cfg = config or load_config()
    dump_dir = Path(cfg.webhook.dump_dir)
    dump_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="ClassIn Toolkit Webhook", version="0.1.0")

    dispatch: dict[str, Dispatcher] = {
        "Attendance": _wrap(ingest_attendance, AttendanceEvent),
        "End": _wrap(ingest_end_summary, EndEvent),
        "HomeworkSubmit": _wrap(ingest_homework_submit, HomeworkSubmitEvent),
        "HomeworkScore": _wrap(ingest_homework_score, HomeworkScoreEvent),
    }

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "school": cfg.academy.name}

    @app.get("/reports/{kind}/{filename}")
    async def serve_report(kind: str, filename: str) -> FileResponse:
        if kind not in ("daily", "weekly"):
            raise HTTPException(status_code=404)
        if "/" in filename or ".." in filename:
            raise HTTPException(status_code=400)
        base = (
            Path(cfg.output.daily.path)
            if kind == "daily"
            else Path(cfg.output.weekly.path)
        )
        path = base / filename
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="text/html; charset=utf-8")

    @app.post("/classin/webhook")
    async def classin_webhook(request: Request) -> dict:
        body = await request.body()
        _dump_raw(dump_dir, body)

        try:
            raw = json.loads(body)
        except json.JSONDecodeError:
            log.exception("non-json webhook body")
            return {"ok": False, "reason": "non-json"}

        if cfg.classin.webhook_secret and not verify_webhook_safekey(
            raw, cfg.classin.webhook_secret
        ):
            log.warning("SafeKey verification failed cmd=%s", raw.get("Cmd"))
            return {"ok": False, "reason": "safekey-mismatch"}

        cmd = raw.get("Cmd") or raw.get("cmd") or ""
        handler = dispatch.get(cmd)
        if not handler:
            log.info("skip unhandled cmd=%s", cmd)
            return {"ok": True, "skipped": cmd}

        try:
            event = parse_event(raw)
        except Exception:
            log.exception("event parse failed cmd=%s", cmd)
            return {"ok": False, "reason": "parse-failed", "cmd": cmd}

        await handler(event, cfg)
        return {"ok": True, "cmd": cmd}

    return app


def _wrap(func, expected_type):
    async def _h(event, cfg):
        if not isinstance(event, expected_type):
            log.warning("skip mismatched type: expected=%s got=%s", expected_type, type(event))
            return
        await func(event, cfg)

    return _h


def _dump_raw(dump_dir: Path, body: bytes) -> None:
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S_%f")
    (dump_dir / f"{stamp}.json").write_bytes(body)


app = create_app  # uvicorn entrypoint: classin_toolkit.webhook_receiver:app (factory)
