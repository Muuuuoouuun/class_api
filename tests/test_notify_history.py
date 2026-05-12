import pytest

from classin_toolkit.config import AppConfig
from classin_toolkit.intelligence.missing_homework import OutgoingMessage
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
            )
        ],
    )

    rows = load_notification_history(cfg)

    assert len(rows) == 1
    assert rows[0]["student_classin_id"] == "10001"
    assert rows[0]["status"] == "dry_run"
    assert rows[0]["provider"] == "dry_run"
    assert rows[0]["message"] == "숙제 제출 안내"


@pytest.mark.asyncio
async def test_dispatch_notifications_records_event_type(tmp_path):
    cfg = _cfg(tmp_path)

    await dispatch_notifications(
        cfg,
        [
            OutgoingMessage(
                student_classin_id="10002",
                student_name="Kim Young-hee",
                parent_phone="01055556666",
                message="Exam reminder",
            )
        ],
        event_type="missing_exam",
    )

    rows = load_notification_history(cfg)

    assert len(rows) == 1
    assert rows[0]["event_type"] == "missing_exam"
    assert rows[0]["student_classin_id"] == "10002"
