"""Notion DB 리포지토리 (Layer 2).

단일 진실원. ClassIn 반환 UID 는 반드시 학생 Master DB 에 먼저 저장되어 있어야 함
(지침 02 §1.1). 수업 기록은 (lesson_id, student) 조합이 고유 키.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from notion_client import Client

from ..config import AppConfig

log = logging.getLogger(__name__)


@dataclass
class StudentRecord:
    page_id: str
    classin_id: str
    name: str
    parent_phone: str | None
    class_name: str | None


# --- Notion 속성 이름 (DB 컬럼명과 일치해야 함) -------------------------
PROP_STUDENT_NAME = "학생명"
PROP_STUDENT_CLASSIN_ID = "ClassIn ID"
PROP_STUDENT_PARENT_PHONE = "학부모 연락처"
PROP_STUDENT_CLASS = "반"

PROP_LESSON_STUDENT = "학생"
PROP_LESSON_DATE = "수업일시"
PROP_LESSON_ATTEND = "출석 여부"
PROP_LESSON_ATTEND_SECONDS = "실참여시간(초)"
PROP_LESSON_HAND = "손들기 횟수"
PROP_LESSON_TROPHY = "트로피 수"
PROP_LESSON_CAMERA = "카메라 시간(분)"
PROP_LESSON_POLL = "Poll 응답"
PROP_LESSON_HOMEWORK = "숙제 제출"
PROP_LESSON_HOMEWORK_LATE = "지각 제출"
PROP_LESSON_HOMEWORK_SCORE = "숙제 점수"
PROP_LESSON_ACTIVITY_ID = "ClassIn 숙제 ID"
PROP_LESSON_CLASSIN_LESSON_ID = "ClassIn 수업 ID"
PROP_LESSON_CLASSIN_COURSE_ID = "ClassIn 반 ID"

PROP_REPORT_STUDENT = "학생"
PROP_REPORT_PERIOD = "리포트 기간"
PROP_REPORT_SUMMARY = "학부모 발송 문구"
PROP_REPORT_SENT = "발송 여부"
PROP_REPORT_APPROVED = "승인됨"
PROP_REPORT_HTML_URL = "HTML 링크"

PROP_MEMO_STUDENT = "학생"
PROP_MEMO_DATE = "일자"
PROP_MEMO_TAG = "태그"
PROP_MEMO_TEXT = "내용"

PROP_EXAM_NAME = "시험명"
PROP_EXAM_STUDENT = "학생"
PROP_EXAM_DATE = "시험일"
PROP_EXAM_CLASS = "반"
PROP_EXAM_SUBJECT = "과목"
PROP_EXAM_ATTENDED = "응시 여부"
PROP_EXAM_SCORE = "원점수"
PROP_EXAM_MAX_SCORE = "만점"
PROP_EXAM_PERCENT = "백분율"
PROP_EXAM_SOURCE = "데이터 출처"
PROP_EXAM_EXTERNAL_ID = "외부 시험 ID"


class NotionRepo:
    def __init__(
        self,
        token: str,
        students_db: str,
        lessons_db: str,
        reports_db: str,
        memos_db: str | None = None,
        exams_db: str | None = None,
    ):
        self._nc = Client(auth=token)
        self.students_db = students_db
        self.lessons_db = lessons_db
        self.reports_db = reports_db
        self.memos_db = memos_db
        self.exams_db = exams_db

    @classmethod
    def from_config(cls, cfg: AppConfig) -> "NotionRepo":
        return cls(
            token=cfg.notion.token,
            students_db=cfg.notion.databases.students,
            lessons_db=cfg.notion.databases.lessons,
            reports_db=cfg.notion.databases.reports,
            memos_db=cfg.notion.databases.memos,
            exams_db=cfg.notion.databases.exams,
        )

    # ============== Student ==============

    def find_student_by_classin_id(self, classin_id: str) -> StudentRecord | None:
        res = self._nc.databases.query(
            database_id=self.students_db,
            filter={
                "property": PROP_STUDENT_CLASSIN_ID,
                "rich_text": {"equals": str(classin_id)},
            },
            page_size=1,
        )
        items = res.get("results", [])
        if not items:
            return None
        return _student_from_page(items[0], classin_id=str(classin_id))

    def resolve_students(self, classin_ids: Iterable[str]) -> dict[str, StudentRecord]:
        out: dict[str, StudentRecord] = {}
        for cid in classin_ids:
            rec = self.find_student_by_classin_id(cid)
            if rec:
                out[cid] = rec
        return out

    def upsert_student(
        self,
        *,
        classin_id: str,
        name: str,
        parent_phone: str | None = None,
        class_name: str | None = None,
    ) -> StudentRecord:
        props: dict = {
            PROP_STUDENT_NAME: {"title": [{"text": {"content": name}}]},
            PROP_STUDENT_CLASSIN_ID: {
                "rich_text": [{"text": {"content": str(classin_id)}}]
            },
        }
        if parent_phone:
            props[PROP_STUDENT_PARENT_PHONE] = {"phone_number": parent_phone}
        if class_name:
            props[PROP_STUDENT_CLASS] = {"select": {"name": class_name}}

        existing = self.find_student_by_classin_id(classin_id)
        if existing:
            self._nc.pages.update(page_id=existing.page_id, properties=props)
            return existing
        page = self._nc.pages.create(parent={"database_id": self.students_db}, properties=props)
        return StudentRecord(
            page_id=page["id"],
            classin_id=str(classin_id),
            name=name,
            parent_phone=parent_phone,
            class_name=class_name,
        )

    def list_active_students(self) -> list[StudentRecord]:
        out: list[StudentRecord] = []
        cursor: str | None = None
        while True:
            kwargs: dict = {"database_id": self.students_db, "page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            res = self._nc.databases.query(**kwargs)
            for page in res.get("results", []):
                props = page["properties"]
                cid = _plain(props.get(PROP_STUDENT_CLASSIN_ID))
                if not cid:
                    continue
                out.append(_student_from_page(page, classin_id=cid))
            if not res.get("has_more"):
                break
            cursor = res.get("next_cursor")
        return out

    def find_students_by_name(
        self, name: str, *, class_name: str | None = None
    ) -> list[StudentRecord]:
        target = name.strip()
        if not target:
            return []
        students = self.list_active_students()
        return [
            student
            for student in students
            if student.name.strip() == target and (class_name is None or student.class_name == class_name)
        ]

    # ============== Lesson record upsert ==============

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
    ) -> str | None:
        student = self.find_student_by_classin_id(student_classin_id)
        if not student:
            log.warning("no student row for classin_id=%s — skip", student_classin_id)
            return None

        existing = self._find_lesson_row(lesson_id, student.page_id)
        props: dict = {
            PROP_LESSON_CLASSIN_LESSON_ID: _rich(lesson_id),
            PROP_LESSON_CLASSIN_COURSE_ID: _rich(course_id),
            PROP_LESSON_STUDENT: {"relation": [{"id": student.page_id}]},
        }
        if class_start:
            props[PROP_LESSON_DATE] = {
                "date": {
                    "start": _iso(class_start),
                    "end": _iso(class_end) if class_end else None,
                }
            }
        if attendance_seconds is not None:
            props[PROP_LESSON_ATTEND_SECONDS] = {"number": int(attendance_seconds)}
            props[PROP_LESSON_ATTEND] = {
                "select": {"name": _attend_label(attendance_seconds, class_start, class_end, first_in_time)}
            }

        if existing:
            self._nc.pages.update(page_id=existing, properties=props)
            return existing
        page = self._nc.pages.create(
            parent={"database_id": self.lessons_db}, properties=props
        )
        return page["id"]

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
    ) -> str | None:
        student = self.find_student_by_classin_id(student_classin_id)
        if not student:
            log.warning("patch skipped — no student for %s", student_classin_id)
            return None
        page_id = self._find_lesson_row(lesson_id, student.page_id)
        if not page_id:
            # Attendance 가 아직 안 들어왔을 수 있다 (HomeworkSubmit 이 먼저 오는 경우).
            # 최소 필드로 새 row 생성.
            page = self._nc.pages.create(
                parent={"database_id": self.lessons_db},
                properties={
                    PROP_LESSON_CLASSIN_LESSON_ID: _rich(lesson_id),
                    PROP_LESSON_STUDENT: {"relation": [{"id": student.page_id}]},
                },
            )
            page_id = page["id"]

        props: dict = {}
        if camera_minutes is not None:
            props[PROP_LESSON_CAMERA] = {"number": round(camera_minutes, 1)}
        if hand_raise is not None:
            props[PROP_LESSON_HAND] = {"number": hand_raise}
        if trophy is not None:
            props[PROP_LESSON_TROPHY] = {"number": trophy}
        if poll is not None:
            props[PROP_LESSON_POLL] = {"number": poll}
        if homework_submitted is not None:
            props[PROP_LESSON_HOMEWORK] = {"checkbox": bool(homework_submitted)}
        if homework_submitted_late is not None:
            props[PROP_LESSON_HOMEWORK_LATE] = {"checkbox": bool(homework_submitted_late)}
        if homework_score is not None:
            props[PROP_LESSON_HOMEWORK_SCORE] = {"number": float(homework_score)}
        if homework_activity_id:
            props[PROP_LESSON_ACTIVITY_ID] = _rich(homework_activity_id)

        if props:
            self._nc.pages.update(page_id=page_id, properties=props)
        return page_id

    def _find_lesson_row(self, lesson_id: str, student_page_id: str) -> str | None:
        res = self._nc.databases.query(
            database_id=self.lessons_db,
            filter={
                "and": [
                    {
                        "property": PROP_LESSON_CLASSIN_LESSON_ID,
                        "rich_text": {"equals": str(lesson_id)},
                    },
                    {
                        "property": PROP_LESSON_STUDENT,
                        "relation": {"contains": student_page_id},
                    },
                ]
            },
            page_size=1,
        )
        items = res.get("results", [])
        return items[0]["id"] if items else None

    # ============== Exam record upsert ==============

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
        if not self.exams_db:
            log.warning("exams_db not configured — skip upsert_exam_result")
            return None

        student = student or self.find_student_by_classin_id(student_classin_id)
        if not student:
            log.warning("exam merge skipped — no student for %s", student_classin_id)
            return None

        page_id = self._find_exam_row(
            exam_name=exam_name,
            exam_date=exam_date,
            student_page_id=student.page_id,
            subject=subject,
        )
        percent = (
            round(float(score) / float(max_score) * 100, 1)
            if score is not None and max_score not in (None, 0)
            else None
        )
        props: dict[str, Any] = {
            PROP_EXAM_NAME: {"title": [{"text": {"content": exam_name[:1900]}}]},
            PROP_EXAM_STUDENT: {"relation": [{"id": student.page_id}]},
            PROP_EXAM_DATE: {"date": {"start": exam_date.date().isoformat()}},
            PROP_EXAM_ATTENDED: {"checkbox": bool(attended)},
        }
        if class_name or student.class_name:
            props[PROP_EXAM_CLASS] = _rich(class_name or student.class_name or "")
        if subject:
            props[PROP_EXAM_SUBJECT] = _rich(subject)
        if score is not None:
            props[PROP_EXAM_SCORE] = {"number": float(score)}
        if max_score is not None:
            props[PROP_EXAM_MAX_SCORE] = {"number": float(max_score)}
        if percent is not None:
            props[PROP_EXAM_PERCENT] = {"number": percent}
        if source:
            props[PROP_EXAM_SOURCE] = _rich(source)
        if external_exam_id:
            props[PROP_EXAM_EXTERNAL_ID] = _rich(external_exam_id)

        if page_id:
            self._nc.pages.update(page_id=page_id, properties=props)
            return page_id

        page = self._nc.pages.create(parent={"database_id": self.exams_db}, properties=props)
        return page["id"]

    def _find_exam_row(
        self,
        *,
        exam_name: str,
        exam_date: datetime,
        student_page_id: str,
        subject: str | None = None,
    ) -> str | None:
        if not self.exams_db:
            return None
        filters: list[dict[str, Any]] = [
            {
                "property": PROP_EXAM_STUDENT,
                "relation": {"contains": student_page_id},
            },
            {
                "property": PROP_EXAM_NAME,
                "title": {"equals": exam_name},
            },
            {
                "property": PROP_EXAM_DATE,
                "date": {"equals": exam_date.date().isoformat()},
            },
        ]
        if subject:
            filters.append(
                {
                    "property": PROP_EXAM_SUBJECT,
                    "rich_text": {"equals": subject},
                }
            )
        res = self._nc.databases.query(
            database_id=self.exams_db,
            filter={"and": filters},
            page_size=1,
        )
        items = res.get("results", [])
        return items[0]["id"] if items else None

    # ============== Queries ==============

    def find_missing_homework(
        self, *, since: datetime, lesson_id: str | None = None
    ) -> list[dict]:
        and_filters: list[dict] = [
            {
                "property": PROP_LESSON_DATE,
                "date": {"on_or_after": since.date().isoformat()},
            },
            {
                "or": [
                    {
                        "property": PROP_LESSON_HOMEWORK,
                        "checkbox": {"equals": False},
                    }
                ]
            },
        ]
        if lesson_id:
            and_filters.append(
                {
                    "property": PROP_LESSON_CLASSIN_LESSON_ID,
                    "rich_text": {"equals": lesson_id},
                }
            )
        pages = self._query_all(
            database_id=self.lessons_db, filter={"and": and_filters}
        )
        return self._attach_student_metadata(
            [_row_summary(p) for p in pages]
        )

    def lesson_records(self, *, since: datetime, until: datetime) -> list[dict]:
        pages = self._query_all(
            database_id=self.lessons_db,
            filter={
                "and": [
                    {
                        "property": PROP_LESSON_DATE,
                        "date": {"on_or_after": since.isoformat()},
                    },
                    {
                        "property": PROP_LESSON_DATE,
                        "date": {"before": until.isoformat()},
                    },
                ]
            },
            sorts=[{"property": PROP_LESSON_DATE, "direction": "ascending"}],
        )
        return self._attach_student_metadata(
            [_row_summary(p) for p in pages]
        )

    def weekly_student_stats(
        self, *, student_page_id: str, since: datetime, until: datetime
    ) -> list[dict]:
        pages = self._query_all(
            database_id=self.lessons_db,
            filter={
                "and": [
                    {
                        "property": PROP_LESSON_STUDENT,
                        "relation": {"contains": student_page_id},
                    },
                    {
                        "property": PROP_LESSON_DATE,
                        "date": {"on_or_after": since.date().isoformat()},
                    },
                    {
                        "property": PROP_LESSON_DATE,
                        "date": {"on_or_before": until.date().isoformat()},
                    },
                ]
            },
        )
        return [_row_summary(p) for p in pages]

    def list_exam_results(
        self,
        *,
        exam_name: str,
        exam_date: datetime,
        class_name: str | None = None,
    ) -> list[dict]:
        if not self.exams_db:
            log.warning("exams_db not configured — skip list_exam_results")
            return []

        filters: list[dict[str, Any]] = [
            {
                "property": PROP_EXAM_NAME,
                "title": {"equals": exam_name},
            },
            {
                "property": PROP_EXAM_DATE,
                "date": {"equals": exam_date.date().isoformat()},
            },
        ]
        if class_name:
            filters.append(
                {
                    "property": PROP_EXAM_CLASS,
                    "rich_text": {"equals": class_name},
                }
            )
        pages = self._query_all(
            database_id=self.exams_db,
            filter={"and": filters},
            sorts=[{"property": PROP_EXAM_DATE, "direction": "ascending"}],
        )
        return self._attach_student_metadata([_exam_row_summary(p) for p in pages])

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
        active_students = self.list_active_students()
        if class_name:
            active_students = [student for student in active_students if student.class_name == class_name]

        by_student_page_id = {
            row["student_page_id"]: row
            for row in results
            if row.get("student_page_id")
        }

        missing: list[dict] = []
        for student in active_students:
            existing = by_student_page_id.get(student.page_id)
            if existing and existing.get("attended") is True:
                continue
            base = {
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
            missing.append(base)
        return missing

    def _attach_student_metadata(self, rows: list[dict]) -> list[dict]:
        if not rows:
            return rows
        by_page_id = {student.page_id: student for student in self.list_active_students()}
        for row in rows:
            student = by_page_id.get(row.get("student_page_id"))
            if not student:
                continue
            row["student_classin_id"] = student.classin_id
            row["student_name"] = student.name
            row["student_class_name"] = student.class_name
            row["parent_phone"] = student.parent_phone
        return rows

    def _query_all(self, **kwargs: Any) -> list[dict]:
        results: list[dict] = []
        cursor: str | None = None
        while True:
            query = dict(kwargs)
            query["page_size"] = 100
            if cursor:
                query["start_cursor"] = cursor
            res = self._nc.databases.query(**query)
            results.extend(res.get("results", []))
            if not res.get("has_more"):
                return results
            cursor = res.get("next_cursor")

    # ============== Report ==============

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
        props = {
            PROP_REPORT_STUDENT: {"relation": [{"id": student.page_id}]},
            PROP_REPORT_PERIOD: {
                "date": {
                    "start": period_start.date().isoformat(),
                    "end": period_end.date().isoformat(),
                }
            },
            PROP_REPORT_SUMMARY: _rich(parent_message[:1900]),
            PROP_REPORT_SENT: {"checkbox": False},
            PROP_REPORT_APPROVED: {"checkbox": True},
        }
        if html_url:
            props[PROP_REPORT_HTML_URL] = {"url": html_url}
        page = self._nc.pages.create(
            parent={"database_id": self.reports_db},
            properties=props,
            children=_md_to_blocks(summary_md),
        )
        return page["id"]

    # ============== Memo (원장 편집 채널) ==============

    def write_memo(
        self,
        *,
        student_classin_id: str,
        text: str,
        tag: str | None = None,
        date: datetime | None = None,
    ) -> str | None:
        if not self.memos_db:
            log.warning("memos_db not configured — skip write_memo")
            return None
        student = self.find_student_by_classin_id(student_classin_id)
        if not student:
            log.warning("memo skipped — no student for %s", student_classin_id)
            return None
        props: dict = {
            PROP_MEMO_STUDENT: {"relation": [{"id": student.page_id}]},
            PROP_MEMO_TEXT: {"title": [{"text": {"content": text[:1900]}}]},
            PROP_MEMO_DATE: {
                "date": {"start": (date or datetime.now(timezone.utc)).date().isoformat()}
            },
        }
        if tag:
            props[PROP_MEMO_TAG] = {"select": {"name": tag}}
        page = self._nc.pages.create(parent={"database_id": self.memos_db}, properties=props)
        return page["id"]


# --- helpers --------------------------------------------------------


def _student_from_page(page: dict, *, classin_id: str) -> StudentRecord:
    props = page["properties"]
    return StudentRecord(
        page_id=page["id"],
        classin_id=classin_id,
        name=_plain(props.get(PROP_STUDENT_NAME)),
        parent_phone=_plain(props.get(PROP_STUDENT_PARENT_PHONE)),
        class_name=_select(props.get(PROP_STUDENT_CLASS)),
    )


def _rich(text: str) -> dict:
    return {"rich_text": [{"text": {"content": str(text)}}]}


def _plain(prop: Any) -> str:
    if not prop:
        return ""
    if "title" in prop:
        return "".join(x.get("plain_text", "") for x in prop["title"])
    if "rich_text" in prop:
        return "".join(x.get("plain_text", "") for x in prop["rich_text"])
    if "phone_number" in prop:
        return prop.get("phone_number") or ""
    return ""


def _select(prop: Any) -> str | None:
    if not prop or not prop.get("select"):
        return None
    return prop["select"].get("name")


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
        return "결석"
    if first_in_time and class_start and (first_in_time - class_start) > 5 * 60:
        return "지각"
    if class_start and class_end:
        duration = class_end - class_start
        if duration > 0 and seconds < duration * 0.5:
            return "지각"
    return "출석"


def _row_summary(page: dict) -> dict:
    p = page["properties"]
    student_relation = (p.get(PROP_LESSON_STUDENT) or {}).get("relation") or []
    student_page_id = student_relation[0]["id"] if student_relation else None
    return {
        "page_id": page["id"],
        "student_page_id": student_page_id,
        "student_classin_id": None,
        "student_name": None,
        "student_class_name": None,
        "lesson_classin_id": _plain(p.get(PROP_LESSON_CLASSIN_LESSON_ID)),
        "course_classin_id": _plain(p.get(PROP_LESSON_CLASSIN_COURSE_ID)),
        "date": (p.get(PROP_LESSON_DATE) or {}).get("date", {}).get("start"),
        "attendance": _select(p.get(PROP_LESSON_ATTEND)),
        "attendance_seconds": (p.get(PROP_LESSON_ATTEND_SECONDS) or {}).get("number") or 0,
        "hand_raise": (p.get(PROP_LESSON_HAND) or {}).get("number") or 0,
        "trophy": (p.get(PROP_LESSON_TROPHY) or {}).get("number") or 0,
        "camera_minutes": (p.get(PROP_LESSON_CAMERA) or {}).get("number") or 0,
        "poll": (p.get(PROP_LESSON_POLL) or {}).get("number") or 0,
        "homework_submitted": (p.get(PROP_LESSON_HOMEWORK) or {}).get("checkbox"),
        "homework_late": (p.get(PROP_LESSON_HOMEWORK_LATE) or {}).get("checkbox"),
        "homework_score": (p.get(PROP_LESSON_HOMEWORK_SCORE) or {}).get("number"),
    }


def _exam_row_summary(page: dict) -> dict:
    p = page["properties"]
    student_relation = (p.get(PROP_EXAM_STUDENT) or {}).get("relation") or []
    student_page_id = student_relation[0]["id"] if student_relation else None
    return {
        "page_id": page["id"],
        "student_page_id": student_page_id,
        "student_classin_id": None,
        "student_name": None,
        "student_class_name": None,
        "exam_name": _plain(p.get(PROP_EXAM_NAME)),
        "exam_date": (p.get(PROP_EXAM_DATE) or {}).get("date", {}).get("start"),
        "class_name": _plain(p.get(PROP_EXAM_CLASS)),
        "subject": _plain(p.get(PROP_EXAM_SUBJECT)),
        "attended": (p.get(PROP_EXAM_ATTENDED) or {}).get("checkbox"),
        "score": (p.get(PROP_EXAM_SCORE) or {}).get("number"),
        "max_score": (p.get(PROP_EXAM_MAX_SCORE) or {}).get("number"),
        "percent": (p.get(PROP_EXAM_PERCENT) or {}).get("number"),
        "source": _plain(p.get(PROP_EXAM_SOURCE)),
        "external_exam_id": _plain(p.get(PROP_EXAM_EXTERNAL_ID)),
    }


def _md_to_blocks(md: str) -> list[dict]:
    blocks: list[dict] = []
    for line in md.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("## "):
            blocks.append(
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {
                        "rich_text": [{"type": "text", "text": {"content": line[3:]}}]
                    },
                }
            )
        elif line.startswith("- "):
            blocks.append(
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [{"type": "text", "text": {"content": line[2:]}}]
                    },
                }
            )
        else:
            blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": line}}]
                    },
                }
            )
    return blocks
