"""카카오 알림톡 디스패처.

지침(02_guidelines §4, feedback_mvp_scope):
- MVP 단계: 템플릿 심사 2~3주 걸리므로 실제 발송 대신 dry_run 모드로 예시 문구만.
- 실제 발송은 Standard 티어부터 포함.
- 제공자: 알리고 or 솔라피 릴레이.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..config import AppConfig
from .message import OutgoingMessage

log = logging.getLogger(__name__)

HISTORY_FILE = "notify_history.jsonl"


async def dispatch_kakao(cfg: AppConfig, messages: list[OutgoingMessage]) -> None:
    await dispatch_notifications(cfg, messages, event_type="missing_homework")


async def dispatch_notifications(
    cfg: AppConfig,
    messages: list[OutgoingMessage],
    *,
    event_type: str,
) -> None:
    if not messages:
        return
    if cfg.notify.mode == "dry_run":
        artifact_path = _dry_run_dump(cfg, messages)
        _append_history(
            cfg,
            messages,
            event_type=event_type,
            provider="dry_run",
            status="dry_run",
            artifact_path=artifact_path,
        )
        return
    if cfg.notify.provider == "aligo":
        try:
            _send_via_aligo(cfg, messages)
        except Exception as exc:
            _append_history(
                cfg,
                messages,
                event_type=event_type,
                provider=cfg.notify.provider,
                status="failed",
                error=str(exc),
            )
            raise
        _append_history(
            cfg,
            messages,
            event_type=event_type,
            provider=cfg.notify.provider,
            status="sent",
        )
    else:
        raise NotImplementedError(f"provider not supported yet: {cfg.notify.provider}")


def load_notification_history(cfg: AppConfig, *, limit: int = 100) -> list[dict[str, Any]]:
    path = notification_history_path(cfg)
    if not path.exists():
        return []

    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("skip malformed notification history line in %s", path)
    rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    return rows[:limit]


def notification_history_path(cfg: AppConfig) -> Path:
    return Path(cfg.reports.output_dir) / HISTORY_FILE


def _dry_run_dump(cfg: AppConfig, messages: list[OutgoingMessage]) -> Path:
    out_dir = Path(cfg.reports.output_dir) / "notify_dry_run"
    out_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"{stamp}.md"
    lines = [f"# Dry-run kakao dispatch @ {stamp}", ""]
    for m in messages:
        lines += [
            f"## {m.student_name} ({m.student_classin_id})",
            f"phone: {m.parent_phone or '-'}",
            "",
            m.message or "(empty)",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("kakao dry-run dumped to %s (%d messages)", path, len(messages))
    return path


def _append_history(
    cfg: AppConfig,
    messages: list[OutgoingMessage],
    *,
    event_type: str,
    provider: str,
    status: str,
    artifact_path: Path | None = None,
    error: str | None = None,
) -> None:
    from datetime import datetime, timezone

    path = notification_history_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as f:
        for message in messages:
            row = {
                "created_at": now,
                "event_type": event_type,
                "provider": provider,
                "status": status,
                "student_classin_id": message.student_classin_id,
                "student_name": message.student_name,
                "parent_phone": message.parent_phone,
                "message": message.message,
                "artifact_path": str(artifact_path) if artifact_path else None,
                "error": error,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _send_via_aligo(cfg: AppConfig, messages: list[OutgoingMessage]) -> None:
    # TODO: 알리고 알림톡 API 연동 — 템플릿 승인 후 활성화
    raise NotImplementedError("aligo live mode not implemented in MVP")
