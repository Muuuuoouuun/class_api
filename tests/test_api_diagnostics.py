from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from classin_toolkit.api_diagnostics import diagnose_apis
from classin_toolkit.classin.client import ClassInAPIError
from classin_toolkit.config import AppConfig


def test_offline_diagnostics_mark_live_probes_skipped() -> None:
    report = diagnose_apis(_cfg(), live=False)

    assert report.ready
    assert _status(report, "ClassIn", "v2 LMS signature probe") == "skipped"
    assert _status(report, "Notion", "students DB access") == "skipped"
    assert _status(report, "Claude", "messages probe") == "skipped"
    assert _status(report, "Aligo", "Kakao balance probe") == "skipped"


def test_diagnostics_flag_placeholder_values() -> None:
    report = diagnose_apis(
        _cfg(
            notion={
                "token": "secret_REPLACE_ME",
                "databases": {"students": "REPLACE_ME_STUDENTS_DB_ID"},
            },
            anthropic={"api_key": "sk-ant-REPLACE_ME"},
        ),
        live=False,
    )

    assert _status(report, "Notion", "integration token") == "missing"
    assert _status(report, "Claude", "API key") == "missing"
    assert any(item.status == "missing" for item in report.blockers)


def test_classin_live_diagnostics_explain_v2_signature_error() -> None:
    report = diagnose_apis(
        _cfg(
            notion={"token": "secret_REPLACE_ME"},
            anthropic={"api_key": "sk-ant-REPLACE_ME"},
            notify={"aligo": {"api_key": "REPLACE_ME", "user_id": "REPLACE_ME"}},
        ),
        live=True,
        classin_client_factory=lambda: FakeClassInClient(),
    )

    item = _item(report, "ClassIn", "v2 LMS signature probe")
    assert item.status == "failed"
    assert "v2 signing key" in item.next_step
    assert _status(report, "ClassIn", "v1 SSO probe") == "ok"


def test_aligo_live_diagnostics_uses_balance_probe_without_sending() -> None:
    fake_http = FakeHttpClient({"result_code": 1, "ALT_CNT": 9, "SMS_CNT": 3})

    report = diagnose_apis(
        _cfg(),
        live=True,
        classin_client_factory=lambda: FakeClassInClient(v2_signature_ok=True),
        notion_client_factory=lambda _token: FakeNotionClient(),
        anthropic_client_factory=lambda _key: FakeAnthropicClient(),
        http_client_factory=lambda: fake_http,
    )

    assert report.ready
    assert _status(report, "Aligo", "Kakao balance probe") == "ok"
    assert fake_http.calls == [
        (
            "https://kakaoapi.aligo.in/akv10/heartinfo/",
            {"apikey": "aligo-key", "userid": "aligo-user"},
        )
    ]


def test_aligo_live_diagnostics_flags_missing_template_config() -> None:
    report = diagnose_apis(_cfg(notify={"mode": "live"}), live=False)

    item = _item(report, "Aligo", "Kakao template config")
    assert item.status == "missing"
    assert "sender_key" in item.detail
    assert "template_code_missing_homework" in item.detail


class FakeClassInClient:
    def __init__(self, *, v2_signature_ok: bool = False) -> None:
        self.v2_signature_ok = v2_signature_ok

    def __enter__(self) -> "FakeClassInClient":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def call_v1(self, action: str, body: dict[str, Any]) -> None:
        raise ClassInAPIError(action, 100, "参数不全或错误")

    def call_v2(self, path: str, body: dict[str, Any]) -> None:
        if self.v2_signature_ok:
            raise ClassInAPIError(path, 100, "missing required parameter")
        raise ClassInAPIError(path, 101002005, "签名异常")


class FakeNotionClient:
    @property
    def databases(self) -> "FakeNotionClient":
        return self

    def retrieve(self, *, database_id: str) -> dict[str, str]:
        return {"id": database_id}

    @property
    def data_sources(self) -> "FakeNotionClient":
        return self

    def query(self, *, data_source_id: str, page_size: int = 1) -> dict:
        return {"object": "list", "results": []}


class FakeAnthropicClient:
    @property
    def messages(self) -> "FakeAnthropicClient":
        return self

    def create(self, **_kwargs: Any) -> Any:
        return AnthropicResponse(content=[AnthropicText(type="text", text="OK")])


class FakeHttpClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __enter__(self) -> "FakeHttpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def post(self, url: str, *, data: dict[str, str]) -> httpx.Response:
        self.calls.append((url, data))
        return httpx.Response(200, json=self.payload, request=httpx.Request("POST", url))


@dataclass
class AnthropicText:
    type: str
    text: str


@dataclass
class AnthropicResponse:
    content: list[AnthropicText]


def _cfg(**overrides: Any) -> AppConfig:
    data = {
        "academy": {"name": "테스트학원", "timezone": "Asia/Seoul"},
        "classin": {
            "base_url": "https://api.eeo.cn",
            "school_id": "sid_123",
            "secret_key": "secret_123",
            "webhook_secret": "webhook_123",
            "default_teacher_uid": "20001",
            "teacher_uids": {},
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
        "anthropic": {"api_key": "sk-ant-test", "model": "claude-test"},
        "notify": {
            "mode": "dry_run",
            "provider": "aligo",
            "aligo": {
                "api_key": "aligo-key",
                "user_id": "aligo-user",
                "sender": "01012345678",
            },
        },
    }
    _deep_update(data, overrides)
    return AppConfig.model_validate(data)


def _deep_update(target: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def _item(report: Any, service: str, check: str) -> Any:
    for item in report.items:
        if item.service == service and item.check == check:
            return item
    raise AssertionError(f"missing diagnostic item: {service} {check}")


def _status(report: Any, service: str, check: str) -> str:
    return str(_item(report, service, check).status)
