"""MVP1 미제출 sweep — 수업 종료 이후 컷오프 시점에 배치 실행.

흐름:
1) Notion 수업 기록 DB 에서 "수업일시 > now - window AND 숙제 제출 != True" 행 조회
2) 학생별로 grouping (같은 학생이 여러 수업에서 미제출일 수 있음 — 현재 demo 에선 수업 단위)
3) Claude 로 학생별 카톡 문구 생성
4) notify dispatcher 로 전송 (MVP 는 dry_run)
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from ..config import AppConfig
from ..intelligence.missing_homework import OutgoingMessage, compose_messages_from_rows
from ..notify.dispatcher import dispatch_kakao, record_notification_history
from ..storage.notion_repo import NotionRepo

log = logging.getLogger(__name__)


def sweep_missing_homework(
    cfg: AppConfig,
    *,
    window_hours: int = 24,
    lesson_id: str | None = None,
    selection_keys: list[str] | None = None,
) -> int:
    messages = preview_missing_homework_messages(
        cfg,
        window_hours=window_hours,
        lesson_id=lesson_id,
        selection_keys=selection_keys,
    )
    if not messages:
        log.info("no missing homework in window")
        return 0

    messages, blocked_messages = _dispatchable_messages(cfg, messages)
    if blocked_messages:
        reason = (
            "message quality not ready for live"
            if cfg.notify.mode == "live"
            else "blocked message quality"
        )
        log.warning(
            "skip %d quality-gated missing-homework messages",
            len(blocked_messages),
        )
        record_notification_history(
            cfg,
            blocked_messages,
            event_type="missing_homework",
            provider="quality_gate",
            status="skipped",
            error=reason,
        )
    if not messages:
        log.info("no dispatchable missing-homework messages after quality gate")
        return 0

    asyncio.run(dispatch_kakao(cfg, messages))
    log.info("dispatched %d missing-homework messages", len(messages))
    return len(messages)


def _dispatchable_messages(
    cfg: AppConfig,
    messages: list[OutgoingMessage],
) -> tuple[list[OutgoingMessage], list[OutgoingMessage]]:
    if cfg.notify.mode == "live":
        dispatchable = [message for message in messages if _is_live_dispatchable(message)]
        blocked = [message for message in messages if not _is_live_dispatchable(message)]
        return dispatchable, blocked

    dispatchable = [message for message in messages if message.quality_status != "blocked"]
    blocked = [message for message in messages if message.quality_status == "blocked"]
    return dispatchable, blocked


def _is_live_dispatchable(message: OutgoingMessage) -> bool:
    return message.quality_status == "ready" and _has_parent_phone(message)


def _has_parent_phone(message: OutgoingMessage) -> bool:
    return bool(str(message.parent_phone or "").strip())


def preview_missing_homework_messages(
    cfg: AppConfig,
    *,
    window_hours: int = 24,
    lesson_id: str | None = None,
    selection_keys: list[str] | None = None,
    repo: NotionRepo | None = None,
) -> list[OutgoingMessage]:
    repo = repo or NotionRepo.from_config(cfg)
    rows = query_missing_homework(cfg, window_hours=window_hours, lesson_id=lesson_id, repo=repo)
    if selection_keys is not None:
        selected = set(selection_keys)
        rows = [row for row in rows if missing_homework_selection_key(row) in selected]
    if not rows:
        return []

    by_student: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_student[row["student_classin_id"]].append(row)

    students_lookup = repo.resolve_students(list(by_student.keys()))
    return compose_messages_from_rows(
        cfg=cfg,
        by_student=by_student,
        students_lookup=students_lookup,
    )


def query_missing_homework(
    cfg: AppConfig,
    *,
    window_hours: int = 24,
    lesson_id: str | None = None,
    repo: NotionRepo | None = None,
) -> list[dict]:
    repo = repo or NotionRepo.from_config(cfg)
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    return repo.find_missing_homework(since=since, lesson_id=lesson_id)


def missing_homework_selection_key(row: dict) -> str:
    return "::".join(
        [
            str(row.get("student_classin_id") or ""),
            str(row.get("lesson_classin_id") or ""),
            str(row.get("date") or ""),
        ]
    )
