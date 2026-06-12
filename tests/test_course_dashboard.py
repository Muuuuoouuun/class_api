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


def test_student_master_builds_course_options_without_lesson_rows(tmp_path: Path) -> None:
    class Repo(FakeRepo):
        students = [
            StudentRecord("p1", "10001", "박서연", "01011112222", "고2-A"),
            StudentRecord("p2", "10002", "김지각", "01055556666", "고2-A"),
            StudentRecord("p3", "10003", "이민수", "01033334444", "고1-B"),
        ]
        rows = []
        exams = []

    cfg = _cfg(tmp_path)
    repo = Repo()
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)

    courses = build_course_options(cfg, repo=repo, now=now)
    dashboard = build_course_dashboard(cfg, course_id="class:고2-A", repo=repo, now=now)

    assert [item["course_id"] for item in courses["items"]] == ["class:고1-B", "class:고2-A"]
    assert courses["items"][1]["student_count"] == 2
    assert courses["items"][1]["source"] == "student_master"
    assert dashboard["summary"]["student_count"] == 2
    assert dashboard["summary"]["lesson_count"] == 0
    assert [item["student_name"] for item in dashboard["students"]] == ["김지각", "박서연"]


def test_course_aliases_merge_classin_courses(tmp_path: Path) -> None:
    class Repo(FakeRepo):
        students = [
            StudentRecord("p1", "10001", "박서연", "01011112222", "중2 수학 겨울방학 대비반"),
            StudentRecord("p2", "10002", "김지각", "01055556666", "26 리얼 테스트"),
        ]
        rows = [
            {
                "student_page_id": "p2",
                "student_classin_id": "10002",
                "student_name": "김지각",
                "student_class_name": "26 리얼 테스트",
                "lesson_classin_id": "L-real",
                "course_classin_id": "C-real",
                "date": "2026-05-01T09:00:00+00:00",
                "attendance": "출석",
                "homework_submitted": True,
                "homework_score": 88,
            },
        ]
        exams = []

    base = _cfg(tmp_path).model_dump()
    base["course_links"] = {
        "aliases": {
            "중2 수학 겨울방학 대비반": ["26 리얼 테스트"],
        },
    }
    cfg = AppConfig.model_validate(base)
    repo = Repo()
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)

    courses = build_course_options(cfg, query="26 리얼", repo=repo, now=now)
    course = courses["items"][0]
    dashboard = build_course_dashboard(cfg, course_id=course["course_id"], repo=repo, now=now)

    assert course["course_id"] == "class:중2 수학 겨울방학 대비반"
    assert course["course_name"] == "중2 수학 겨울방학 대비반"
    assert course["class_names"] == ["26 리얼 테스트", "중2 수학 겨울방학 대비반"]
    assert course["student_count"] == 2
    assert dashboard["summary"]["student_count"] == 2
    assert dashboard["summary"]["lesson_count"] == 1


def test_course_alias_option_exists_without_records(tmp_path: Path) -> None:
    class Repo(FakeRepo):
        students = []
        rows = []
        exams = []

    base = _cfg(tmp_path).model_dump()
    base["course_links"] = {
        "aliases": {
            "중2 수학 겨울방학 대비반": ["26 리얼 테스트"],
        },
    }
    cfg = AppConfig.model_validate(base)

    courses = build_course_options(
        cfg,
        query="26 리얼",
        repo=Repo(),
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )

    assert courses["items"] == [
        {
            "course_id": "class:중2 수학 겨울방학 대비반",
            "course_name": "중2 수학 겨울방학 대비반",
            "class_names": ["26 리얼 테스트", "중2 수학 겨울방학 대비반"],
            "student_count": 0,
            "lesson_count": 0,
            "latest_date": "",
            "source": "course_links",
            "label": "중2 수학 겨울방학 대비반 · 26 리얼 테스트, 중2 수학 겨울방학 대비반 · 0명",
        }
    ]


def test_dashboard_defaults_to_active_students_when_records_are_empty(tmp_path: Path) -> None:
    class Repo(FakeRepo):
        students = [
            StudentRecord("p1", "10001", "박서연", "01011112222", "고2-A"),
            StudentRecord("p2", "10002", "김지각", "01055556666", "고2-A"),
        ]
        rows = []
        exams = []

    cfg = _cfg(tmp_path)
    dashboard = build_course_dashboard(
        cfg,
        repo=Repo(),
        now=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )

    assert dashboard["summary"]["scope_label"] == "전체 코스"
    assert dashboard["summary"]["student_count"] == 2
    assert dashboard["students"][0]["lesson_count"] == 0


def test_live_dashboard_excludes_seed_demo_data(tmp_path: Path) -> None:
    class Repo(FakeRepo):
        students = [
            StudentRecord("p-demo", "10001", "박성실", "010-0000-0001", "고2-A"),
            StudentRecord("p-real", "90001", "실제학생", "01099990000", "중3-A"),
        ]
        rows = [
            {
                "student_page_id": "p-demo",
                "student_classin_id": "10001",
                "student_name": "박성실",
                "student_class_name": "고2-A",
                "lesson_classin_id": "DEMO-2026-05-01-1",
                "course_classin_id": "DEMO-COURSE-G2A",
                "date": "2026-05-01T09:00:00+00:00",
                "attendance": "출석",
                "homework_submitted": True,
                "homework_score": 100,
            },
            {
                "student_page_id": "p-real",
                "student_classin_id": "90001",
                "student_name": "실제학생",
                "student_class_name": "중3-A",
                "lesson_classin_id": "L-real",
                "course_classin_id": "C-real",
                "date": "2026-05-01T09:00:00+00:00",
                "attendance": "출석",
                "homework_submitted": True,
                "homework_score": 88,
            },
        ]
        exams = []

    cfg = _cfg(tmp_path)
    repo = Repo()
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)

    courses = build_course_options(cfg, repo=repo, now=now)
    students = build_student_options(cfg, repo=repo)
    dashboard = build_course_dashboard(cfg, repo=repo, now=now)

    assert [item["course_id"] for item in courses["items"]] == ["C-real"]
    assert [item["student_classin_id"] for item in students["items"]] == ["90001"]
    assert dashboard["summary"]["student_count"] == 1
    assert dashboard["students"][0]["student_name"] == "실제학생"


def test_configured_courses_survive_storage_lookup_failure(tmp_path: Path) -> None:
    class BrokenRepo:
        def list_active_students(self):
            raise RuntimeError("storage down")

        def lesson_records(self, *, since, until):
            raise RuntimeError("storage down")

        def exam_records(self, *, since, until, class_name=None):
            raise RuntimeError("storage down")

    base = _cfg(tmp_path).model_dump()
    base["course_links"] = {
        "aliases": {
            "Live Course": ["Current Class"],
        },
    }
    cfg = AppConfig.model_validate(base)
    repo = BrokenRepo()
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)

    courses = build_course_options(cfg, repo=repo, now=now)
    students = build_student_options(cfg, repo=repo)
    dashboard = build_course_dashboard(cfg, repo=repo, now=now)

    assert courses["warning"] == "storage_unavailable"
    assert courses["items"][0]["course_id"] == "class:Live Course"
    assert courses["items"][0]["source"] == "course_links"
    assert students["items"] == []
    assert students["warning"] == "storage_unavailable"
    assert dashboard["warning"] == "storage_unavailable"
    assert dashboard["course_options"][0]["course_id"] == "class:Live Course"


def test_zero_limit_returns_empty(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    repo = FakeRepo()
    courses = build_course_options(
        cfg, repo=repo, limit=0, now=datetime(2026, 5, 12, tzinfo=timezone.utc)
    )
    students = build_student_options(cfg, repo=repo, limit=0)
    assert courses["items"] == []
    assert students["items"] == []
