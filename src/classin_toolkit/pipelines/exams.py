"""시험 결과 병합과 시험 미응시 알림 파이프라인."""
from __future__ import annotations

import asyncio
import csv
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import AppConfig
from ..intelligence.missing_exam import compose_messages_from_rows
from ..notify.dispatcher import dispatch_notifications
from ..storage.notion_repo import NotionRepo, StudentRecord

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExamImportRow:
    exam_name: str
    exam_date: datetime
    student_classin_id: str | None = None
    student_name: str | None = None
    class_name: str | None = None
    subject: str | None = None
    score: float | None = None
    max_score: float | None = None
    attended: bool = True
    source: str | None = None
    external_exam_id: str | None = None


@dataclass(frozen=True)
class ExamImportResult:
    total_rows: int
    merged_rows: int
    unresolved_rows: int
    skipped_rows: int
    errors: list[str]
    dry_run: bool = False


def import_exam_results(
    cfg: AppConfig,
    *,
    path: Path,
    exam_name: str | None = None,
    exam_date: str | None = None,
    class_name: str | None = None,
    source: str | None = None,
    dry_run: bool = False,
) -> ExamImportResult:
    rows = load_exam_rows(
        path,
        default_exam_name=exam_name,
        default_exam_date=exam_date,
        default_class_name=class_name,
        default_source=source,
    )
    return merge_exam_results(cfg, rows, dry_run=dry_run)


def load_exam_rows(
    path: Path,
    *,
    default_exam_name: str | None = None,
    default_exam_date: str | None = None,
    default_class_name: str | None = None,
    default_source: str | None = None,
) -> list[ExamImportRow]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            raw_rows = list(csv.DictReader(f))
    elif suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            raw_rows = payload
        elif isinstance(payload, dict) and isinstance(payload.get("rows"), list):
            raw_rows = payload["rows"]
        else:
            raise ValueError("JSON exam import must be a list or {'rows': [...]}")
    else:
        raise ValueError("exam import supports only .csv or .json")

    return [
        _normalize_exam_row(
            raw,
            default_exam_name=default_exam_name,
            default_exam_date=default_exam_date,
            default_class_name=default_class_name,
            default_source=default_source,
        )
        for raw in raw_rows
    ]


def merge_exam_results(
    cfg: AppConfig,
    rows: list[ExamImportRow],
    *,
    repo: NotionRepo | None = None,
    dry_run: bool = False,
) -> ExamImportResult:
    repo = repo or NotionRepo.from_config(cfg)
    active_students = repo.list_active_students()
    students_by_id = {student.classin_id: student for student in active_students}
    students_by_name: dict[str, list[StudentRecord]] = defaultdict(list)
    for student in active_students:
        students_by_name[student.name.strip()].append(student)

    merged_rows = 0
    unresolved_rows = 0
    skipped_rows = 0
    errors: list[str] = []

    for idx, row in enumerate(rows, start=1):
        student = _resolve_student(
            row,
            students_by_id=students_by_id,
            students_by_name=students_by_name,
        )
        if not student:
            unresolved_rows += 1
            ident = row.student_classin_id or row.student_name or "(unknown)"
            errors.append(f"row {idx}: student not found or ambiguous: {ident}")
            continue

        if dry_run:
            merged_rows += 1
            continue

        page_id = repo.upsert_exam_result(
            student_classin_id=student.classin_id,
            student=student,
            exam_name=row.exam_name,
            exam_date=row.exam_date,
            class_name=row.class_name or student.class_name,
            subject=row.subject,
            attended=row.attended,
            score=row.score,
            max_score=row.max_score,
            source=row.source,
            external_exam_id=row.external_exam_id,
        )
        if page_id:
            merged_rows += 1
        else:
            skipped_rows += 1

    return ExamImportResult(
        total_rows=len(rows),
        merged_rows=merged_rows,
        unresolved_rows=unresolved_rows,
        skipped_rows=skipped_rows,
        errors=errors,
        dry_run=dry_run,
    )


def query_missing_exam(
    cfg: AppConfig,
    *,
    exam_name: str,
    exam_date: str,
    class_name: str | None = None,
    repo: NotionRepo | None = None,
) -> list[dict]:
    repo = repo or NotionRepo.from_config(cfg)
    return repo.find_missing_exam(
        exam_name=exam_name,
        exam_date=_parse_exam_date(exam_date),
        class_name=class_name,
    )


def sweep_missing_exam(
    cfg: AppConfig,
    *,
    exam_name: str,
    exam_date: str,
    class_name: str | None = None,
) -> int:
    repo = NotionRepo.from_config(cfg)
    rows = query_missing_exam(
        cfg,
        exam_name=exam_name,
        exam_date=exam_date,
        class_name=class_name,
        repo=repo,
    )
    if not rows:
        log.info("no missing exam rows exam=%s date=%s", exam_name, exam_date)
        return 0

    by_student: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        student_id = row.get("student_classin_id")
        if not student_id:
            continue
        by_student[student_id].append(row)

    students_lookup = repo.resolve_students(list(by_student.keys()))
    messages = compose_messages_from_rows(
        cfg=cfg,
        exam_name=exam_name,
        exam_date=exam_date,
        by_student=by_student,
        students_lookup=students_lookup,
    )
    asyncio.run(dispatch_notifications(cfg, messages, event_type="missing_exam"))
    log.info("dispatched %d missing-exam messages", len(messages))
    return len(messages)


def _normalize_exam_row(
    raw: dict,
    *,
    default_exam_name: str | None,
    default_exam_date: str | None,
    default_class_name: str | None,
    default_source: str | None,
) -> ExamImportRow:
    item = {_normalize_key(k): v for k, v in raw.items()}
    exam_name = _value(item, "exam_name", "시험명") or default_exam_name
    exam_date_raw = _value(item, "exam_date", "시험일") or default_exam_date
    if not exam_name or not exam_date_raw:
        raise ValueError("each exam row requires exam_name and exam_date")

    attended_raw = _value(item, "attended", "응시여부", "응시_여부")
    score = _to_float(_value(item, "score", "원점수"))
    return ExamImportRow(
        exam_name=str(exam_name).strip(),
        exam_date=_parse_exam_date(str(exam_date_raw)),
        student_classin_id=_blank_to_none(
            _value(item, "student_classin_id", "classin_id", "student_id", "uid", "classin_uid")
        ),
        student_name=_blank_to_none(_value(item, "student_name", "name", "student", "학생명")),
        class_name=_blank_to_none(_value(item, "class_name", "class", "반")) or default_class_name,
        subject=_blank_to_none(_value(item, "subject", "과목")),
        score=score,
        max_score=_to_float(_value(item, "max_score", "만점")),
        attended=_to_bool(attended_raw, default=score is not None),
        source=_blank_to_none(_value(item, "source", "데이터출처")) or default_source,
        external_exam_id=_blank_to_none(_value(item, "external_exam_id", "exam_id")),
    )


def _resolve_student(
    row: ExamImportRow,
    *,
    students_by_id: dict[str, StudentRecord],
    students_by_name: dict[str, list[StudentRecord]],
) -> StudentRecord | None:
    if row.student_classin_id:
        return students_by_id.get(row.student_classin_id)

    if not row.student_name:
        return None

    candidates = students_by_name.get(row.student_name.strip(), [])
    if row.class_name:
        narrowed = [student for student in candidates if student.class_name == row.class_name]
        if len(narrowed) == 1:
            return narrowed[0]
        if len(narrowed) > 1:
            return None
    if len(candidates) == 1:
        return candidates[0]
    return None


def _normalize_key(key: object) -> str:
    return str(key).strip().lower().replace(" ", "_")


def _value(item: dict[str, object], *keys: str) -> object | None:
    for key in keys:
        normalized = _normalize_key(key)
        if normalized in item:
            return item[normalized]
    return None


def _blank_to_none(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_float(value: object | None) -> float | None:
    text = _blank_to_none(value)
    if text is None:
        return None
    normalized = text.replace(",", "").strip()
    if normalized.endswith("%"):
        normalized = normalized[:-1].strip()
    if normalized.endswith("점"):
        normalized = normalized[:-1].strip()
    if normalized in {"-", "미응시", "결시", "absent", "n/a"}:
        return None
    return float(normalized)


def _to_bool(value: object | None, *, default: bool) -> bool:
    text = _blank_to_none(value)
    if text is None:
        return default
    lowered = text.lower()
    if lowered in {"1", "true", "t", "yes", "y", "응시", "출석"}:
        return True
    if lowered in {"0", "false", "f", "no", "n", "결시", "미응시"}:
        return False
    return default


def _parse_exam_date(value: str) -> datetime:
    text = value.strip()
    if not text:
        raise ValueError("exam_date is empty")
    candidates = [text]
    if text.endswith("Z"):
        candidates.append(text[:-1] + "+00:00")
    if "/" in text:
        candidates.append(text.replace("/", "-"))
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unsupported exam_date format: {value}")
