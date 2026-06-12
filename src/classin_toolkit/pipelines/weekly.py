"""주간 리포트 파이프라인 (MVP2) — HTML 먼저, Notion 아카이브는 승인 후.

흐름:
1. `generate_drafts(cfg)` — 학생별 Claude 리포트 생성 + HTML 드래프트 파일 저장
2. 컨설턴트/원장이 HTML 리뷰 (`reports_out/weekly/<date>_<slug>.html`)
3. `approve_all(cfg, period_start)` — 드래프트 대상 리포트를 Notion 아카이브 + 학부모 문구 최종화

`output.weekly.require_approval == false` 면 generate 시점에 아카이브까지 바로 실행.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import AppConfig
from ..intelligence.weekly_report import build_weekly_report
from ..storage.html_renderer import HtmlWeeklyRenderer
from ..storage.notion_repo import NotionRepo, StudentRecord
from ..storage.output_port import WeeklyRenderInput
from .demo_filter import without_seed_demo_students

log = logging.getLogger(__name__)

DRAFT_INDEX_NAME = "drafts.json"


@dataclass
class DraftRecord:
    student_classin_id: str
    student_name: str
    html_path: str
    public_url: str | None
    period_start: str
    period_end: str
    summary_markdown: str
    parent_message: str
    approved: bool = False
    notion_page_id: str | None = None


def generate_drafts(cfg: AppConfig, *, reference: datetime | None = None) -> int:
    ref = reference or datetime.now(tz=timezone.utc)
    this_start = (ref - timedelta(days=ref.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    this_end = this_start + timedelta(days=6, hours=23, minutes=59)
    prev_start = this_start - timedelta(days=7)
    prev_end = this_end - timedelta(days=7)

    repo = NotionRepo.from_config(cfg)
    renderer = HtmlWeeklyRenderer()
    students = without_seed_demo_students(repo.list_active_students())
    log.info("weekly drafts for %d students (%s ~ %s)", len(students), this_start, this_end)

    drafts: list[DraftRecord] = []
    for student in students:
        try:
            draft = _one_student(
                cfg, repo, renderer, student, this_start, this_end, prev_start, prev_end
            )
            if draft:
                drafts.append(draft)
        except Exception:
            log.exception("weekly draft failed for %s", student.name)

    _write_index(cfg, this_start, drafts)

    if not cfg.output.weekly.require_approval:
        return approve_all(cfg, period_start=this_start)
    return len(drafts)


def approve_all(cfg: AppConfig, *, period_start: datetime) -> int:
    repo = NotionRepo.from_config(cfg)
    index_path = _index_path(cfg, period_start)
    if not index_path.exists():
        log.warning("no draft index at %s", index_path)
        return 0
    raw = json.loads(index_path.read_text(encoding="utf-8"))
    drafts = [DraftRecord(**d) for d in raw]

    approved = 0
    mode = cfg.output.weekly.mode
    for d in drafts:
        if d.approved:
            continue
        if mode in ("notion", "html+notion"):
            student = repo.find_student_by_classin_id(d.student_classin_id)
            if not student:
                continue
            page_id = repo.archive_approved_weekly_report(
                student=student,
                period_start=datetime.fromisoformat(d.period_start),
                period_end=datetime.fromisoformat(d.period_end),
                summary_md=d.summary_markdown,
                parent_message=d.parent_message,
                html_url=d.public_url,
            )
            d.notion_page_id = page_id
        d.approved = True
        approved += 1

    _write_index_records(index_path, drafts)
    log.info("approved %d drafts (period=%s, mode=%s)", approved, period_start.date(), mode)
    return approved


def _one_student(
    cfg: AppConfig,
    repo: NotionRepo,
    renderer: HtmlWeeklyRenderer,
    student: StudentRecord,
    this_start: datetime,
    this_end: datetime,
    prev_start: datetime,
    prev_end: datetime,
) -> DraftRecord | None:
    lessons = repo.weekly_student_stats(
        student_page_id=student.page_id, since=this_start, until=this_end
    )
    if not lessons:
        log.info("skip %s — no lessons this week", student.name)
        return None
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

    inp = WeeklyRenderInput(
        student_classin_id=student.classin_id,
        student_name=student.name,
        class_name=student.class_name,
        period_start=this_start,
        period_end=this_end,
        lessons=lessons,
        prev_week_lessons=prev,
        summary_markdown=report.summary_markdown,
        parent_message=report.parent_message,
    )

    mode = cfg.output.weekly.mode
    html_result = None
    if mode in ("html", "html+notion"):
        html_result = renderer.write_draft(cfg, inp)

    return DraftRecord(
        student_classin_id=student.classin_id,
        student_name=student.name,
        html_path=str(html_result.path) if html_result and html_result.path else "",
        public_url=html_result.public_url if html_result else None,
        period_start=this_start.isoformat(),
        period_end=this_end.isoformat(),
        summary_markdown=report.summary_markdown,
        parent_message=report.parent_message,
    )


def _index_path(cfg: AppConfig, period_start: datetime) -> Path:
    out_dir = Path(cfg.output.weekly.path)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{period_start.date()}_{DRAFT_INDEX_NAME}"


def _write_index(cfg: AppConfig, period_start: datetime, drafts: list[DraftRecord]) -> None:
    _write_index_records(_index_path(cfg, period_start), drafts)


def _write_index_records(path: Path, drafts: list[DraftRecord]) -> None:
    path.write_text(
        json.dumps([asdict(d) for d in drafts], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# backward-compat: 기존 CLI 는 run_weekly_reports 를 호출
def run_weekly_reports(cfg: AppConfig, *, reference: datetime | None = None) -> int:
    return generate_drafts(cfg, reference=reference)
