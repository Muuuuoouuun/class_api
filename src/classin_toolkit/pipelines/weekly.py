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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..intelligence.academy_context import build_report_contexts
from ..intelligence.report_quality import evaluate_weekly_report_quality
from ..intelligence.weekly_report import build_weekly_report
from ..storage.html_renderer import HtmlWeeklyRenderer
from ..storage.notion_repo import NotionRepo, StudentRecord
from ..storage.output_port import WeeklyRenderInput

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
    report_context_summary: str = ""
    quality_status: str = "review"
    quality_score: int = 0
    quality_warnings: list[str] = field(default_factory=list)
    approved: bool = False
    notion_page_id: str | None = None


@dataclass(frozen=True)
class ApprovalResult:
    approved: int
    skipped_blocked_quality: int = 0
    skipped_missing_student: int = 0
    skipped_already_approved: int = 0

    @property
    def skipped(self) -> int:
        return (
            self.skipped_blocked_quality
            + self.skipped_missing_student
            + self.skipped_already_approved
        )


@dataclass(frozen=True)
class DraftListResult:
    period_start: str
    period_end: str
    index_path: str
    exists: bool
    summary: dict[str, int]
    items: list[dict[str, Any]]


def generate_drafts(
    cfg: AppConfig,
    *,
    reference: datetime | None = None,
    class_name: str | None = None,
    student_classin_ids: list[str] | None = None,
) -> int:
    ref = reference or datetime.now(tz=timezone.utc)
    this_start = (ref - timedelta(days=ref.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    this_end = this_start + timedelta(days=6, hours=23, minutes=59)
    prev_start = this_start - timedelta(days=7)
    prev_end = this_end - timedelta(days=7)

    repo = NotionRepo.from_config(cfg)
    renderer = HtmlWeeklyRenderer()
    students = repo.list_active_students()
    if class_name:
        students = [student for student in students if student.class_name == class_name]
    if student_classin_ids is not None:
        selected_ids = set(student_classin_ids)
        students = [student for student in students if student.classin_id in selected_ids]
    report_contexts = build_report_contexts(
        cfg,
        [
            {
                "student_classin_id": student.classin_id,
                "student_name": student.name,
                "student_class_name": student.class_name,
            }
            for student in students
        ],
    ).contexts
    log.info(
        "weekly drafts for %d students (%s ~ %s, class=%s, selected=%s)",
        len(students),
        this_start,
        this_end,
        class_name or "all",
        len(student_classin_ids) if student_classin_ids is not None else "all",
    )

    drafts: list[DraftRecord] = []
    for student in students:
        try:
            draft = _one_student(
                cfg,
                repo,
                renderer,
                student,
                this_start,
                this_end,
                prev_start,
                prev_end,
                report_contexts.get(student.classin_id),
            )
            if draft:
                drafts.append(draft)
        except Exception:
            log.exception("weekly draft failed for %s", student.name)

    _write_index(cfg, this_start, drafts)

    if not cfg.output.weekly.require_approval:
        return approve_all(cfg, period_start=this_start).approved
    return len(drafts)


def approve_all(
    cfg: AppConfig,
    *,
    period_start: datetime,
    force_blocked_quality: bool = False,
) -> ApprovalResult:
    repo = NotionRepo.from_config(cfg)
    index_path = _index_path(cfg, period_start)
    if not index_path.exists():
        log.warning("no draft index at %s", index_path)
        return ApprovalResult(approved=0)
    raw = json.loads(index_path.read_text(encoding="utf-8"))
    drafts = [DraftRecord(**d) for d in raw]

    approved = 0
    skipped_blocked_quality = 0
    skipped_missing_student = 0
    skipped_already_approved = 0
    mode = cfg.output.weekly.mode
    for d in drafts:
        if d.approved:
            skipped_already_approved += 1
            continue
        if d.quality_status == "blocked" and not force_blocked_quality:
            skipped_blocked_quality += 1
            continue
        if mode in ("notion", "html+notion"):
            student = repo.find_student_by_classin_id(d.student_classin_id)
            if not student:
                skipped_missing_student += 1
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
    result = ApprovalResult(
        approved=approved,
        skipped_blocked_quality=skipped_blocked_quality,
        skipped_missing_student=skipped_missing_student,
        skipped_already_approved=skipped_already_approved,
    )
    log.info(
        "approved %d drafts (period=%s, mode=%s, skipped=%d, blocked_quality=%d)",
        result.approved,
        period_start.date(),
        mode,
        result.skipped,
        result.skipped_blocked_quality,
    )
    return result


def list_drafts(cfg: AppConfig, *, period_start: datetime) -> DraftListResult:
    index_path = _index_path(cfg, period_start)
    period_end = period_start + timedelta(days=6, hours=23, minutes=59)
    if not index_path.exists():
        return DraftListResult(
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
            index_path=str(index_path),
            exists=False,
            summary=_draft_summary([]),
            items=[],
        )

    raw = json.loads(index_path.read_text(encoding="utf-8"))
    drafts = [DraftRecord(**d) for d in raw]
    items = [_draft_item(d) for d in drafts]
    items.sort(
        key=lambda item: (
            item["approved"],
            _quality_priority(item["quality_status"]),
            item["student_name"],
            item["student_classin_id"],
        )
    )
    if drafts:
        period_end = datetime.fromisoformat(drafts[0].period_end)
    return DraftListResult(
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
        index_path=str(index_path),
        exists=True,
        summary=_draft_summary(drafts),
        items=items,
    )


def _one_student(
    cfg: AppConfig,
    repo: NotionRepo,
    renderer: HtmlWeeklyRenderer,
    student: StudentRecord,
    this_start: datetime,
    this_end: datetime,
    prev_start: datetime,
    prev_end: datetime,
    report_context: dict[str, Any] | None = None,
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
    exams = repo.student_exam_results(
        student_page_id=student.page_id, since=this_start, until=this_end
    )
    report = build_weekly_report(
        cfg=cfg,
        student=student,
        period_start=this_start,
        period_end=this_end,
        lessons=lessons,
        prev_week_lessons=prev,
        exam_results=exams,
        academy_context=report_context,
    )
    quality = evaluate_weekly_report_quality(
        student_name=student.name,
        summary_markdown=report.summary_markdown,
        parent_message=report.parent_message,
        lessons=lessons,
        exam_results=exams,
        academy_context=report_context,
    ).as_dict()

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
        exam_results=exams,
        report_context=report_context,
        quality=quality,
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
        report_context_summary=(report_context or {}).get("summary", ""),
        quality_status=quality["status"],
        quality_score=quality["score"],
        quality_warnings=quality["warnings"],
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


def _draft_summary(drafts: list[DraftRecord]) -> dict[str, int]:
    total = len(drafts)
    approved = sum(1 for draft in drafts if draft.approved)
    ready = sum(1 for draft in drafts if draft.quality_status == "ready")
    review = sum(1 for draft in drafts if draft.quality_status == "review")
    blocked = sum(1 for draft in drafts if draft.quality_status == "blocked")
    return {
        "total": total,
        "approved": approved,
        "pending": total - approved,
        "ready": ready,
        "review": review,
        "blocked": blocked,
        "ready_unapproved": sum(
            1 for draft in drafts if not draft.approved and draft.quality_status == "ready"
        ),
        "review_unapproved": sum(
            1 for draft in drafts if not draft.approved and draft.quality_status == "review"
        ),
        "blocked_unapproved": sum(
            1 for draft in drafts if not draft.approved and draft.quality_status == "blocked"
        ),
        "with_context": sum(1 for draft in drafts if draft.report_context_summary),
        "with_public_url": sum(1 for draft in drafts if draft.public_url),
    }


def _draft_item(draft: DraftRecord) -> dict[str, Any]:
    item = asdict(draft)
    item["preview_url"] = (
        f"/reports/weekly/{Path(draft.html_path).name}" if draft.html_path else None
    )
    return item


def _quality_priority(status: str) -> int:
    return {"blocked": 0, "review": 1, "ready": 2}.get(status, 3)


# backward-compat: 기존 CLI 는 run_weekly_reports 를 호출
def run_weekly_reports(
    cfg: AppConfig,
    *,
    reference: datetime | None = None,
    class_name: str | None = None,
    student_classin_ids: list[str] | None = None,
) -> int:
    return generate_drafts(
        cfg,
        reference=reference,
        class_name=class_name,
        student_classin_ids=student_classin_ids,
    )
