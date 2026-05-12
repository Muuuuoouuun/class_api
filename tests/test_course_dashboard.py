from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from classin_toolkit.config import AppConfig
from classin_toolkit.pipelines.course_dashboard import (
    build_course_dashboard,
    build_course_options,
    build_student_options,
)
from classin_toolkit.storage.notion_repo import StudentRecord


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "academy": {"name": "테스트학원", "timezone": "Asia/Seoul"},
            "classin": {
                "school_id": "sid",
                "secret_key": "secret",
                "webhook_secret": "webhook",
            },
            "notion": {
                "token": "secret_test",
                "databases": {
                    "students": "students",
                    "lessons": "lessons",
                    "reports": "reports",
                    "memos": "memos",
                    "exams": "exams",
                },
            },
            "anthropic": {"api_key": "sk-ant-test"},
        }
    )


class FakeRepo:
    students = [
        StudentRecord("p1", "10001", "박서연", "01011112222", "고2-A"),
        StudentRecord("p2", "10002", "김지각", "01055556666", "고2-A"),
    ]
    rows = [
        {
            "student_page_id": "p1",
            "student_classin_id": "10001",
            "student_name": "박서연",
            "student_class_name": "고2-A",
            "lesson_classin_id": "L1",
            "course_classin_id": "C-A",
            "date": "2026-04-01T09:00:00+00:00",
            "attendance": "출석",
            "homework_submitted": True,
            "homework_score": 70,
        },
        {
            "student_page_id": "p1",
            "student_classin_id": "10001",
            "student_name": "박서연",
            "student_class_name": "고2-A",
            "lesson_classin_id": "L2",
            "course_classin_id": "C-A",
            "date": "2026-04-08T09:00:00+00:00",
            "attendance": "지각",
            "homework_submitted": True,
            "homework_score": 80,
        },
        {
            "student_page_id": "p2",
            "student_classin_id": "10002",
            "student_name": "김지각",
            "student_class_name": "고2-A",
            "lesson_classin_id": "L2",
            "course_classin_id": "C-A",
            "date": "2026-04-08T09:00:00+00:00",
            "attendance": "결석",
            "homework_submitted": False,
            "homework_score": None,
        },
    ]
    exams = [
        {
            "student_page_id": "p1",
            "student_classin_id": "10001",
            "student_name": "박서연",
            "student_class_name": "고2-A",
            "exam_name": "4월 단원평가",
            "exam_date": "2026-04-15",
            "percent": 90,
            "attended": True,
        }
    ]

    def list_active_students(self):
        return self.students

    def lesson_records(self, *, since, until):
        return self.rows

    def exam_records(self, *, since, until, class_name=None):
        if class_name:
            return [e for e in self.exams if e.get("student_class_name") == class_name]
        return self.exams


def test_course_dashboard_aggregates_attendance_scores_and_options(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    repo = FakeRepo()
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)

    course_options = build_course_options(cfg, query="C-A", repo=repo, now=now)
    student_options = build_student_options(cfg, query="지각", repo=repo)
    dashboard = build_course_dashboard(cfg, course_id="C-A", repo=repo, now=now)

    assert course_options["items"][0]["course_id"] == "C-A"
    assert course_options["items"][0]["student_count"] == 2
    assert student_options["items"][0]["student_classin_id"] == "10002"
    assert dashboard["summary"]["student_count"] == 2
    assert dashboard["summary"]["lesson_count"] == 3
    assert dashboard["summary"]["attendance_rate"] == 0.667
    assert dashboard["summary"]["avg_score"] == 80.0
    assert dashboard["summary"]["homework_missing"] == 1
    assert dashboard["score_trend"][-1]["avg_score"] == 90.0
    assert dashboard["students"][0]["student_classin_id"] == "10002"
    assert dashboard["students"][0]["risk_level"] == "high"


def test_student_dashboard_filters_to_single_student(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    dashboard = build_course_dashboard(
        cfg,
        student_id="10001",
        repo=FakeRepo(),
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )

    assert dashboard["summary"]["student_count"] == 1
    assert dashboard["summary"]["lesson_count"] == 2
    assert dashboard["summary"]["attendance_rate"] == 1.0
    assert dashboard["students"][0]["student_name"] == "박서연"


def test_exam_score_uses_score_max_when_percent_missing(tmp_path: Path) -> None:
    class Repo(FakeRepo):
        exams = [
            {
                "student_page_id": "p1",
                "student_classin_id": "10001",
                "student_name": "박서연",
                "student_class_name": "고2-A",
                "exam_name": "기말",
                "exam_date": "2026-04-22",
                "score": 36,
                "max_score": 40,
                "attended": True,
            },
            {
                "student_classin_id": "10001",
                "student_name": "박서연",
                "exam_name": "쪽지",
                "exam_date": "2026-04-23",
                "score": 80,
                "max_score": 0,
                "attended": True,
            },
        ]

    cfg = _cfg(tmp_path)
    dashboard = build_course_dashboard(
        cfg,
        student_id="10001",
        repo=Repo(),
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )

    exam_points = [p for p in dashboard["students"][0]["score_points"] if p["kind"] == "exam"]
    values = sorted(p["value"] for p in exam_points)
    assert values == [80.0, 90.0]


def test_exam_only_student_appears_in_metrics(tmp_path: Path) -> None:
    class Repo(FakeRepo):
        students = list(FakeRepo.students) + [
            StudentRecord("p9", "10099", "신규생", "01099998888", "고2-A"),
        ]
        exams = list(FakeRepo.exams) + [
            {
                "student_page_id": "p9",
                "student_classin_id": "10099",
                "student_name": "신규생",
                "student_class_name": "고2-A",
                "exam_name": "4월 단원평가",
                "exam_date": "2026-04-15",
                "percent": 55,
                "attended": True,
            }
        ]

    cfg = _cfg(tmp_path)
    dashboard = build_course_dashboard(
        cfg,
        repo=Repo(),
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )

    ids = [item["student_classin_id"] for item in dashboard["students"]]
    assert "10099" in ids
    exam_only = next(item for item in dashboard["students"] if item["student_classin_id"] == "10099")
    assert exam_only["score_avg"] == 55.0
    assert exam_only["lesson_count"] == 0


def test_attendance_unknown_excluded_from_rate(tmp_path: Path) -> None:
    class Repo(FakeRepo):
        rows = list(FakeRepo.rows) + [
            {
                "student_page_id": "p1",
                "student_classin_id": "10001",
                "student_name": "박서연",
                "student_class_name": "고2-A",
                "lesson_classin_id": "L3",
                "course_classin_id": "C-A",
                "date": "2026-04-15T09:00:00+00:00",
                "attendance": "조퇴",
                "homework_submitted": True,
                "homework_score": 80,
            }
        ]

    cfg = _cfg(tmp_path)
    dashboard = build_course_dashboard(
        cfg,
        student_id="10001",
        repo=Repo(),
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )

    assert dashboard["summary"]["lesson_count"] == 3
    assert dashboard["summary"]["attendance_rate"] == 1.0


def test_zero_limit_returns_empty(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    repo = FakeRepo()
    courses = build_course_options(
        cfg, repo=repo, limit=0, now=datetime(2026, 5, 12, tzinfo=timezone.utc)
    )
    students = build_student_options(cfg, repo=repo, limit=0)
    assert courses["items"] == []
    assert students["items"] == []
