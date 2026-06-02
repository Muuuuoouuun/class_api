from fastapi.testclient import TestClient

from classin_toolkit.config import AppConfig
from classin_toolkit.storage.notion_repo import StudentRecord
from classin_toolkit.ui import create_app


def _cfg(tmp_path) -> AppConfig:
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
            "notify": {
                "mode": "dry_run",
                "provider": "aligo",
                "aligo": {
                    "api_key": "aligo-key",
                    "user_id": "aligo-user",
                    "sender": "01012345678",
                },
            },
            "output": {
                "daily": {"path": str(tmp_path / "daily")},
                "weekly": {"path": str(tmp_path / "weekly")},
            },
            "webhook": {"dump_dir": str(tmp_path / "incoming")},
        }
    )


def test_ui_home_renders_with_config(tmp_path):
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.get("/")

    assert res.status_code == 200
    assert "ClassIn 운영 콘솔" in res.text
    assert "테스트학원" in res.text
    assert "API 연결 점검" in res.text
    assert "data-tab=\"schedule\"" in res.text
    assert "data-tab=\"actions\"" in res.text
    assert "핵심 기능" in res.text
    assert "선택 문자 발송" in res.text
    assert "스케줄표로 수업·숙제 생성" in res.text
    assert "반별 리포트 생성" in res.text
    assert "오늘 0시 이후" in res.text
    assert "반 선택" in res.text
    assert "전체 반" in res.text
    assert "반 목록 새로고침" in res.text
    assert "스케줄 표" in res.text


def test_ui_diagnostics_endpoint_returns_offline_probe_results(tmp_path):
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.get("/api/diagnostics")

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["ready"] is True
    assert body["live"] is False
    assert body["summary"]["skipped"] > 0
    assert {
        "service": "ClassIn",
        "check": "SID / secret_key",
        "status": "ok",
        "detail": "입력됨",
        "next_step": "",
    } in body["items"]


def test_ui_missing_homework_returns_service_error(monkeypatch, tmp_path):
    def broken_query(*_args, **_kwargs):
        raise RuntimeError("Notion says no")

    monkeypatch.setattr("classin_toolkit.ui.query_missing_homework", broken_query)
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.get("/api/missing-homework?window_hours=24")

    assert res.status_code == 502
    assert res.json()["detail"] == "미제출 조회 실패: Notion says no"


def test_ui_missing_homework_requires_notion_config(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    cfg.notion.token = "secret_REPLACE_ME"

    def query_should_not_run(*_args, **_kwargs):
        raise AssertionError("missing Notion config should block before query")

    monkeypatch.setattr("classin_toolkit.ui.query_missing_homework", query_should_not_run)
    client = TestClient(create_app(config=cfg))

    res = client.get("/api/missing-homework?window_hours=24")

    assert res.status_code == 400
    assert res.json()["detail"].startswith("Notion 설정이 필요합니다")


def test_ui_schedule_groups_lesson_records(monkeypatch, tmp_path):
    class FakeRepo:
        def lesson_records(self, *, since, until):
            assert since.date().isoformat() == "2026-04-20"
            assert until.date().isoformat() == "2026-04-27"
            return [
                {
                    "student_classin_id": "10001",
                    "student_name": "홍길동",
                    "student_class_name": "고2-A",
                    "lesson_classin_id": "lesson-1",
                    "course_classin_id": "course-1",
                    "date": "2026-04-20T10:00:00+00:00",
                    "attendance": "출석",
                    "homework_submitted": True,
                    "homework_late": False,
                    "homework_score": 95,
                },
                {
                    "student_classin_id": "10002",
                    "student_name": "김영희",
                    "student_class_name": "고2-A",
                    "lesson_classin_id": "lesson-1",
                    "course_classin_id": "course-1",
                    "date": "2026-04-20T10:00:00+00:00",
                    "attendance": "지각",
                    "homework_submitted": False,
                    "homework_late": None,
                    "homework_score": None,
                },
            ]

    monkeypatch.setattr(
        "classin_toolkit.ui.NotionRepo.from_config",
        staticmethod(lambda _cfg: FakeRepo()),
    )
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.get("/api/schedule?start=2026-04-20&days=7")

    assert res.status_code == 200
    body = res.json()
    assert body["summary"] == {
        "total_lessons": 1,
        "total_student_rows": 2,
        "late": 1,
        "absent": 0,
        "homework_missing": 1,
    }
    assert body["items"][0]["student_count"] == 2
    assert body["items"][0]["attendance"]["출석"] == 1
    assert body["items"][0]["attendance"]["지각"] == 1
    assert body["items"][0]["homework_done"] == 1
    assert body["items"][0]["homework_missing"] == 1


def test_ui_schedule_requires_notion_config(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    cfg.notion.token = "secret_REPLACE_ME"

    def repo_should_not_run(_cfg):
        raise AssertionError("missing Notion config should block before repo access")

    monkeypatch.setattr(
        "classin_toolkit.ui.NotionRepo.from_config",
        staticmethod(repo_should_not_run),
    )
    client = TestClient(create_app(config=cfg))

    res = client.get("/api/schedule?start=2026-04-20&days=7")

    assert res.status_code == 400
    assert res.json()["detail"].startswith("Notion 설정이 필요합니다")


def test_ui_parse_schedule_dry_run_returns_counts(monkeypatch, tmp_path):
    class Result:
        courses_created = 1
        lessons_created = 3
        homework_created = 2
        errors = ["course 고2: teacher UID missing"]

    captured = {}

    def fake_run_core_engine(cfg, *, schedule_text, dry_run):
        captured["academy"] = cfg.academy.name
        captured["schedule_text"] = schedule_text
        captured["dry_run"] = dry_run
        return Result()

    monkeypatch.setattr("classin_toolkit.ui.run_core_engine", fake_run_core_engine)
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.post(
        "/api/parse-schedule-dry-run",
        json={"schedule_text": "course_name,teacher,date\n고2,김선생,2026-05-06"},
    )

    assert res.status_code == 200
    assert captured == {
        "academy": "테스트학원",
        "schedule_text": "course_name,teacher,date\n고2,김선생,2026-05-06",
        "dry_run": True,
    }
    body = res.json()
    assert body["message"] == "스케줄 dry-run을 완료했습니다."
    assert body["summary"] == {"courses": 1, "lessons": 3, "homework": 2, "errors": 1}
    assert body["errors"] == ["course 고2: teacher UID missing"]


def test_ui_create_schedule_can_run_live_after_review(monkeypatch, tmp_path):
    class Result:
        courses_created = 1
        lessons_created = 2
        homework_created = 1
        errors = []

    captured = {}

    def fake_run_core_engine(cfg, *, schedule_text, dry_run):
        captured["academy"] = cfg.academy.name
        captured["schedule_text"] = schedule_text
        captured["dry_run"] = dry_run
        return Result()

    monkeypatch.setattr("classin_toolkit.ui.run_core_engine", fake_run_core_engine)
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.post(
        "/api/create-schedule",
        json={
            "schedule_text": "course_name,teacher,date\n고2,김선생,2026-05-06",
            "dry_run": False,
        },
    )

    assert res.status_code == 200
    assert captured == {
        "academy": "테스트학원",
        "schedule_text": "course_name,teacher,date\n고2,김선생,2026-05-06",
        "dry_run": False,
    }
    body = res.json()
    assert body["message"] == "수업과 숙제를 생성했습니다."
    assert body["summary"] == {"courses": 1, "lessons": 2, "homework": 1, "errors": 0}
    assert body["dry_run"] is False


def test_ui_sweep_missing_homework_accepts_selection_keys(monkeypatch, tmp_path):
    captured = {}

    def fake_sweep_missing_homework(cfg, *, window_hours, lesson_id, selection_keys=None):
        captured["academy"] = cfg.academy.name
        captured["window_hours"] = window_hours
        captured["lesson_id"] = lesson_id
        captured["selection_keys"] = selection_keys
        return 1

    monkeypatch.setattr("classin_toolkit.ui.sweep_missing_homework", fake_sweep_missing_homework)
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.post(
        "/api/sweep-missing-homework",
        json={
            "window_hours": 4,
            "lesson_id": "lesson-1",
            "selection_keys": ["10001::lesson-1::2026-04-24T10:00:00+00:00"],
        },
    )

    assert res.status_code == 200
    assert captured == {
        "academy": "테스트학원",
        "window_hours": 4,
        "lesson_id": "lesson-1",
        "selection_keys": ["10001::lesson-1::2026-04-24T10:00:00+00:00"],
    }
    assert res.json()["count"] == 1


def test_ui_generate_class_reports_wraps_weekly_drafts(monkeypatch, tmp_path):
    captured = {}

    def fake_generate_drafts(
        cfg,
        *,
        reference=None,
        class_name=None,
        student_classin_ids=None,
    ):
        captured["academy"] = cfg.academy.name
        captured["reference"] = reference.date().isoformat()
        captured["class_name"] = class_name
        captured["student_classin_ids"] = student_classin_ids
        return 7

    monkeypatch.setattr("classin_toolkit.ui.generate_drafts", fake_generate_drafts)
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.post(
        "/api/generate-class-reports",
        json={
            "class_name": "고2-A",
            "week": "2026-04-20",
            "student_classin_ids": ["10001", "10002"],
        },
    )

    assert res.status_code == 200
    assert captured == {
        "academy": "테스트학원",
        "reference": "2026-04-20",
        "class_name": "고2-A",
        "student_classin_ids": ["10001", "10002"],
    }
    body = res.json()
    assert body["message"] == "고2-A 리포트 드래프트 7건을 생성했습니다."
    assert body["count"] == 7
    assert body["selected"] == 2
    assert body["includes"] == ["출결", "숙제", "시험 점수"]


def test_ui_report_targets_filters_by_class(monkeypatch, tmp_path):
    class FakeRepo:
        def list_active_students(self):
            return [
                StudentRecord("page-1", "10001", "홍길동", "01012345678", "고2-A"),
                StudentRecord("page-2", "10002", "김영희", "", "고2-B"),
            ]

    monkeypatch.setattr(
        "classin_toolkit.ui.NotionRepo.from_config",
        staticmethod(lambda _cfg: FakeRepo()),
    )
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.get("/api/report-targets?class_name=고2-A")

    assert res.status_code == 200
    body = res.json()
    assert body["summary"] == {"total": 1, "classes": 1, "with_parent_phone": 1}
    assert body["items"] == [
        {
            "student_classin_id": "10001",
            "student_name": "홍길동",
            "class_name": "고2-A",
            "has_parent_phone": True,
        }
    ]


def test_ui_import_exam_results_accepts_csv_text(monkeypatch, tmp_path):
    class Result:
        total_rows = 2
        merged_rows = 1
        unresolved_rows = 1
        skipped_rows = 0
        errors = ["row 2: student not found or ambiguous: 김영희"]
        dry_run = True

    captured = {}

    def fake_import_exam_results(
        cfg,
        *,
        path,
        exam_name,
        exam_date,
        class_name,
        source,
        dry_run,
    ):
        captured["academy"] = cfg.academy.name
        captured["csv_text"] = path.read_text(encoding="utf-8")
        captured["exam_name"] = exam_name
        captured["exam_date"] = exam_date
        captured["class_name"] = class_name
        captured["source"] = source
        captured["dry_run"] = dry_run
        return Result()

    monkeypatch.setattr("classin_toolkit.ui.import_exam_results", fake_import_exam_results)
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.post(
        "/api/import-exam-results",
        json={
            "csv_text": "student_name,score\n홍길동,92\n김영희,",
            "exam_name": "4월 월말평가",
            "exam_date": "2026-04-24",
            "class_name": "고2-A",
        },
    )

    assert res.status_code == 200
    assert captured == {
        "academy": "테스트학원",
        "csv_text": "student_name,score\n홍길동,92\n김영희,",
        "exam_name": "4월 월말평가",
        "exam_date": "2026-04-24",
        "class_name": "고2-A",
        "source": "ui-csv-import",
        "dry_run": True,
    }
    body = res.json()
    assert body["message"] == "시험 결과 dry-run을 완료했습니다."
    assert body["summary"] == {
        "total": 2,
        "merged": 1,
        "unresolved": 1,
        "skipped": 0,
        "errors": 1,
    }
    assert body["errors"] == ["row 2: student not found or ambiguous: 김영희"]


def test_ui_status_reports_local_counts(tmp_path):
    cfg = _cfg(tmp_path)
    daily = tmp_path / "daily"
    weekly = tmp_path / "weekly"
    incoming = tmp_path / "incoming"
    daily.mkdir()
    weekly.mkdir()
    incoming.mkdir()
    (daily / "2026-04-24.html").write_text("daily", encoding="utf-8")
    (weekly / "2026-04-20_홍길동.html").write_text("weekly", encoding="utf-8")
    (weekly / "2026-04-20_drafts.json").write_text("[]", encoding="utf-8")
    (incoming / "event.json").write_text("{}", encoding="utf-8")
    client = TestClient(create_app(config=cfg))

    res = client.get("/api/status")

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["counts"] == {
        "incoming_json": 1,
        "daily_html": 1,
        "weekly_html": 1,
        "weekly_indexes": 1,
        "notification_history": 0,
    }


def test_ui_missing_homework_includes_notification_status(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)

    def fake_query_missing_homework(cfg, *, window_hours, lesson_id):
        return [
            {
                "student_classin_id": "10001",
                "student_name": "홍길동",
                "student_class_name": "고2-A",
                "parent_phone": "01012345678",
                "lesson_classin_id": "lesson-1",
                "course_classin_id": "course-1",
                "date": "2026-04-24T10:00:00+00:00",
                "attendance": "출석",
                "homework_late": False,
                "homework_score": None,
            },
            {
                "student_classin_id": "10002",
                "student_name": "김영희",
                "student_class_name": "고2-A",
                "parent_phone": "",
                "lesson_classin_id": "lesson-1",
                "course_classin_id": "course-1",
                "date": "2026-04-24T10:00:00+00:00",
                "attendance": "지각",
                "homework_late": None,
                "homework_score": None,
            },
        ]

    def fake_history(cfg, *, limit):
        return [
            {
                "created_at": "2026-04-24T12:00:00+00:00",
                "student_classin_id": "10001",
                "student_name": "홍길동",
                "parent_phone": "01012345678",
                "provider": "dry_run",
                "status": "dry_run",
                "message": "숙제 제출 안내",
            }
        ]

    monkeypatch.setattr("classin_toolkit.ui.query_missing_homework", fake_query_missing_homework)
    monkeypatch.setattr("classin_toolkit.ui.load_notification_history", fake_history)
    client = TestClient(create_app(config=cfg))

    res = client.get("/api/missing-homework?window_hours=24")

    assert res.status_code == 200
    body = res.json()
    assert body["summary"] == {
        "total_missing": 2,
        "with_parent_phone": 1,
        "no_parent_phone": 1,
        "pending": 1,
        "dry_run": 1,
        "sent": 0,
        "failed": 0,
    }
    assert body["items"][0]["notification_status"] == "dry_run"
    assert body["items"][0]["selection_key"] == "10001::lesson-1::2026-04-24T10:00:00+00:00"
    assert body["items"][1]["notification_status"] == "pending"
