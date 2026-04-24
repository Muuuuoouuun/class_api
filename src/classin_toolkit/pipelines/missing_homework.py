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
from ..notify.dispatcher import dispatch_kakao
from ..storage.notion_repo import NotionRepo

log = logging.getLogger(__name__)


def sweep_missing_homework(
    cfg: AppConfig, *, window_hours: int = 24, lesson_id: str | None = None
) -> int:
    repo = NotionRepo.from_config(cfg)
    rows = query_missing_homework(cfg, window_hours=window_hours, lesson_id=lesson_id, repo=repo)
    if not rows:
        log.info("no missing homework in window")
        return 0

    by_student: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_student[r["student_classin_id"]].append(r)

    students_lookup = repo.resolve_students(list(by_student.keys()))
    messages: list[OutgoingMessage] = compose_messages_from_rows(
        cfg=cfg, by_student=by_student, students_lookup=students_lookup
    )

    asyncio.run(dispatch_kakao(cfg, messages))
    log.info("dispatched %d missing-homework messages", len(messages))
    return len(messages)


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
