from fastapi.testclient import TestClient

from classin_toolkit.config import AppConfig, NeisSchoolConfig
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
    assert "ClassIn Toolkit UI" in res.text
    assert "neisSchools" in res.text
    assert "fetchNeisSchedules" in res.text
    assert "테스트학원" in res.text
    assert "다음 액션" in res.text
    assert "성과 대시보드" in res.text
    assert "수성구 주요 고교 시험 기간 종합 예시" in res.text
    assert "경북고등학교" in res.text
    assert "수성구 청호로 300" in res.text
    assert "울산 남구 주요 고교 시험 기간 종합 예시" in res.text
    assert "학성고등학교" in res.text
    assert "울산광역시 남구 문수로 436" in res.text


def test_ui_home_prefills_configured_neis_schools(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.neis.schools = [
        NeisSchoolConfig(name="신성고등학교", office_code="J10", school_code="7530178")
    ]
    client = TestClient(create_app(config=cfg))

    res = client.get("/")

    assert res.status_code == 200
    assert "신성고등학교|J10|7530178" in res.text


def test_ui_report_pdf_downloads(tmp_path):
    client = TestClient(create_app(config=_cfg(tmp_path)))

    res = client.post("/api/report-pdf", json={"comment": "테스트 코멘트입니다."})

    assert res.status_code == 200
    assert res.headers["content-type"] == "application/pdf"
    assert "attachment" in res.headers["content-disposition"]
    assert res.content.startswith(b"%PDF")


def test_ui_demo_mode_runs_without_config_or_notion(monkeypatch, tmp_path):
    def fail_query(*args, **kwargs):
        raise AssertionError("live query should not be called in demo mode")

    monkeypatch.setattr("classin_toolkit.ui.query_missing_homework", fail_query)
    monkeypatch.setattr("classin_toolkit.ui.query_missing_exam", fail_query)
    monkeypatch.setattr("classin_toolkit.ui.load_notification_history", fail_query)
    client = TestClient(create_app(config_path=tmp_path / "missing.yaml", demo=True))

    status = client.get("/api/status").json()
    missing = client.get("/api/missing-homework").json()
    notifications = client.get("/api/notifications").json()
    courses = client.get("/api/course-dashboard/courses?q=C-").json()
    dashboard = client.get("/api/course-dashboard?days=90").json()
    sweep = client.post("/api/sweep-missing-homework", json={}).json()
    exam_import = client.post("/api/import-exam-results", json={}).json()
    exam_preview = client.get(
        "/api/missing-exam?exam_name=April%20Monthly%20Exam&exam_date=2026-04-24"
    ).json()
    exam_sweep = client.post("/api/sweep-missing-exam", json={}).json()

    assert status["ok"] is True
    assert status["mode"] == "demo"
    assert status["academy"] == "ClassIn Demo Academy"
    assert missing["ok"] is True
    assert missing["summary"]["total_missing"] > 0
    assert missing["summary"]["needs_phone"] > 0
    assert missing["data_context"]["summary"]["students_with_context"] > 0
    assert any(item["report_context"]["has_context"] for item in missing["items"])
    assert notifications["summary"]["total"] > 0
    assert courses["items"]
    assert dashboard["summary"]["student_count"] > 0
    assert dashboard["score_trend"]
    assert sweep["demo"] is True
    assert exam_import["demo"] is True
    assert exam_preview["demo"] is True
    assert exam_preview["summary"]["total_missing"] > 0
    assert exam_sweep["demo"] is True


def test_ui_import_exam_results_calls_pipeline(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    captured = {}

    class Result:
        total_rows = 2
        merged_rows = 1
        unresolved_rows = 1
        skipped_rows = 0
        errors = ["row 2: student not found"]
        dry_run = True

    def fake_import_exam_results(cfg, **kwargs):
        captured.update(kwargs)
        return Result()

    monkeypatch.setattr("classin_toolkit.ui.import_exam_results", fake_import_exam_results)
    client = TestClient(create_app(config=cfg))

    res = client.post(
        "/api/import-exam-results",
        json={
            "path": "samples/exam_results_sample.csv",
            "exam_name": "April Monthly Exam",
            "exam_date": "2026-04-24",
            "class_name": "High2-A",
            "source": "academy-db",
            "dry_run": True,
        },
    )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["total"] == 2
    assert body["merged"] == 1
    assert body["unresolved"] == 1
    assert captured["exam_name"] == "April Monthly Exam"
    assert captured["exam_date"] == "2026-04-24"
    assert captured["class_name"] == "High2-A"
    assert captured["source"] == "academy-db"
    assert captured["dry_run"] is True


def test_ui_missing_exam_preview_calls_pipeline(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    captured = {}

    def fake_query_missing_exam(cfg, *, exam_name, exam_date, class_name):
        captured.update(
            {
                "exam_name": exam_name,
                "exam_date": exam_date,
                "class_name": class_name,
            }
        )
        return [
            {
                "student_classin_id": "10002",
                "student_name": "Kim Young-hee",
                "student_class_name": "High2-A",
                "parent_phone": "01055556666",
                "exam_name": exam_name,
                "exam_date": exam_date,
                "attended": False,
            },
            {
                "student_classin_id": "10003",
                "student_name": "Lee Min-su",
                "student_class_name": "High2-A",
                "parent_phone": "",
                "exam_name": exam_name,
                "exam_date": exam_date,
                "attended": None,
            },
        ]

    monkeypatch.setattr("classin_toolkit.ui.query_missing_exam", fake_query_missing_exam)
    client = TestClient(create_app(config=cfg))

    res = client.get(
        "/api/missing-exam"
        "?exam_name=April%20Monthly%20Exam"
        "&exam_date=2026-04-24"
        "&class_name=High2-A"
    )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert captured == {
        "exam_name": "April Monthly Exam",
        "exam_date": "2026-04-24",
        "class_name": "High2-A",
    }
    assert body["summary"] == {
        "exam_name": "April Monthly Exam",
        "exam_date": "2026-04-24",
        "class_name": "High2-A",
        "total_missing": 2,
        "with_parent_phone": 1,
        "no_parent_phone": 1,
        "recorded_absent": 1,
        "not_recorded": 1,
    }
    assert body["items"][0]["has_parent_phone"] is True
    assert body["items"][1]["has_parent_phone"] is False


def test_ui_neis_schedule_endpoint_calls_pipeline(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    captured = {}

    def fake_fetch(cfg, **kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "source": "neis",
            "items": [
                {
                    "id": "neis:1",
                    "source": "neis",
                    "day": "월",
                    "date": "2026-07-06",
                    "time": "2026-07-06",
                    "class_name": "기말고사",
                    "school_name": "서울고등학교",
                    "event_name": "기말고사",
                    "category": "exam",
                    "confidence": 1.0,
                }
            ],
            "schools": [{"name": "서울고등학교"}],
            "unmatched": [],
            "summary": {"event_count": 1, "school_count": 1, "unmatched_count": 0},
        }

    monkeypatch.setattr("classin_toolkit.ui.fetch_relevant_school_schedules", fake_fetch)
    client = TestClient(create_app(config=cfg))

    res = client.post(
        "/api/neis/schedules",
        json={
            "schools": "서울고등학교",
            "start_date": "2026-07-01",
            "end_date": "2026-07-31",
            "keywords": "시험\n방학식",
        },
    )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["items"][0]["event_name"] == "기말고사"
    assert captured == {
        "schools": "서울고등학교",
        "start_date": "2026-07-01",
        "end_date": "2026-07-31",
        "keywords": "시험\n방학식",
    }


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
        "needs_phone": 1,
        "needs_message": 0,
        "needs_review": 1,
        "needs_retry": 0,
        "repeat_students": 0,
    }
    assert body["items"][0]["notification_status"] == "dry_run"
    assert body["items"][0]["action_required"] == "needs_review"
    assert body["items"][0]["report_context"]["has_context"] is False
    assert body["items"][1]["notification_status"] == "pending"
    assert body["items"][1]["action_required"] == "needs_phone"


def test_ui_quick_missing_homework_alert_uses_input_only(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    captured = {}

    async def fake_dispatch(cfg, messages, *, event_type):
        captured["event_type"] = event_type
        captured["messages"] = messages

    monkeypatch.setattr("classin_toolkit.ui.dispatch_notifications", fake_dispatch)
    client = TestClient(create_app(config=cfg))

    res = client.post(
        "/api/quick/missing-homework-alert",
        json={
            "course_id": "C-CAPTURED",
            "raw_recipients": "10001,Alice,01012345678",
            "message": "{{student_name}} / {{course_id}}",
        },
    )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["course_id"] == "C-CAPTURED"
    assert captured["event_type"] == "manual_missing_homework"
    assert captured["messages"][0].student_classin_id == "10001"
    assert captured["messages"][0].student_name == "Alice"
    assert captured["messages"][0].message == "Alice / C-CAPTURED"


def test_ui_sweep_missing_homework_uses_selected_payload(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    captured = {}

    def fake_query_missing_homework(cfg, *, window_hours, lesson_id):
        captured["query"] = {"window_hours": window_hours, "lesson_id": lesson_id}
        return [
            {
                "student_classin_id": "10001",
                "student_name": "홍길동",
                "student_class_name": "고2-A",
                "parent_phone": "01012345678",
                "lesson_classin_id": "lesson-1",
                "course_classin_id": "course-1",
                "date": "2026-04-24T10:00:00+00:00",
            },
            {
                "student_classin_id": "10002",
                "student_name": "김영희",
                "student_class_name": "고2-A",
                "parent_phone": "01022223333",
                "lesson_classin_id": "lesson-2",
                "course_classin_id": "course-1",
                "date": "2026-04-24T11:00:00+00:00",
            },
        ]

    async def fake_dispatch(cfg, messages, *, event_type):
        captured["event_type"] = event_type
        captured["messages"] = messages

    monkeypatch.setattr("classin_toolkit.ui.query_missing_homework", fake_query_missing_homework)
    monkeypatch.setattr("classin_toolkit.ui.dispatch_notifications", fake_dispatch)
    client = TestClient(create_app(config=cfg))

    res = client.post(
        "/api/sweep-missing-homework",
        json={
            "window_hours": 48,
            "course_id": "course-1",
            "template": "{{student_name}}/{{class_name}}/{{date}}/{{academy_name}}",
            "dry_run": True,
            "recipients": [
                {
                    "student_classin_id": "10001",
                    "lesson_classin_id": "lesson-1",
                    "date": "2026-04-24T10:00:00+00:00",
                }
            ],
        },
    )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["count"] == 1
    assert captured["query"] == {"window_hours": 48, "lesson_id": None}
    assert captured["event_type"] == "missing_homework"
    assert len(captured["messages"]) == 1
    assert captured["messages"][0].student_classin_id == "10001"
    assert captured["messages"][0].message == "홍길동/고2-A/2026-04-24/테스트학원"


def test_ui_quick_class_bulk_create_posts_to_classin(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    captured = {"lessons": [], "homework": []}

    class FakeClassInClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    class FakeCEDClient:
        def __init__(self, client):
            self.client = client

        def add_course_class(self, lesson):
            lesson.classin_id = f"class-{len(captured['lessons']) + 1}"
            captured["lessons"].append(lesson)
            return lesson

        def release_homework(self, homework):
            captured["homework"].append(homework)
            return homework

    monkeypatch.setattr("classin_toolkit.ui.ClassInClient", FakeClassInClient)
    monkeypatch.setattr("classin_toolkit.ui.CEDClient", FakeCEDClient)
    client = TestClient(create_app(config=cfg))

    res = client.post(
        "/api/quick/class-bulk-create",
        json={
            "course_id": "C-CAPTURED",
            "raw_classes": "2026-06-11 19:00,Algebra test,90,20001",
            "homework_activity_id": "HW-1",
        },
    )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["created"] == 1
    assert body["homework_released"] == 1
    assert captured["client_kwargs"]["school_id"] == "sid"
    assert captured["lessons"][0].course_id == "C-CAPTURED"
    assert captured["lessons"][0].title == "Algebra test"
    assert captured["lessons"][0].teacher_id == "20001"
    assert captured["homework"][0].classin_id == "HW-1"
    assert captured["homework"][0].lesson_id == "class-1"
