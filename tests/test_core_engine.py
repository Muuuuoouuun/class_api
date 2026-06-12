from __future__ import annotations

from datetime import datetime
from typing import Any

from classin_toolkit.config import AppConfig
from classin_toolkit.intelligence.schedule_parser import ParsedCourse, ParsedHomework, ParsedLesson
from classin_toolkit.pipelines import core_engine


class FakeClassInClient:
    instances: list["FakeClassInClient"] = []

    def __init__(self, **_kwargs: Any) -> None:
        self.v1_calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.v2_calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.instances.append(self)

    def __enter__(self) -> "FakeClassInClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call_v1(self, action: str, body: dict[str, Any], **kwargs: Any) -> Any:
        self.v1_calls.append((action, body, kwargs))
        if action == "addCourse":
            return 501
        if action == "addCourseClass":
            return 801
        return None

    def call_v2(self, path: str, body: dict[str, Any], **kwargs: Any) -> Any:
        self.v2_calls.append((path, body, kwargs))
        if path == "/lms/unit/create":
            return {"unitId": 601}
        if path == "/lms/activity/createClass":
            return {"activityId": 701, "classId": 801}
        if path == "/lms/activity/createActivityNoClass":
            return {"activityId": 901}
        if path == "/lms/activity/release":
            return {"activityId": body.get("activityId")}
        return {}


def test_core_engine_creates_lms_classroom_and_homework(monkeypatch) -> None:
    parsed = [
        ParsedCourse(
            course_name="고2 수학 A반",
            teacher_name="김선생",
            lessons=[
                ParsedLesson(
                    title="수1 - 지수함수 1",
                    start_at=datetime.fromisoformat("2026-05-06T19:00:00+09:00"),
                    end_at=datetime.fromisoformat("2026-05-06T21:00:00+09:00"),
                    homework=ParsedHomework(
                        title="워크북 p.42-48",
                        due_at=datetime.fromisoformat("2026-05-08T23:59:00+09:00"),
                    ),
                )
            ],
        )
    ]
    _wire_core_engine(monkeypatch, parsed)

    result = core_engine.run_core_engine(_cfg(), schedule_text="csv", dry_run=False)
    client = FakeClassInClient.instances[-1]

    assert result.courses_created == 1
    assert result.lessons_created == 1
    assert result.homework_created == 1
    assert result.errors == []
    assert client.v1_calls == [
        ("addCourse", {"courseName": "고2 수학 A반", "mainTeacherUid": 20001}, {})
    ]
    assert [call[0] for call in client.v2_calls] == [
        "/lms/unit/create",
        "/lms/activity/createClass",
        "/lms/activity/createActivityNoClass",
        "/lms/activity/release",
    ]
    assert client.v2_calls[1][1]["teacherUid"] == 20001
    assert client.v2_calls[1][1]["unitId"] == 601
    assert client.v2_calls[2][1]["activityType"] == 2
    assert client.v2_calls[2][1]["name"] == "워크북 p.42-48"
    assert client.v2_calls[3][1] == {"courseId": 501, "activityId": 901}


def test_core_engine_dry_run_returns_planned_counts(monkeypatch) -> None:
    parsed = [
        ParsedCourse(
            course_name="고2 수학 A반",
            teacher_name="김선생",
            lessons=[
                ParsedLesson(
                    title="수1",
                    start_at=datetime.fromisoformat("2026-05-06T19:00:00+09:00"),
                    end_at=datetime.fromisoformat("2026-05-06T21:00:00+09:00"),
                    homework=ParsedHomework(title="워크북"),
                ),
                ParsedLesson(
                    title="수2",
                    start_at=datetime.fromisoformat("2026-05-08T19:00:00+09:00"),
                    end_at=datetime.fromisoformat("2026-05-08T21:00:00+09:00"),
                ),
            ],
        )
    ]
    _wire_core_engine(monkeypatch, parsed)

    result = core_engine.run_core_engine(_cfg(), schedule_text="csv", dry_run=True)

    assert result.courses_created == 1
    assert result.lessons_created == 2
    assert result.homework_created == 1
    assert result.errors == []
    assert FakeClassInClient.instances == []


def test_core_engine_lms_requires_teacher_uid(monkeypatch) -> None:
    parsed = [
        ParsedCourse(
            course_name="고2 수학 A반",
            teacher_name="미등록선생",
            lessons=[
                ParsedLesson(
                    title="수1",
                    start_at=datetime.fromisoformat("2026-05-06T19:00:00+09:00"),
                    end_at=datetime.fromisoformat("2026-05-06T21:00:00+09:00"),
                )
            ],
        )
    ]
    _wire_core_engine(monkeypatch, parsed)

    result = core_engine.run_core_engine(
        _cfg(classin={"teacher_uids": {}, "default_teacher_uid": None}),
        schedule_text="csv",
        dry_run=False,
    )
    client = FakeClassInClient.instances[-1]

    assert result.courses_created == 0
    assert result.lessons_created == 0
    assert result.errors == ["course 고2 수학 A반: teacher UID missing for 미등록선생"]
    assert client.v1_calls == []
    assert client.v2_calls == []


def test_core_engine_legacy_mode_uses_add_course_class(monkeypatch) -> None:
    parsed = [
        ParsedCourse(
            course_name="고2 수학 A반",
            teacher_name=None,
            lessons=[
                ParsedLesson(
                    title="수1",
                    start_at=datetime.fromisoformat("2026-05-06T19:00:00+09:00"),
                    end_at=datetime.fromisoformat("2026-05-06T21:00:00+09:00"),
                )
            ],
        )
    ]
    _wire_core_engine(monkeypatch, parsed)

    result = core_engine.run_core_engine(
        _cfg(classin={"schedule_api": "legacy", "teacher_uids": {}}),
        schedule_text="csv",
        dry_run=False,
    )
    client = FakeClassInClient.instances[-1]

    assert result.courses_created == 1
    assert result.lessons_created == 1
    assert result.homework_created == 0
    assert [call[0] for call in client.v1_calls] == ["addCourse", "addCourseClass"]
    assert client.v2_calls == []


def _wire_core_engine(monkeypatch, parsed: list[ParsedCourse]) -> None:
    FakeClassInClient.instances = []
    monkeypatch.setattr(core_engine, "parse_schedule", lambda _cfg, _text: parsed)
    monkeypatch.setattr(core_engine, "ClassInClient", FakeClassInClient)
    monkeypatch.setattr(core_engine.NotionRepo, "from_config", staticmethod(lambda _cfg: object()))


def _cfg(**overrides: Any) -> AppConfig:
    data: dict[str, Any] = {
        "academy": {"name": "테스트학원", "timezone": "Asia/Seoul"},
        "classin": {
            "school_id": "sid_123",
            "secret_key": "secret_123",
            "webhook_secret": "webhook_123",
            "teacher_uids": {"김선생": "20001"},
        },
        "notion": {
            "token": "secret_test",
            "databases": {
                "students": "students_db",
                "lessons": "lessons_db",
                "reports": "reports_db",
                "memos": "memos_db",
                "exams": "exams_db",
            },
        },
        "anthropic": {"api_key": "sk-ant-test"},
    }
    _deep_update(data, overrides)
    return AppConfig.model_validate(data)


def _deep_update(target: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value
