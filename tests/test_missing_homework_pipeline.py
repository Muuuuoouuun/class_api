from classin_toolkit.config import AppConfig
from classin_toolkit.notify.message import OutgoingMessage
from classin_toolkit.pipelines import missing_homework


def test_sweep_missing_homework_skips_blocked_quality_messages(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    captured: dict = {}

    class FakeRepo:
        def find_missing_homework(self, *, since, lesson_id):
            return [
                {
                    "student_classin_id": "10001",
                    "lesson_classin_id": "lesson-1",
                    "date": "2026-04-24T10:00:00+00:00",
                },
                {
                    "student_classin_id": "10002",
                    "lesson_classin_id": "lesson-1",
                    "date": "2026-04-24T10:00:00+00:00",
                },
            ]

        def resolve_students(self, classin_ids):
            captured["resolved_ids"] = sorted(classin_ids)
            return {}

    def fake_compose_messages_from_rows(*, cfg, by_student, students_lookup):
        captured["student_ids"] = sorted(by_student.keys())
        return [
            OutgoingMessage(
                student_classin_id="10001",
                student_name="홍길동",
                parent_phone="01012345678",
                message="홍길동 학생 숙제 제출 안내입니다. 오늘 중 제출 부탁드립니다.",
                quality_status="ready",
                quality_score=95,
            ),
            OutgoingMessage(
                student_classin_id="10002",
                student_name="김영희",
                parent_phone="",
                message="",
                quality_status="blocked",
                quality_score=20,
                quality_warnings=["연락처: 보호자 연락처가 없습니다."],
            ),
        ]

    async def fake_dispatch_kakao(cfg, messages):
        captured["dispatch_ids"] = [message.student_classin_id for message in messages]

    def fake_record_notification_history(cfg, messages, *, event_type, provider, status, error):
        captured["history"] = {
            "ids": [message.student_classin_id for message in messages],
            "event_type": event_type,
            "provider": provider,
            "status": status,
            "error": error,
        }

    monkeypatch.setattr(
        missing_homework.NotionRepo,
        "from_config",
        staticmethod(lambda _cfg: FakeRepo()),
    )
    monkeypatch.setattr(
        missing_homework,
        "compose_messages_from_rows",
        fake_compose_messages_from_rows,
    )
    monkeypatch.setattr(missing_homework, "dispatch_kakao", fake_dispatch_kakao)
    monkeypatch.setattr(
        missing_homework,
        "record_notification_history",
        fake_record_notification_history,
    )

    count = missing_homework.sweep_missing_homework(cfg)

    assert count == 1
    assert captured["resolved_ids"] == ["10001", "10002"]
    assert captured["student_ids"] == ["10001", "10002"]
    assert captured["dispatch_ids"] == ["10001"]
    assert captured["history"] == {
        "ids": ["10002"],
        "event_type": "missing_homework",
        "provider": "quality_gate",
        "status": "skipped",
        "error": "blocked message quality",
    }


def test_sweep_missing_homework_live_skips_non_ready_or_unreachable_messages(
    monkeypatch,
    tmp_path,
):
    cfg = _cfg(tmp_path)
    cfg.notify.mode = "live"
    captured: dict = {}

    class FakeRepo:
        def find_missing_homework(self, *, since, lesson_id):
            return [
                {
                    "student_classin_id": "10001",
                    "lesson_classin_id": "lesson-1",
                    "date": "2026-04-24T10:00:00+00:00",
                },
                {
                    "student_classin_id": "10002",
                    "lesson_classin_id": "lesson-1",
                    "date": "2026-04-24T10:00:00+00:00",
                },
                {
                    "student_classin_id": "10003",
                    "lesson_classin_id": "lesson-1",
                    "date": "2026-04-24T10:00:00+00:00",
                },
            ]

        def resolve_students(self, classin_ids):
            captured["resolved_ids"] = sorted(classin_ids)
            return {}

    def fake_compose_messages_from_rows(*, cfg, by_student, students_lookup):
        return [
            OutgoingMessage(
                student_classin_id="10001",
                student_name="홍길동",
                parent_phone="01012345678",
                message="홍길동 학생 숙제 제출 안내입니다. 오늘 중 제출 부탁드립니다.",
                quality_status="ready",
                quality_score=95,
            ),
            OutgoingMessage(
                student_classin_id="10002",
                student_name="김영희",
                parent_phone="01011112222",
                message="김영희 학생 숙제 제출 확인이 필요합니다.",
                quality_status="review",
                quality_score=70,
                quality_warnings=["문구 확인 필요"],
            ),
            OutgoingMessage(
                student_classin_id="10003",
                student_name="이민수",
                parent_phone="",
                message="이민수 학생 숙제 제출 확인이 필요합니다.",
                quality_status="ready",
                quality_score=91,
            ),
        ]

    async def fake_dispatch_kakao(cfg, messages):
        captured["dispatch_ids"] = [message.student_classin_id for message in messages]

    def fake_record_notification_history(cfg, messages, *, event_type, provider, status, error):
        captured["history"] = {
            "ids": [message.student_classin_id for message in messages],
            "event_type": event_type,
            "provider": provider,
            "status": status,
            "error": error,
        }

    monkeypatch.setattr(
        missing_homework.NotionRepo,
        "from_config",
        staticmethod(lambda _cfg: FakeRepo()),
    )
    monkeypatch.setattr(
        missing_homework,
        "compose_messages_from_rows",
        fake_compose_messages_from_rows,
    )
    monkeypatch.setattr(missing_homework, "dispatch_kakao", fake_dispatch_kakao)
    monkeypatch.setattr(
        missing_homework,
        "record_notification_history",
        fake_record_notification_history,
    )

    count = missing_homework.sweep_missing_homework(cfg)

    assert count == 1
    assert captured["resolved_ids"] == ["10001", "10002", "10003"]
    assert captured["dispatch_ids"] == ["10001"]
    assert captured["history"] == {
        "ids": ["10002", "10003"],
        "event_type": "missing_homework",
        "provider": "quality_gate",
        "status": "skipped",
        "error": "message quality not ready for live",
    }


def test_preview_missing_homework_messages_applies_selection_keys(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    captured: dict = {}

    class FakeRepo:
        def find_missing_homework(self, *, since, lesson_id):
            captured["lesson_id"] = lesson_id
            return [
                {
                    "student_classin_id": "10001",
                    "lesson_classin_id": "lesson-1",
                    "date": "2026-04-24T10:00:00+00:00",
                },
                {
                    "student_classin_id": "10002",
                    "lesson_classin_id": "lesson-1",
                    "date": "2026-04-24T10:00:00+00:00",
                },
            ]

        def resolve_students(self, classin_ids):
            captured["resolved_ids"] = list(classin_ids)
            return {}

    def fake_compose_messages_from_rows(*, cfg, by_student, students_lookup):
        captured["student_ids"] = sorted(by_student.keys())
        return [
            OutgoingMessage(
                student_classin_id=student_id,
                student_name=student_id,
                parent_phone="01012345678",
                message="숙제 제출 확인이 필요합니다.",
                quality_status="ready",
                quality_score=90,
            )
            for student_id in sorted(by_student.keys())
        ]

    monkeypatch.setattr(
        missing_homework,
        "compose_messages_from_rows",
        fake_compose_messages_from_rows,
    )

    messages = missing_homework.preview_missing_homework_messages(
        cfg,
        lesson_id="lesson-1",
        selection_keys=["10002::lesson-1::2026-04-24T10:00:00+00:00"],
        repo=FakeRepo(),
    )

    assert [message.student_classin_id for message in messages] == ["10002"]
    assert captured == {
        "lesson_id": "lesson-1",
        "resolved_ids": ["10002"],
        "student_ids": ["10002"],
    }


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
                },
            },
            "anthropic": {"api_key": "sk-ant-test"},
            "reports": {"output_dir": str(tmp_path / "reports")},
            "notify": {"mode": "dry_run", "provider": "aligo"},
        }
    )
