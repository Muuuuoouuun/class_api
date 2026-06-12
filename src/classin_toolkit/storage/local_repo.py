"""Local JSON storage for ClassIn Toolkit runtime data.

The app originally used Notion as the operational datastore. This repository
keeps the same method shape while persisting to one local JSON file, so webhook
ingest, dashboards, reports, and notification sweeps can run without Notion.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..config import AppConfig
from .models import StudentRecord

log = logging.getLogger(__name__)


class LocalRepo:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @classmethod
    def from_config(cls, cfg: AppConfig) -> "LocalRepo":
        return cls(cfg.storage.path)

    # ============== Student ==============

    def find_student_by_classin_id(self, classin_id: str) -> StudentRecord | None:
        classin_id = str(classin_id)
        data = self._load()
        for row in data["students"]:
            if str(row.get("classin_id") or "") == classin_id:
                return _student_from_row(row)
        return None

    def resolve_students(self, classin_ids: Iterable[str]) -> dict[str, StudentRecord]:
        return {
            str(classin_id): student
            for classin_id in classin_ids
            if (student := self.find_student_by_classin_id(str(classin_id))) is not None
        }

    def upsert_student(
        self,
        *,
        classin_id: str,
        name: str,
        parent_phone: str | None = None,
        class_name: str | None = None,
    ) -> StudentRecord:
        classin_id = str(classin_id)
        data = self._load()
        page_id = _student_page_id(classin_id)
        row = _find_student_row(data, classin_id)
        if row is None:
            row = {"page_id": page_id, "classin_id": classin_id}
            data["students"].append(row)
        row["name"] = name or row.get("name") or classin_id
        if parent_phone is not None:
            row["parent_phone"] = parent_phone
        else:
            row.setdefault("parent_phone", "")
        if class_name is not None:
            row["class_name"] = class_name
        else:
            row.setdefault("class_name", "")
        self._save(data)
        return _student_from_row(row)

    def list_active_students(self) -> list[StudentRecord]:
        data = self._load()
        return [_student_from_row(row) for row in data["students"] if row.get("classin_id")]

    # ============== Lesson records ==============

    def upsert_lesson_record(
        self,
        *,
        lesson_id: str,
        course_id: str,
        student_classin_id: str,
        class_start: int | None = None,
        class_end: int | None = None,
        attendance_seconds: int | None = None,
        first_in_time: int | None = None,
        last_out_time: int | None = None,
        student_name: str | None = None,
        class_name: str | None = None,
        parent_phone: str | None = None,
    ) -> str | None:
        data = self._load()
        student = self._ensure_student(
            data,
            student_classin_id=student_classin_id,
            student_name=student_name,
            class_name=class_name,
            parent_phone=parent_phone,
        )
        page_id = _lesson_page_id(lesson_id, student.classin_id)
        row = _find_lesson_row(data, lesson_id, student.classin_id)
        if row is None:
            row = {
                "page_id": page_id,
                "student_page_id": student.page_id,
                "student_classin_id": student.classin_id,
                "lesson_classin_id": str(lesson_id or ""),
                "course_classin_id": str(course_id or ""),
                "homework_submitted": False,
            }
            data["lessons"].append(row)
        if course_id:
            row["course_classin_id"] = str(course_id)
        if class_start is not None:
            row["date"] = _iso(class_start)
        elif not row.get("date"):
            row["date"] = None
        if attendance_seconds is not None:
            row["attendance_seconds"] = int(attendance_seconds)
            row["attendance"] = _attend_label(
                int(attendance_seconds), class_start, class_end, first_in_time
            )
        if last_out_time is not None:
            row["last_out_time"] = _iso(last_out_time)
        self._attach_student_fields(row, student)
        self._save(data)
        return page_id

    def patch_lesson_record(
        self,
        *,
        lesson_id: str,
        student_classin_id: str,
        camera_minutes: float | None = None,
        hand_raise: int | None = None,
        trophy: int | None = None,
        poll: int | None = None,
        homework_submitted: bool | None = None,
        homework_submitted_late: bool | None = None,
        homework_score: float | None = None,
        homework_activity_id: str | None = None,
        page_id: str | None = None,
        student_name: str | None = None,
        class_name: str | None = None,
        parent_phone: str | None = None,
        course_id: str | None = None,
        event_time: int | None = None,
    ) -> str | None:
        data = self._load()
        student = self._ensure_student(
            data,
            student_classin_id=student_classin_id,
            student_name=student_name,
            class_name=class_name,
            parent_phone=parent_phone,
        )
        row = _find_lesson_row_by_page_id(data, page_id) if page_id else None
        row = row or _find_lesson_row(data, lesson_id, student.classin_id)
        if row is None:
            row = {
                "page_id": _lesson_page_id(lesson_id, student.classin_id),
                "student_page_id": student.page_id,
                "student_classin_id": student.classin_id,
                "lesson_classin_id": str(lesson_id or ""),
                "course_classin_id": str(course_id or ""),
                "date": None,
                "homework_submitted": False,
            }
            data["lessons"].append(row)
        if course_id:
            row["course_classin_id"] = str(course_id)
        if event_time is not None and not row.get("date"):
            row["date"] = _iso(event_time)
        if camera_minutes is not None:
            row["camera_minutes"] = round(float(camera_minutes), 1)
        if hand_raise is not None:
            row["hand_raise"] = int(hand_raise)
        if trophy is not None:
            row["trophy"] = int(trophy)
        if poll is not None:
            row["poll"] = int(poll)
        if homework_submitted is not None:
            row["homework_submitted"] = bool(homework_submitted)
        if homework_submitted_late is not None:
            row["homework_late"] = bool(homework_submitted_late)
        if homework_score is not None:
            row["homework_score"] = float(homework_score)
        if homework_activity_id:
            row["homework_activity_id"] = str(homework_activity_id)
        self._attach_student_fields(row, student)
        self._save(data)
        return str(row.get("page_id") or "")

    def find_missing_homework(
        self, *, since: datetime, lesson_id: str | None = None
    ) -> list[dict]:
        rows = []
        for row in self._load()["lessons"]:
            if lesson_id and str(row.get("lesson_classin_id") or "") != str(lesson_id):
                continue
            if row.get("homework_submitted") is not False:
                continue
            if not _within_since(row.get("date"), since):
                continue
            rows.append(dict(row))
        return self._attach_student_metadata(rows)

    def lesson_records(self, *, since: datetime, until: datetime) -> list[dict]:
        rows = [
            dict(row)
            for row in self._load()["lessons"]
            if _within_range(row.get("date"), since, until)
        ]
        rows.sort(key=lambda row: row.get("date") or "")
        return self._attach_student_metadata(rows)

    def weekly_student_stats(
        self, *, student_page_id: str, since: datetime, until: datetime
    ) -> list[dict]:
        rows = [
            dict(row)
            for row in self._load()["lessons"]
            if row.get("student_page_id") == student_page_id
            and _within_range(row.get("date"), since, until)
        ]
        rows.sort(key=lambda row: row.get("date") or "")
        return self._attach_student_metadata(rows)

    # ============== Exam records ==============

    def upsert_exam_result(
        self,
        *,
        student_classin_id: str,
        student: StudentRecord | None = None,
        exam_name: str,
        exam_date: datetime,
        class_name: str | None = None,
        subject: str | None = None,
        attended: bool = True,
        score: float | None = None,
        max_score: float | None = None,
        source: str | None = None,
        external_exam_id: str | None = None,
    ) -> str | None:
        data = self._load()
        student = student or self._ensure_student(
            data, student_classin_id=student_classin_id, student_name=None
        )
        page_id = _exam_page_id(exam_name, exam_date, student.classin_id, subject)
        row = _find_exam_row(data, page_id)
        if row is None:
            row = {"page_id": page_id}
            data["exams"].append(row)
        percent = (
            round(float(score) / float(max_score) * 100, 1)
            if score is not None and max_score not in (None, 0)
            else None
        )
        row.update(
            {
                "student_page_id": student.page_id,
                "student_classin_id": student.classin_id,
                "student_name": student.name,
                "student_class_name": student.class_name,
                "parent_phone": student.parent_phone,
                "exam_name": exam_name,
                "exam_date": exam_date.date().isoformat(),
                "class_name": class_name or student.class_name or "",
                "attended": bool(attended),
                "subject": subject,
                "score": float(score) if score is not None else None,
                "max_score": float(max_score) if max_score is not None else None,
                "percent": percent,
                "source": source,
                "external_exam_id": external_exam_id,
            }
        )
        self._save(data)
        return page_id

    def list_exam_results(
        self,
        *,
        exam_name: str,
        exam_date: datetime,
        class_name: str | None = None,
    ) -> list[dict]:
        target = exam_date.date().isoformat()
        rows = []
        for row in self._load()["exams"]:
            if row.get("exam_name") != exam_name or row.get("exam_date") != target:
                continue
            if class_name and row.get("class_name") != class_name:
                continue
            rows.append(dict(row))
        return self._attach_student_metadata(rows)

    def exam_records(
        self,
        *,
        since: datetime,
        until: datetime,
        class_name: str | None = None,
    ) -> list[dict]:
        rows = []
        for row in self._load()["exams"]:
            if not _within_range(row.get("exam_date"), since, until, date_only=True):
                continue
            if class_name and row.get("class_name") != class_name:
                continue
            rows.append(dict(row))
        rows.sort(key=lambda row: row.get("exam_date") or "")
        return self._attach_student_metadata(rows)

    def find_missing_exam(
        self,
        *,
        exam_name: str,
        exam_date: datetime,
        class_name: str | None = None,
    ) -> list[dict]:
        results = self.list_exam_results(
            exam_name=exam_name,
            exam_date=exam_date,
            class_name=class_name,
        )
        students = self.list_active_students()
        if class_name:
            students = [student for student in students if student.class_name == class_name]
        by_student = {row.get("student_page_id"): row for row in results}
        missing = []
        for student in students:
            existing = by_student.get(student.page_id)
            if existing and existing.get("attended") is True:
                continue
            missing.append(
                {
                    "student_page_id": student.page_id,
                    "student_classin_id": student.classin_id,
                    "student_name": student.name,
                    "student_class_name": student.class_name,
                    "parent_phone": student.parent_phone,
                    "exam_name": exam_name,
                    "exam_date": exam_date.date().isoformat(),
                    "attended": existing.get("attended") if existing else None,
                    "subject": existing.get("subject") if existing else None,
                    "score": existing.get("score") if existing else None,
                    "max_score": existing.get("max_score") if existing else None,
                    "percent": existing.get("percent") if existing else None,
                    "source": existing.get("source") if existing else None,
                }
            )
        return missing

    # ============== Local outputs ==============

    def archive_approved_weekly_report(
        self,
        *,
        student: StudentRecord,
        period_start: datetime,
        period_end: datetime,
        summary_md: str,
        parent_message: str,
        html_url: str | None = None,
    ) -> str:
        data = self._load()
        page_id = f"report:{student.classin_id}:{period_start.date().isoformat()}"
        data["reports"] = [row for row in data["reports"] if row.get("page_id") != page_id]
        data["reports"].append(
            {
                "page_id": page_id,
                "student_page_id": student.page_id,
                "student_classin_id": student.classin_id,
                "student_name": student.name,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "summary_markdown": summary_md,
                "parent_message": parent_message,
                "html_url": html_url,
                "approved": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._save(data)
        return page_id

    def write_memo(
        self, *, student_classin_id: str, text: str, tag: str | None = None
    ) -> str | None:
        data = self._load()
        student = self._ensure_student(
            data, student_classin_id=student_classin_id, student_name=None
        )
        page_id = f"memo:{student.classin_id}:{len(data['memos']) + 1}"
        data["memos"].append(
            {
                "page_id": page_id,
                "student_page_id": student.page_id,
                "student_classin_id": student.classin_id,
                "student_name": student.name,
                "text": text,
                "tag": tag,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._save(data)
        return page_id

    # ============== Internals ==============

    def _ensure_student(
        self,
        data: dict[str, Any],
        *,
        student_classin_id: str,
        student_name: str | None,
        class_name: str | None = None,
        parent_phone: str | None = None,
    ) -> StudentRecord:
        classin_id = str(student_classin_id)
        row = _find_student_row(data, classin_id)
        if row is None:
            row = {
                "page_id": _student_page_id(classin_id),
                "classin_id": classin_id,
                "name": student_name or classin_id,
                "parent_phone": parent_phone or "",
                "class_name": class_name or "",
            }
            data["students"].append(row)
            return _student_from_row(row)
        if student_name and (not row.get("name") or row.get("name") == classin_id):
            row["name"] = student_name
        if class_name and not row.get("class_name"):
            row["class_name"] = class_name
        if parent_phone and not row.get("parent_phone"):
            row["parent_phone"] = parent_phone
        return _student_from_row(row)

    def _attach_student_metadata(self, rows: list[dict]) -> list[dict]:
        data = self._load()
        by_page_id = {row.get("page_id"): _student_from_row(row) for row in data["students"]}
        by_classin_id = {
            str(row.get("classin_id") or ""): _student_from_row(row) for row in data["students"]
        }
        for row in rows:
            student = by_page_id.get(row.get("student_page_id")) or by_classin_id.get(
                str(row.get("student_classin_id") or "")
            )
            if student:
                self._attach_student_fields(row, student)
        return rows

    def _attach_student_fields(self, row: dict[str, Any], student: StudentRecord) -> None:
        row["student_page_id"] = student.page_id
        row["student_classin_id"] = student.classin_id
        row["student_name"] = student.name
        row["student_class_name"] = student.class_name
        row["parent_phone"] = student.parent_phone

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_store()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("local store is malformed; starting with empty store: %s", self.path)
            return _empty_store()
        data = _empty_store()
        for key in data:
            if isinstance(raw.get(key), list):
                data[key] = raw[key]
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _empty_store() -> dict[str, list[dict]]:
    return {"students": [], "lessons": [], "exams": [], "reports": [], "memos": []}


def _student_page_id(classin_id: str) -> str:
    return f"student:{classin_id}"


def _lesson_page_id(lesson_id: str, classin_id: str) -> str:
    return f"lesson:{lesson_id}:{classin_id}"


def _exam_page_id(
    exam_name: str, exam_date: datetime, classin_id: str, subject: str | None
) -> str:
    subject_key = subject or "-"
    return f"exam:{exam_date.date().isoformat()}:{exam_name}:{classin_id}:{subject_key}"


def _find_student_row(data: dict[str, Any], classin_id: str) -> dict | None:
    for row in data["students"]:
        if str(row.get("classin_id") or "") == str(classin_id):
            return row
    return None


def _find_lesson_row(data: dict[str, Any], lesson_id: str, classin_id: str) -> dict | None:
    for row in data["lessons"]:
        if str(row.get("lesson_classin_id") or "") == str(lesson_id) and str(
            row.get("student_classin_id") or ""
        ) == str(classin_id):
            return row
    return None


def _find_lesson_row_by_page_id(data: dict[str, Any], page_id: str | None) -> dict | None:
    if not page_id:
        return None
    for row in data["lessons"]:
        if row.get("page_id") == page_id:
            return row
    return None


def _find_exam_row(data: dict[str, Any], page_id: str) -> dict | None:
    for row in data["exams"]:
        if row.get("page_id") == page_id:
            return row
    return None


def _student_from_row(row: dict[str, Any]) -> StudentRecord:
    classin_id = str(row.get("classin_id") or "")
    return StudentRecord(
        page_id=str(row.get("page_id") or _student_page_id(classin_id)),
        classin_id=classin_id,
        name=str(row.get("name") or classin_id),
        parent_phone=str(row.get("parent_phone") or ""),
        class_name=str(row.get("class_name") or "") or None,
    )


def _iso(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()


def _attend_label(
    seconds: int,
    class_start: int | None,
    class_end: int | None,
    first_in_time: int | None,
) -> str:
    if seconds <= 0:
        return "absent"
    if first_in_time and class_start and (first_in_time - class_start) > 5 * 60:
        return "late"
    if class_start and class_end:
        duration = class_end - class_start
        if duration > 0 and seconds < duration * 0.5:
            return "late"
    return "present"


def _parse_datetime(value: str | None, *, date_only: bool = False) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if date_only and len(text) == 10:
        text = f"{text}T00:00:00+00:00"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _within_since(value: str | None, since: datetime) -> bool:
    parsed = _parse_datetime(value)
    if parsed is None:
        return False
    return parsed >= since.astimezone(timezone.utc)


def _within_range(
    value: str | None,
    since: datetime,
    until: datetime,
    *,
    date_only: bool = False,
) -> bool:
    parsed = _parse_datetime(value, date_only=date_only)
    if parsed is None:
        return False
    return since.astimezone(timezone.utc) <= parsed < until.astimezone(timezone.utc)
