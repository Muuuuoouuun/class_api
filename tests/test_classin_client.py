from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from classin_toolkit.classin.ced import CEDClient
from classin_toolkit.classin.client import ClassInAPIError, ClassInClient
from classin_toolkit.classin.schemas import Course


def test_call_v1_uses_form_safekey_and_errno_1_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/partner/api/course.api.php"
        assert request.url.params["action"] == "register"
        assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"

        form = parse_qs(request.content.decode())
        assert form["SID"] == ["123456"]
        assert form["timeStamp"] == ["1000"]
        assert form["safeKey"] == [hashlib.md5(b"secret1000").hexdigest()]
        assert form["telephone"] == ["01012345678"]
        return httpx.Response(
            200,
            json={"data": 1001930, "error_info": {"errno": 1, "error": "ok"}},
        )

    client = ClassInClient(
        base_url="https://api.eeo.cn",
        school_id="123456",
        secret_key="secret",
        transport=httpx.MockTransport(handler),
    )

    assert client.call_v1("register", {"telephone": "01012345678"}, ts=1000) == 1001930


def test_call_v1_encodes_nested_values_as_json_strings() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        assert json.loads(form["studentJson"][0]) == [{"uid": "1001"}]
        return httpx.Response(200, json={"error_info": {"errno": 1, "error": "ok"}})

    client = ClassInClient(
        base_url="https://api.eeo.cn",
        school_id="123456",
        secret_key="secret",
        transport=httpx.MockTransport(handler),
    )

    client.call_v1("addCourseStudentMultiple", {"studentJson": [{"uid": "1001"}]}, ts=1000)


def test_call_v1_duplicate_register_can_be_allowed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": 1001930, "error_info": {"errno": 135, "error": "exists"}},
        )

    client = ClassInClient(
        base_url="https://api.eeo.cn",
        school_id="123456",
        secret_key="secret",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ClassInAPIError):
        client.call_v1("register", {}, ts=1000)
    assert client.call_v1("register", {}, success_codes=(1, 135), ts=1000) == 1001930


def test_call_v2_uses_path_headers_json_and_code_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/lms/unit/create"
        assert request.headers["X-EEO-UID"] == "123456"
        assert request.headers["X-EEO-TS"] == "1000"
        payload = json.loads(request.content)
        assert payload == {"courseId": 414193, "name": "Unit", "publishFlag": 2}
        assert "sid" not in payload
        assert "timeStamp" not in payload
        return httpx.Response(
            200,
            json={"code": 1, "msg": "ok", "data": {"unitId": 26020895}},
        )

    client = ClassInClient(
        base_url="https://api.eeo.cn",
        school_id="123456",
        secret_key="secret",
        transport=httpx.MockTransport(handler),
    )

    data = client.call_v2(
        "/lms/unit/create",
        {"courseId": 414193, "name": "Unit", "publishFlag": 2},
        ts=1000,
    )
    assert data == {"unitId": 26020895}


def test_call_v2_raises_on_non_success_code() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 101002005, "msg": "bad sign"})

    client = ClassInClient(
        base_url="https://api.eeo.cn",
        school_id="123456",
        secret_key="secret",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ClassInAPIError) as exc:
        client.call_v2("/lms/unit/create", {}, ts=1000)
    assert exc.value.errno == 101002005


class FakeClassInClient:
    def __init__(self) -> None:
        self.v1_calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.v2_calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def call_v1(self, action: str, body: dict[str, Any], **kwargs: Any) -> Any:
        self.v1_calls.append((action, body, kwargs))
        return 42

    def call_v2(self, path: str, body: dict[str, Any], **kwargs: Any) -> Any:
        self.v2_calls.append((path, body, kwargs))
        return {"unitId": 99, "activityId": 88, "classId": 77}


def test_ced_course_payload_uses_main_teacher_uid() -> None:
    fake = FakeClassInClient()
    ced = CEDClient(fake)  # type: ignore[arg-type]

    course = ced.add_course(Course(name="Algebra", teacher_ids=["1001"]))

    assert course.classin_id == "42"
    assert fake.v1_calls == [
        ("addCourse", {"courseName": "Algebra", "mainTeacherUid": 1001}, {})
    ]


def test_ced_add_course_students_uses_student_json() -> None:
    fake = FakeClassInClient()
    ced = CEDClient(fake)  # type: ignore[arg-type]

    ced.add_course_students("2001", [1001, 1002])

    assert fake.v1_calls == [
        (
            "addCourseStudentMultiple",
            {
                "courseId": 2001,
                "identity": 1,
                "studentJson": [{"uid": "1001"}, {"uid": "1002"}],
            },
            {},
        )
    ]


def test_ced_create_non_class_activity_requires_activity_id() -> None:
    class MissingActivityIdClient(FakeClassInClient):
        def call_v2(self, path: str, body: dict[str, Any], **kwargs: Any) -> Any:
            self.v2_calls.append((path, body, kwargs))
            return {"unitId": 99}

    ced = CEDClient(MissingActivityIdClient())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="missing activityId"):
        ced.create_non_class_activity(
            course_id=1,
            unit_id=2,
            name="Homework",
            teacher_uid=3,
        )
