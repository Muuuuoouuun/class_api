from fastapi.testclient import TestClient

from classin_toolkit.config import AppConfig
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
    assert "ClassIn Toolkit UI" in res.text
    assert "테스트학원" in res.text
    assert "API 연결 점검" in res.text


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
    assert body["items"][1]["notification_status"] == "pending"
