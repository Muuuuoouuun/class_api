"""Demo persona data seeding.

The demo seed creates the five personas from docs/15_demo_scenario.md in the
real Notion schema, so the local UI, daily snapshot, missing-homework sweep, and
weekly drafts all have realistic data to work with.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from ..config import AppConfig
from ..storage.notion_repo import NotionRepo

KST = timezone(timedelta(hours=9), "KST")
DEMO_COURSE_ID = "DEMO-COURSE-G2A"
DEMO_ACTIVITY_ID = "DEMO-HW-001"


@dataclass(frozen=True)
class DemoStudent:
    classin_id: str
    name: str
    class_name: str
    parent_phone: str
    persona: str


@dataclass(frozen=True)
class DemoLessonRow:
    lesson_id: str
    course_id: str
    student: DemoStudent
    start_at: datetime
    end_at: datetime
    attendance_seconds: int
    first_in_at: datetime | None
    last_out_at: datetime | None
    hand_raise: int
    trophy: int
    camera_minutes: float
    poll: int
    homework_submitted: bool
    homework_late: bool
    homework_score: float | None


@dataclass(frozen=True)
class DemoDataset:
    students: tuple[DemoStudent, ...]
    lesson_rows: tuple[DemoLessonRow, ...]


@dataclass(frozen=True)
class DemoSeedResult:
    students: int
    lesson_rows: int
    dry_run: bool


DEMO_STUDENTS: tuple[DemoStudent, ...] = (
    DemoStudent("10001", "박성실", "고2-A", "010-0000-0001", "성실형"),
    DemoStudent("10002", "김지각", "고2-A", "010-0000-0002", "지각형"),
    DemoStudent("10003", "이하락", "고2-A", "010-0000-0003", "참여도 하락형"),
    DemoStudent("10004", "정활발", "고2-A", "010-0000-0004", "리더형"),
    DemoStudent("10005", "최결석", "고2-A", "", "장기 결석형"),
)


def build_demo_dataset(*, base_date: date, weeks: int = 3) -> DemoDataset:
    """Build deterministic persona rows.

    `base_date` is treated as the Monday of the latest report week. Older weeks
    are generated before it to make trend comments possible.
    """
    rows: list[DemoLessonRow] = []
    monday = base_date - timedelta(days=base_date.weekday())
    for week_idx in range(weeks):
        week_start = monday - timedelta(days=(weeks - week_idx - 1) * 7)
        for slot_idx, day_offset in enumerate((1, 3)):  # Tue/Thu evening
            start_at = datetime.combine(
                week_start + timedelta(days=day_offset),
                time(hour=19),
                tzinfo=KST,
            )
            end_at = start_at + timedelta(hours=2)
            lesson_id = f"DEMO-{start_at.date().isoformat()}-{slot_idx + 1}"
            for student in DEMO_STUDENTS:
                rows.append(
                    _row_for_student(
                        student=student,
                        lesson_id=lesson_id,
                        start_at=start_at,
                        end_at=end_at,
                        week_idx=week_idx,
                        slot_idx=slot_idx,
                    )
                )
    return DemoDataset(students=DEMO_STUDENTS, lesson_rows=tuple(rows))


def seed_demo_data(
    cfg: AppConfig,
    *,
    base_date: date,
    weeks: int = 3,
    dry_run: bool = True,
) -> DemoSeedResult:
    dataset = build_demo_dataset(base_date=base_date, weeks=weeks)
    if dry_run:
        return DemoSeedResult(
            students=len(dataset.students),
            lesson_rows=len(dataset.lesson_rows),
            dry_run=True,
        )

    repo = NotionRepo.from_config(cfg)
    for student in dataset.students:
        repo.upsert_student(
            classin_id=student.classin_id,
            name=student.name,
            parent_phone=student.parent_phone,
            class_name=student.class_name,
        )
    for row in dataset.lesson_rows:
        page_id = repo.upsert_lesson_record(
            lesson_id=row.lesson_id,
            course_id=row.course_id,
            student_classin_id=row.student.classin_id,
            class_start=_epoch(row.start_at),
            class_end=_epoch(row.end_at),
            attendance_seconds=row.attendance_seconds,
            first_in_time=_epoch(row.first_in_at),
            last_out_time=_epoch(row.last_out_at),
        )
        repo.patch_lesson_record(
            lesson_id=row.lesson_id,
            student_classin_id=row.student.classin_id,
            camera_minutes=row.camera_minutes,
            hand_raise=row.hand_raise,
            trophy=row.trophy,
            poll=row.poll,
            homework_submitted=row.homework_submitted,
            homework_submitted_late=row.homework_late,
            homework_score=row.homework_score,
            homework_activity_id=DEMO_ACTIVITY_ID,
            page_id=page_id,
        )
    return DemoSeedResult(
        students=len(dataset.students),
        lesson_rows=len(dataset.lesson_rows),
        dry_run=False,
    )


def build_demo_lesson_records(*, base_date: date, weeks: int = 3) -> list[dict[str, Any]]:
    dataset = build_demo_dataset(base_date=base_date, weeks=weeks)
    return [_record_from_demo_row(row) for row in dataset.lesson_rows]


def build_demo_missing_homework_rows(
    *,
    base_date: date,
    weeks: int = 3,
    latest_week_only: bool = True,
) -> list[dict[str, Any]]:
    rows = build_demo_lesson_records(base_date=base_date, weeks=weeks)
    if latest_week_only:
        latest_start = base_date - timedelta(days=base_date.weekday())
        latest_end = latest_start + timedelta(days=7)
        rows = [
            row
            for row in rows
            if latest_start <= datetime.fromisoformat(row["date"]).date() < latest_end
        ]
    return [row for row in rows if row.get("homework_submitted") is False]


def build_demo_notification_history(
    missing_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return deterministic notification states for UI demos.

    The mix intentionally covers the states teachers need to understand:
    generated dry-run copy, failed delivery, and pending/no-phone cases.
    """
    history: list[dict[str, Any]] = []
    seen: set[str] = set()
    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for row in missing_rows:
        student_id = row.get("student_classin_id") or ""
        if student_id in seen:
            continue
        seen.add(student_id)
        if student_id == "10002":
            status = "dry_run"
            message = "김지각 학생 숙제 제출 안내 문구입니다. 오늘 중 제출 부탁드립니다."
        elif student_id == "10003":
            status = "failed"
            message = "이하락 학생 최근 숙제 누락 안내 문구입니다."
        else:
            continue
        history.append(
            {
                "created_at": created_at,
                "event_type": "missing_homework",
                "provider": "dry_run" if status == "dry_run" else "aligo",
                "status": status,
                "student_classin_id": student_id,
                "student_name": row.get("student_name") or "",
                "parent_phone": row.get("parent_phone") or "",
                "message": message,
                "artifact_path": "demo://notify_history",
                "error": "demo failure" if status == "failed" else None,
            }
        )
    return history


def _row_for_student(
    *,
    student: DemoStudent,
    lesson_id: str,
    start_at: datetime,
    end_at: datetime,
    week_idx: int,
    slot_idx: int,
) -> DemoLessonRow:
    duration = int((end_at - start_at).total_seconds())
    if student.classin_id == "10001":  # 박성실
        return _present(
            row_base(student, lesson_id, start_at, end_at),
            duration,
            6,
            2,
            118,
            4,
            True,
            95,
        )
    if student.classin_id == "10002":  # 김지각
        late_minutes = 12 + slot_idx * 4
        return _late(
            row_base(student, lesson_id, start_at, end_at),
            duration - late_minutes * 60,
            late_minutes,
            1,
            0,
            45,
            2,
            week_idx == 0 and slot_idx == 0,
            70 if week_idx == 0 and slot_idx == 0 else None,
        )
    if student.classin_id == "10003":  # 이하락
        decay = week_idx * 2 + slot_idx
        return _present(
            row_base(student, lesson_id, start_at, end_at),
            max(duration - decay * 900, 2400),
            max(4 - decay, 0),
            1 if decay < 2 else 0,
            max(80 - decay * 12, 20),
            max(3 - decay, 0),
            decay < 3,
            82 if decay < 3 else None,
        )
    if student.classin_id == "10004":  # 정활발
        return _present(
            row_base(student, lesson_id, start_at, end_at),
            duration,
            9,
            4,
            120,
            5,
            True,
            98,
        )
    return _absent(row_base(student, lesson_id, start_at, end_at))


def row_base(
    student: DemoStudent,
    lesson_id: str,
    start_at: datetime,
    end_at: datetime,
) -> dict:
    return {
        "lesson_id": lesson_id,
        "course_id": DEMO_COURSE_ID,
        "student": student,
        "start_at": start_at,
        "end_at": end_at,
    }


def _present(
    base: dict,
    seconds: int,
    hand_raise: int,
    trophy: int,
    camera_minutes: float,
    poll: int,
    homework_submitted: bool,
    homework_score: float | None,
) -> DemoLessonRow:
    start_at: datetime = base["start_at"]
    return DemoLessonRow(
        **base,
        attendance_seconds=seconds,
        first_in_at=start_at - timedelta(minutes=2),
        last_out_at=base["end_at"],
        hand_raise=hand_raise,
        trophy=trophy,
        camera_minutes=camera_minutes,
        poll=poll,
        homework_submitted=homework_submitted,
        homework_late=False,
        homework_score=homework_score,
    )


def _late(
    base: dict,
    seconds: int,
    late_minutes: int,
    hand_raise: int,
    trophy: int,
    camera_minutes: float,
    poll: int,
    homework_submitted: bool,
    homework_score: float | None,
) -> DemoLessonRow:
    start_at: datetime = base["start_at"]
    return DemoLessonRow(
        **base,
        attendance_seconds=seconds,
        first_in_at=start_at + timedelta(minutes=late_minutes),
        last_out_at=base["end_at"],
        hand_raise=hand_raise,
        trophy=trophy,
        camera_minutes=camera_minutes,
        poll=poll,
        homework_submitted=homework_submitted,
        homework_late=homework_submitted,
        homework_score=homework_score,
    )


def _absent(base: dict) -> DemoLessonRow:
    return DemoLessonRow(
        **base,
        attendance_seconds=0,
        first_in_at=None,
        last_out_at=None,
        hand_raise=0,
        trophy=0,
        camera_minutes=0,
        poll=0,
        homework_submitted=False,
        homework_late=False,
        homework_score=None,
    )


def _record_from_demo_row(row: DemoLessonRow) -> dict[str, Any]:
    return {
        "page_id": f"demo-page-{row.lesson_id}-{row.student.classin_id}",
        "student_page_id": f"demo-student-{row.student.classin_id}",
        "student_classin_id": row.student.classin_id,
        "student_name": row.student.name,
        "student_class_name": row.student.class_name,
        "parent_phone": row.student.parent_phone,
        "lesson_classin_id": row.lesson_id,
        "course_classin_id": row.course_id,
        "date": row.start_at.isoformat(),
        "attendance": _attendance_label(row),
        "attendance_seconds": row.attendance_seconds,
        "hand_raise": row.hand_raise,
        "trophy": row.trophy,
        "camera_minutes": row.camera_minutes,
        "poll": row.poll,
        "homework_submitted": row.homework_submitted,
        "homework_late": row.homework_late,
        "homework_score": row.homework_score,
    }


def _attendance_label(row: DemoLessonRow) -> str:
    if row.attendance_seconds <= 0:
        return "결석"
    if row.first_in_at and (row.first_in_at - row.start_at) > timedelta(minutes=5):
        return "지각"
    duration = int((row.end_at - row.start_at).total_seconds())
    if duration > 0 and row.attendance_seconds < duration * 0.5:
        return "지각"
    return "출석"


def _epoch(value: datetime | None) -> int | None:
    if value is None:
        return None
    return int(value.timestamp())
