import pytest
import httpx

from classin_toolkit.config import AppConfig
from classin_toolkit.intelligence.missing_homework import OutgoingMessage
from classin_toolkit.notify.dispatcher import ALIGO_ALIMTALK_SEND_URL
from classin_toolkit.notify.dispatcher import (
    dispatch_kakao,
    dispatch_notifications,
    load_notification_history,
)


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
                },
            },
            "anthropic": {"api_key": "sk-ant-test"},
            "reports": {"output_dir": str(tmp_path / "reports")},
            "notify": {"mode": "dry_run", "provider": "aligo"},
        }
    )


@pytest.mark.asyncio
async def test_dry_run_dispatch_writes_notification_history(tmp_path):
    cfg = _cfg(tmp_path)

    await dispatch_kakao(
        cfg,
        [
            OutgoingMessage(
                student_classin_id="10001",
                student_name="홍길동",
                parent_phone="01012345678",
                message="숙제 제출 안내",
                quality_status="ready",
                quality_score=95,
            )
        ],
    )

    rows = load_notification_history(cfg)

    assert len(rows) == 1
    assert rows[0]["student_classin_id"] == "10001"
    assert rows[0]["status"] == "dry_run"
    assert rows[0]["provider"] == "dry_run"
    assert rows[0]["message"] == "숙제 제출 안내"
    assert rows[0]["quality_status"] == "ready"
    assert rows[0]["quality_score"] == 95
    assert rows[0]["quality_warnings"] == []


@pytest.mark.asyncio
async def test_dispatch_notifications_records_event_type(tmp_path):
    cfg = _cfg(tmp_path)

    await dispatch_notifications(
        cfg,
        [
            OutgoingMessage(
                student_classin_id="10002",
                student_name="김영희",
                parent_phone="01055556666",
                message="시험 미응시 안내",
            )
        ],
        event_type="missing_exam",
    )

    rows = load_notification_history(cfg)

    assert len(rows) == 1
    assert rows[0]["event_type"] == "missing_exam"
    assert rows[0]["student_classin_id"] == "10002"


@pytest.mark.asyncio
async def test_live_aligo_dispatch_posts_payload_and_records_provider_result(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.notify.mode = "live"
    cfg.notify.aligo.api_key = "aligo-key"
    cfg.notify.aligo.user_id = "aligo-user"
    cfg.notify.aligo.sender = "010-1234-5678"
    cfg.notify.aligo.sender_key = "sender-key"
    cfg.notify.aligo.template_code_missing_homework = "TPL_HOMEWORK"
    fake_http = FakeHttpClient(
        {
            "code": 0,
            "message": "성공적으로 전송요청 하였습니다.",
            "info": {"type": "AT", "mid": 12345, "scnt": 1, "fcnt": 0, "unit": 9, "total": 9},
        }
    )

    await dispatch_kakao(
        cfg,
        [
            OutgoingMessage(
                student_classin_id="10001",
                student_name="홍길동",
                parent_phone="010-9999-0000",
                message="홍길동 학생 숙제 제출 안내입니다.",
                quality_status="ready",
                quality_score=98,
            )
        ],
        http_client_factory=lambda: fake_http,
    )

    assert fake_http.calls == [
        (
            ALIGO_ALIMTALK_SEND_URL,
            {
                "apikey": "aligo-key",
                "userid": "aligo-user",
                "senderkey": "sender-key",
                "tpl_code": "TPL_HOMEWORK",
                "sender": "01012345678",
                "failover": "N",
                "receiver_1": "01099990000",
                "recvname_1": "홍길동",
                "subject_1": "테스트학원 숙제 안내",
                "message_1": "홍길동 학생 숙제 제출 안내입니다.",
            },
        )
    ]
    rows = load_notification_history(cfg)
    assert rows[0]["status"] == "sent"
    assert rows[0]["provider"] == "aligo"
    assert rows[0]["provider_message_id"] == "12345"
    assert rows[0]["provider_response"]["success_count"] == 1


@pytest.mark.asyncio
async def test_live_aligo_blocks_non_ready_messages_and_records_failure(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.notify.mode = "live"
    cfg.notify.aligo.api_key = "aligo-key"
    cfg.notify.aligo.user_id = "aligo-user"
    cfg.notify.aligo.sender = "01012345678"
    cfg.notify.aligo.sender_key = "sender-key"
    cfg.notify.aligo.template_code_missing_homework = "TPL_HOMEWORK"
    fake_http = FakeHttpClient({"code": 0, "info": {"scnt": 1, "fcnt": 0}})

    with pytest.raises(ValueError, match="quality_status=review"):
        await dispatch_kakao(
            cfg,
            [
                OutgoingMessage(
                    student_classin_id="10002",
                    student_name="김영희",
                    parent_phone="01055556666",
                    message="숙제 제출 안내",
                    quality_status="review",
                    quality_score=70,
                )
            ],
            http_client_factory=lambda: fake_http,
        )

    assert fake_http.calls == []
    rows = load_notification_history(cfg)
    assert rows[0]["status"] == "failed"
    assert rows[0]["provider"] == "aligo"
    assert "quality_status=review" in rows[0]["error"]


class FakeHttpClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def post(self, url, *, data):
        self.calls.append((url, data))
        return httpx.Response(200, json=self.payload, request=httpx.Request("POST", url))
