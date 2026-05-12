"""Course/student performance dashboard aggregation.

The UI uses this module as a read-only projection over the existing Notion
lesson and exam records.  ClassIn's public classroom API is creation/edit
heavy, so searchable dashboard options are derived from records already
captured by webhooks and imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from typing import Any, Protocol

from ..config import AppConfig
from ..storage.notion_repo import NotionRepo, StudentRecord


class DashboardRepo(Protocol):
    def list_active_students(self) -> list[StudentRecord]: ...

    def lesson_records(self, *, since: datetime, until: datetime) -> list[dict]: ...

    def exam_records(
        self,
        *,
        since: datetime,
        until: datetime,
        class_name: str | None = None,
    ) -> list[dict]: ...


@dataclass(frozen=True)
class GradePoint:
    date: datetime
    value: float
    label: str
    kind: str
    student_id: str
    student_name: str


_PRESENT = {"출석", "present", "attended"}
_LATE = {"지각", "late", "tardy"}
_ABSENT = {"결석", "absent", "missing"}


def build_course_options(
    cfg: AppConfig,
    *,
    query: str = "",
    limit: int = 30,
    repo: DashboardRepo | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return searchable course dropdown options from recent lesson records."""

    repo = repo or NotionRepo.from_config(cfg)
    now = _as_utc(now or datetime.now(timezone.utc))
    rows = repo.lesson_records(since=now - timedelta(days=365), until=now + timedelta(days=1))
    options = _course_options(rows, query=query, limit=limit)
    return {"ok": True, "items": options, "query": query}


def build_student_options(
    cfg: AppConfig,
    *,
    query: str = "",
    limit: int = 30,
    repo: DashboardRepo | None = None,
) -> dict[str, Any]:
    repo = repo or NotionRepo.from_config(cfg)
    needle = _norm(query)
    items = []
    for student in repo.list_active_students():
        haystack = _norm(f"{student.name} {student.classin_id} {student.class_name or ''}")
        if needle and needle not in haystack:
            continue
        label = " · ".join(
            part for part in (student.name, student.class_name, student.classin_id) if part
        )
        items.append(
            {
                "student_classin_id": student.classin_id,
                "student_name": student.name,
                "class_name": student.class_name or "",
                "label": label,
            }
        )
    items.sort(
        key=lambda item: (
            item["class_name"],
            item["student_name"],
            item["student_classin_id"],
        )
    )
    return {"ok": True, "items": items[: max(0, limit)], "query": query}


def build_course_dashboard(
    cfg: AppConfig,
    *,
    course_id: str | None = None,
    student_id: str | None = None,
    days: int = 90,
    repo: DashboardRepo | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    repo = repo or NotionRepo.from_config(cfg)
    now = _as_utc(now or datetime.now(timezone.utc))
    days = max(7, min(int(days or 90), 365))
    since = now - timedelta(days=days)
    until = now + timedelta(days=1)

    all_rows = repo.lesson_records(since=since, until=until)
    active_students = repo.list_active_students()
    students_by_id = {student.classin_id: student for student in active_students}

    course_id = (course_id or "").strip()
    student_id = (student_id or "").strip()
    rows = [
        row
        for row in all_rows
        if (not course_id or str(row.get("course_classin_id") or "") == course_id)
        and (not student_id or str(row.get("student_classin_id") or "") == student_id)
    ]

    for row in rows:
        sid = str(row.get("student_classin_id") or "")
        if sid and sid not in students_by_id:
            students_by_id[sid] = StudentRecord(
                page_id=str(row.get("student_page_id") or ""),
                classin_id=sid,
                name=str(row.get("student_name") or "미등록"),
                parent_phone=row.get("parent_phone") or None,
                class_name=row.get("student_class_name") or None,
            )

    selected_student_ids = {str(row.get("student_classin_id") or "") for row in rows}
    selected_student_ids.discard("")
    if student_id:
        selected_student_ids.add(student_id)

    exams = repo.exam_records(since=since, until=until)
    if course_id and selected_student_ids:
        exams = [
            exam
            for exam in exams
            if str(exam.get("student_classin_id") or "") in selected_student_ids
        ]
    if student_id:
        exams = [
            exam
            for exam in exams
            if str(exam.get("student_classin_id") or "") == student_id
        ]

    for exam in exams:
        sid = str(exam.get("student_classin_id") or "")
        if sid and sid not in students_by_id:
            students_by_id[sid] = StudentRecord(
                page_id=str(exam.get("student_page_id") or ""),
                classin_id=sid,
                name=str(exam.get("student_name") or "미등록"),
                parent_phone=exam.get("parent_phone") or None,
                class_name=exam.get("student_class_name") or None,
            )

    metrics = _student_metrics(rows, exams, students_by_id)
    score_points = _grade_points(rows, exams)
    attendance_trend = _attendance_trend(rows)
    score_trend = _score_trend(score_points)
    summary = _summary(rows, score_points, metrics, course_id=course_id, student_id=student_id)

    options = _course_options(all_rows, query="", limit=50)
    student_options = _student_option_items(active_students, limit=80)
    scope_label = _scope_label(summary, options, student_options)

    return {
        "ok": True,
        "filters": {
            "course_id": course_id,
            "student_id": student_id,
            "days": days,
            "since": since.date().isoformat(),
            "until": now.date().isoformat(),
        },
        "summary": {**summary, "scope_label": scope_label},
        "course_options": options,
        "student_options": student_options,
        "score_trend": score_trend,
        "attendance_trend": attendance_trend,
        "students": metrics,
        "needs_attention": _needs_attention(metrics),
        "top_movers": _top_movers(metrics),
    }


def demo_course_options(query: str = "", limit: int = 30) -> dict[str, Any]:
    rows, _students, _exams = _demo_rows()
    return {"ok": True, "demo": True, "items": _course_options(rows, query=query, limit=limit)}


def demo_student_options(query: str = "", limit: int = 30) -> dict[str, Any]:
    _rows, students, _exams = _demo_rows()
    needle = _norm(query)
    items = []
    for student in students:
        haystack = _norm(f"{student.name} {student.classin_id} {student.class_name}")
        if needle and needle not in haystack:
            continue
        items.append(
            {
                "student_classin_id": student.classin_id,
                "student_name": student.name,
                "class_name": student.class_name or "",
                "label": " · ".join(
                    part
                    for part in (student.name, student.class_name, student.classin_id)
                    if part
                ),
            }
        )
    return {"ok": True, "demo": True, "items": items[: max(0, limit)]}


def demo_course_dashboard(
    *,
    course_id: str | None = None,
    student_id: str | None = None,
    days: int = 90,
) -> dict[str, Any]:
    all_rows, students, exams = _demo_rows()
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=max(7, min(days, 365)))
    course_id = (course_id or "").strip()
    student_id = (student_id or "").strip()
    rows = [
        row
        for row in all_rows
        if _inside_window(row.get("date"), since, now + timedelta(days=1))
        and (not course_id or str(row.get("course_classin_id") or "") == course_id)
        and (not student_id or str(row.get("student_classin_id") or "") == student_id)
    ]
    exams = [
        exam
        for exam in exams
        if _inside_window(exam.get("exam_date"), since, now + timedelta(days=1))
    ]
    selected_student_ids = {str(row.get("student_classin_id") or "") for row in rows}
    selected_student_ids.discard("")
    if student_id:
        selected_student_ids.add(student_id)
    if course_id and selected_student_ids:
        exams = [
            exam
            for exam in exams
            if str(exam.get("student_classin_id") or "") in selected_student_ids
        ]
    if student_id:
        exams = [exam for exam in exams if str(exam.get("student_classin_id") or "") == student_id]

    students_by_id = {student.classin_id: student for student in students}
    metrics = _student_metrics(rows, exams, students_by_id)
    score_points = _grade_points(rows, exams)
    options = _course_options(all_rows, query="", limit=50)
    student_options = _student_option_items(students, limit=80)
    summary = _summary(rows, score_points, metrics, course_id=course_id, student_id=student_id)
    payload = {
        "ok": True,
        "demo": True,
        "filters": {
            "course_id": course_id,
            "student_id": student_id,
            "days": days,
            "since": since.date().isoformat(),
            "until": now.date().isoformat(),
        },
        "summary": {**summary, "scope_label": _scope_label(summary, options, student_options)},
        "course_options": options,
        "student_options": student_options,
        "score_trend": _score_trend(score_points),
        "attendance_trend": _attendance_trend(rows),
        "students": metrics,
        "needs_attention": _needs_attention(metrics),
        "top_movers": _top_movers(metrics),
    }
    return payload


def _course_options(rows: list[dict], *, query: str, limit: int) -> list[dict[str, Any]]:
    needle = _norm(query)
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        course_id = str(row.get("course_classin_id") or "").strip()
        if not course_id:
            continue
        group = grouped.setdefault(
            course_id,
            {
                "course_id": course_id,
                "course_name": f"Course {course_id}",
                "class_names": set(),
                "student_ids": set(),
                "lesson_count": 0,
                "latest_date": "",
            },
        )
        if row.get("student_class_name"):
            group["class_names"].add(str(row["student_class_name"]))
        if row.get("student_classin_id"):
            group["student_ids"].add(str(row["student_classin_id"]))
        group["lesson_count"] += 1
        if row.get("date"):
            group["latest_date"] = max(group["latest_date"], str(row["date"]))

    items = []
    for group in grouped.values():
        class_names = sorted(group["class_names"])
        class_hint = ", ".join(class_names[:2])
        label = " · ".join(
            part
            for part in (
                group["course_name"],
                class_hint,
                f"{len(group['student_ids'])}명",
            )
            if part
        )
        haystack = _norm(f"{group['course_id']} {label}")
        if needle and needle not in haystack:
            continue
        items.append(
            {
                "course_id": group["course_id"],
                "course_name": group["course_name"],
                "class_names": class_names,
                "student_count": len(group["student_ids"]),
                "lesson_count": group["lesson_count"],
                "latest_date": group["latest_date"],
                "label": label,
            }
        )

    items.sort(key=lambda item: (item["course_name"], item["course_id"]))
    return items[: max(0, limit)]


def _student_option_items(students: list[StudentRecord], *, limit: int) -> list[dict[str, str]]:
    items = []
    for student in students:
        label = " · ".join(
            part for part in (student.name, student.class_name, student.classin_id) if part
        )
        items.append(
            {
                "student_classin_id": student.classin_id,
                "student_name": student.name,
                "class_name": student.class_name or "",
                "label": label,
            }
        )
    items.sort(
        key=lambda item: (
            item["class_name"],
            item["student_name"],
            item["student_classin_id"],
        )
    )
    return items[: max(0, limit)]


def _student_metrics(
    rows: list[dict],
    exams: list[dict],
    students_by_id: dict[str, StudentRecord],
) -> list[dict[str, Any]]:
    grouped_rows: dict[str, list[dict]] = {}
    grouped_exams: dict[str, list[dict]] = {}
    for row in rows:
        sid = str(row.get("student_classin_id") or "")
        if sid:
            grouped_rows.setdefault(sid, []).append(row)
    for exam in exams:
        sid = str(exam.get("student_classin_id") or "")
        if sid:
            grouped_exams.setdefault(sid, []).append(exam)

    metrics = []
    for sid in sorted(set(grouped_rows) | set(grouped_exams)):
        lesson_rows = grouped_rows.get(sid, [])
        exam_rows = grouped_exams.get(sid, [])
        student = students_by_id.get(sid)
        name = (
            student.name
            if student
            else _first_value(lesson_rows + exam_rows, "student_name", "미등록")
        )
        class_name = (
            student.class_name
            if student and student.class_name
            else _first_value(lesson_rows + exam_rows, "student_class_name", "")
        )
        kinds = [_attendance_kind(row.get("attendance")) for row in lesson_rows]
        present = sum(1 for kind in kinds if kind in {"present", "late"})
        late = sum(1 for kind in kinds if kind == "late")
        absent = sum(1 for kind in kinds if kind == "absent")
        known = sum(1 for kind in kinds if kind != "unknown")
        total = len(lesson_rows)
        score_points = _grade_points(lesson_rows, exam_rows)
        values = [point.value for point in score_points]
        score_delta = values[-1] - values[0] if len(values) >= 2 else None
        homework_missing = sum(1 for row in lesson_rows if row.get("homework_submitted") is False)
        attendance_rate = round(present / known, 3) if known else None
        score_avg = round(mean(values), 1) if values else None
        risk_level = _risk_level(attendance_rate, score_avg, homework_missing)
        metrics.append(
            {
                "student_classin_id": sid,
                "student_name": name,
                "class_name": class_name or "",
                "lesson_count": total,
                "attendance_rate": attendance_rate,
                "present_count": present,
                "late_count": late,
                "absent_count": absent,
                "homework_missing": homework_missing,
                "score_avg": score_avg,
                "score_delta": round(score_delta, 1) if score_delta is not None else None,
                "risk_level": risk_level,
                "score_points": [
                    {
                        "date": point.date.date().isoformat(),
                        "value": round(point.value, 1),
                        "label": point.label,
                        "kind": point.kind,
                    }
                    for point in score_points[-12:]
                ],
                "attendance_points": _attendance_trend(lesson_rows)[-12:],
            }
        )

    metrics.sort(
        key=lambda item: (
            _risk_rank(item["risk_level"]),
            -(item["homework_missing"] or 0),
            item["attendance_rate"] if item["attendance_rate"] is not None else 2,
            item["student_name"],
        )
    )
    return metrics


def _grade_points(rows: list[dict], exams: list[dict]) -> list[GradePoint]:
    points: list[GradePoint] = []
    for row in rows:
        value = _number(row.get("homework_score"))
        dt = _parse_datetime(row.get("date"))
        sid = str(row.get("student_classin_id") or "")
        if value is None or not dt or not sid:
            continue
        points.append(
            GradePoint(
                date=dt,
                value=value,
                label="숙제",
                kind="homework",
                student_id=sid,
                student_name=str(row.get("student_name") or "미등록"),
            )
        )
    for exam in exams:
        value = _exam_score(exam)
        dt = _parse_datetime(exam.get("exam_date"))
        sid = str(exam.get("student_classin_id") or "")
        if value is None or not dt or not sid:
            continue
        label = str(exam.get("exam_name") or exam.get("subject") or "시험")
        points.append(
            GradePoint(
                date=dt,
                value=value,
                label=label,
                kind="exam",
                student_id=sid,
                student_name=str(exam.get("student_name") or "미등록"),
            )
        )
    points.sort(key=lambda point: (point.date, point.student_name, point.label))
    return points


def _score_trend(points: list[GradePoint]) -> list[dict[str, Any]]:
    grouped: dict[date, list[GradePoint]] = {}
    for point in points:
        grouped.setdefault(_week_start(point.date), []).append(point)
    trend = []
    for week, week_points in sorted(grouped.items()):
        homework = [point.value for point in week_points if point.kind == "homework"]
        exams = [point.value for point in week_points if point.kind == "exam"]
        all_values = [point.value for point in week_points]
        trend.append(
            {
                "date": week.isoformat(),
                "label": f"{week.month}/{week.day}",
                "avg_score": round(mean(all_values), 1),
                "homework_avg": round(mean(homework), 1) if homework else None,
                "exam_avg": round(mean(exams), 1) if exams else None,
                "count": len(all_values),
            }
        )
    return trend[-14:]


def _attendance_trend(rows: list[dict]) -> list[dict[str, Any]]:
    grouped: dict[date, dict[str, int]] = {}
    for row in rows:
        dt = _parse_datetime(row.get("date"))
        if not dt:
            continue
        group = grouped.setdefault(
            _week_start(dt),
            {"present": 0, "late": 0, "absent": 0, "unknown": 0, "total": 0},
        )
        kind = _attendance_kind(row.get("attendance"))
        if kind == "late":
            group["late"] += 1
        elif kind == "absent":
            group["absent"] += 1
        elif kind == "present":
            group["present"] += 1
        else:
            group["unknown"] += 1
        group["total"] += 1

    trend = []
    for week, counts in sorted(grouped.items()):
        attended = counts["present"] + counts["late"]
        known = counts["total"] - counts["unknown"]
        rate = round(attended / known, 3) if known else 0
        trend.append(
            {
                "date": week.isoformat(),
                "label": f"{week.month}/{week.day}",
                "attendance_rate": rate,
                "present": counts["present"],
                "late": counts["late"],
                "absent": counts["absent"],
                "total": counts["total"],
            }
        )
    return trend[-14:]


def _summary(
    rows: list[dict],
    score_points: list[GradePoint],
    metrics: list[dict[str, Any]],
    *,
    course_id: str,
    student_id: str,
) -> dict[str, Any]:
    total = len(rows)
    kinds = [_attendance_kind(row.get("attendance")) for row in rows]
    present = sum(1 for kind in kinds if kind in {"present", "late"})
    known = sum(1 for kind in kinds if kind != "unknown")
    scores = [point.value for point in score_points]
    score_delta = scores[-1] - scores[0] if len(scores) >= 2 else None
    return {
        "course_id": course_id,
        "student_id": student_id,
        "student_count": len(metrics),
        "lesson_count": total,
        "attendance_rate": round(present / known, 3) if known else None,
        "avg_score": round(mean(scores), 1) if scores else None,
        "score_delta": round(score_delta, 1) if score_delta is not None else None,
        "homework_missing": sum(1 for row in rows if row.get("homework_submitted") is False),
        "risk_count": sum(1 for item in metrics if item["risk_level"] in {"high", "medium"}),
    }


def _scope_label(
    summary: dict[str, Any],
    course_options: list[dict[str, Any]],
    student_options: list[dict[str, str]],
) -> str:
    if summary.get("student_id"):
        for item in student_options:
            if item["student_classin_id"] == summary["student_id"]:
                return item["label"]
        return f"학생 {summary['student_id']}"
    if summary.get("course_id"):
        for item in course_options:
            if item["course_id"] == summary["course_id"]:
                return item["label"]
        return f"Course {summary['course_id']}"
    return "전체 코스"


def _needs_attention(metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in metrics
        if item["risk_level"] in {"high", "medium"}
    ][:8]


def _top_movers(metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    movers = [item for item in metrics if item.get("score_delta") is not None]
    movers.sort(key=lambda item: item["score_delta"], reverse=True)
    return movers[:5]


def _exam_score(row: dict[str, Any]) -> float | None:
    percent = _number(row.get("percent"))
    if percent is not None:
        return percent
    score = _number(row.get("score"))
    max_score = _number(row.get("max_score"))
    if score is not None and max_score not in (None, 0):
        return round(score / max_score * 100, 1)
    return score


def _attendance_kind(value: Any) -> str:
    text = str(value or "").strip().casefold()
    if not text:
        return "unknown"
    if text in _LATE:
        return "late"
    if text in _ABSENT:
        return "absent"
    if text in _PRESENT:
        return "present"
    return "unknown"


def _risk_level(
    attendance_rate: float | None,
    score_avg: float | None,
    homework_missing: int,
) -> str:
    if (
        (attendance_rate is not None and attendance_rate < 0.72)
        or (score_avg is not None and score_avg < 70)
        or homework_missing >= 3
    ):
        return "high"
    if (
        (attendance_rate is not None and attendance_rate < 0.86)
        or (score_avg is not None and score_avg < 80)
        or homework_missing >= 1
    ):
        return "medium"
    return "good"


def _risk_rank(value: str) -> int:
    return {"high": 0, "medium": 1, "good": 2}.get(value, 3)


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())
        except ValueError:
            return None
    return _as_utc(parsed)


def _inside_window(value: Any, since: datetime, until: datetime) -> bool:
    parsed = _parse_datetime(value)
    return bool(parsed and since <= parsed < until)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _week_start(value: datetime) -> date:
    current = value.date()
    return current - timedelta(days=current.weekday())


def _first_value(rows: list[dict], key: str, fallback: str) -> str:
    for row in rows:
        value = row.get(key)
        if value:
            return str(value)
    return fallback


def _norm(value: Any) -> str:
    return str(value or "").replace(" ", "").casefold()


def _demo_rows() -> tuple[list[dict[str, Any]], list[StudentRecord], list[dict[str, Any]]]:
    today = datetime.now(timezone.utc).date()
    students = [
        StudentRecord("p1", "10001", "박서연", "01011112222", "고2-A"),
        StudentRecord("p2", "10002", "김지각", "01055556666", "고2-A"),
        StudentRecord("p3", "10003", "이하락", "01033334444", "고2-B"),
        StudentRecord("p4", "10004", "정민준", "01077778888", "고2-B"),
        StudentRecord("p5", "10005", "최미연", "", "고1-A"),
    ]
    courses = {"C-ALG-2A": students[:2], "C-CAL-2B": students[2:4], "C-GEO-1A": students[4:]}
    rows: list[dict[str, Any]] = []
    for course_id, course_students in courses.items():
        for week in range(10):
            class_date = today - timedelta(days=(9 - week) * 7)
            for index, student in enumerate(course_students):
                drift = week * (3 if student.classin_id != "10003" else -1)
                base = 72 + index * 8 + drift
                attendance = "출석"
                if student.classin_id == "10002" and week in {2, 5, 8}:
                    attendance = "지각"
                if student.classin_id == "10003" and week in {4, 7}:
                    attendance = "결석"
                rows.append(
                    {
                        "student_classin_id": student.classin_id,
                        "student_name": student.name,
                        "student_class_name": student.class_name,
                        "lesson_classin_id": f"{course_id}-L{week + 1}",
                        "course_classin_id": course_id,
                        "date": datetime.combine(
                            class_date,
                            datetime.min.time(),
                            tzinfo=timezone.utc,
                        ).isoformat(),
                        "attendance": attendance,
                        "homework_submitted": not (
                            student.classin_id in {"10002", "10005"} and week in {3, 6}
                        ),
                        "homework_score": max(48, min(100, base)),
                    }
                )
    exams = [
        {
            "student_classin_id": student.classin_id,
            "student_name": student.name,
            "student_class_name": student.class_name,
            "exam_name": "5월 단원평가",
            "exam_date": (today - timedelta(days=8)).isoformat(),
            "percent": 78 + index * 4,
            "attended": True,
        }
        for index, student in enumerate(students)
    ]
    return rows, students, exams
