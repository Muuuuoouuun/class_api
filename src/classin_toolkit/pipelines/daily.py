"""일일 현황 스냅샷 렌더 (Layer 4).

출력 정책:
- `output.daily.mode == "html"` (default): HTML 만.
- `"notion"`: 기존 수업 기록 DB 로 족하므로 별도 Notion 페이지 푸시 금지. no-op + 로그.
- `"both"`: HTML + Notion 일일 메모 페이지 (요약만, 로우레벨 DB 미복제).

참고: 메모리 재설계(2026-04-24) — 매일 바뀌는 데이터를 Notion 에 푸시하는 건 토큰 낭비.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from ..config import AppConfig
from ..storage.html_renderer import HtmlDailyRenderer
from ..storage.notion_repo import NotionRepo
from ..storage.output_port import DailySnapshot, RenderResult

log = logging.getLogger(__name__)


def render_daily(cfg: AppConfig, *, target: date | None = None) -> RenderResult | None:
    target = target or datetime.now().date()
    snap = _build_snapshot(cfg, target)

    mode = cfg.output.daily.mode
    if mode == "notion":
        log.info("daily notion mode — no HTML file generated (raw records already in lessons DB)")
        return None

    result = HtmlDailyRenderer().write(cfg, snap)
    if mode == "both":
        log.info("both mode — HTML written; Notion daily digest push not yet implemented")
    return result


def _build_snapshot(cfg: AppConfig, target: date) -> DailySnapshot:
    repo = NotionRepo.from_config(cfg)
    start = datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    # 수업 기록 DB 를 읽어서 수업 단위로 aggregate
    rows = repo.find_missing_homework(
        since=start - timedelta(hours=1), lesson_id=None
    ) + []
    # 더 일반적인 조회가 필요하면 repo 에 lesson_aggregate 메서드 추가 대상.

    lessons_by_id: dict[str, dict] = {}
    students_lookup = repo.resolve_students(
        [r["student_classin_id"] for r in rows if r.get("student_classin_id")]
    )
    missing_homework = []
    for r in rows:
        cid = r.get("student_classin_id") or ""
        student = students_lookup.get(cid)
        missing_homework.append(
            {
                "student_classin_id": cid,
                "student_name": student.name if student else "",
                "class_name": student.class_name if student else None,
                "lesson_title": r.get("lesson_classin_id"),
            }
        )

    return DailySnapshot(
        date=target.isoformat(),
        academy=cfg.academy.name,
        lessons=list(lessons_by_id.values()),
        missing_homework=missing_homework,
        attendance_rate=_attendance_rate(rows),
        generated_at=datetime.now(),
    )


def _attendance_rate(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    present = sum(1 for r in rows if r.get("attendance") in ("출석", "지각"))
    return present / len(rows)
