"""카카오 알림톡 디스패처.

지침(02_guidelines §4, feedback_mvp_scope):
- MVP 단계: 템플릿 심사 2~3주 걸리므로 실제 발송 대신 dry_run 모드로 예시 문구만.
- 실제 발송은 Standard 티어부터 포함.
- 제공자: 알리고 or 솔라피 릴레이.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import AppConfig
from ..intelligence.missing_homework import OutgoingMessage

log = logging.getLogger(__name__)


async def dispatch_kakao(cfg: AppConfig, messages: list[OutgoingMessage]) -> None:
    if cfg.notify.mode == "dry_run":
        _dry_run_dump(cfg, messages)
        return
    if cfg.notify.provider == "aligo":
        _send_via_aligo(cfg, messages)
    else:
        raise NotImplementedError(f"provider not supported yet: {cfg.notify.provider}")


def _dry_run_dump(cfg: AppConfig, messages: list[OutgoingMessage]) -> None:
    out_dir = Path(cfg.reports.output_dir) / "notify_dry_run"
    out_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
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


def _send_via_aligo(cfg: AppConfig, messages: list[OutgoingMessage]) -> None:
    # TODO: 알리고 알림톡 API 연동 — 템플릿 승인 후 활성화
    raise NotImplementedError("aligo live mode not implemented in MVP")
