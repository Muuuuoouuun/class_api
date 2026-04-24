"""주간 리포트 파이프라인 (MVP2).

지침(03_plan §2 MVP2): 매주 금요일 실행 → 학생별 Claude 리포트 → Notion 페이지 + 카톡 문구.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..config import AppConfig
from ..intelligence.weekly_report import build_weekly_report
from ..storage.notion_repo import NotionRepo, StudentRecord

log = logging.getLogger(__name__)


def run_weekly_reports(cfg: AppConfig, *, reference: datetime | None = None) -> int:
    ref = reference or datetime.now()
    this_start = (ref - timedelta(days=ref.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    this_end = this_start + timedelta(days=6, hours=23, minutes=59)
    prev_start = this_start - timedelta(days=7)
    prev_end = this_end - timedelta(days=7)

    repo = NotionRepo.from_config(cfg)
    students = repo.list_active_students()
    log.info("weekly reports for %d students (%s ~ %s)", len(students), this_start, this_end)

    count = 0
    for student in students:
        try:
            _generate_one(cfg, repo, student, this_start, this_end, prev_start, prev_end)
            count += 1
        except Exception:
            log.exception("weekly report failed for %s", student.name)
    return count


def _generate_one(
    cfg: AppConfig,
    repo: NotionRepo,
    student: StudentRecord,
    this_start: datetime,
    this_end: datetime,
    prev_start: datetime,
    prev_end: datetime,
) -> None:
    lessons = repo.weekly_student_stats(
        student_page_id=student.page_id, since=this_start, until=this_end
    )
    if not lessons:
        log.info("skip %s — no lessons this week", student.name)
        return
    prev = repo.weekly_student_stats(
        student_page_id=student.page_id, since=prev_start, until=prev_end
    )

    report = build_weekly_report(
        cfg=cfg,
        student=student,
        period_start=this_start,
        period_end=this_end,
        lessons=lessons,
        prev_week_lessons=prev,
    )
    repo.save_weekly_report(
        student=student,
        period_start=this_start,
        period_end=this_end,
        summary_md=report.summary_markdown,
        parent_message=report.parent_message,
    )
