"""POST /classin/webhook 수신 엔드포인트 계약 테스트.

ClassIn Datasub 는 수신 성공을 `error_info.errno == 1` 로만 인정한다.
errno≠1 이거나 형식이 다르면 재전송 → 시간당 에러메일 → 후속 데이터 차단.
(outbound API 응답 파싱은 test_classin_client.py 가 담당)
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from classin_toolkit import webhook_receiver
from classin_toolkit.classin.signing import sign_v1_safekey
from classin_toolkit.config import AppConfig

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
SECRET = "webhook-secret"


def _load(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def _cfg(tmp_path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "academy": {"name": "테스트학원", "timezone": "Asia/Seoul"},
            "classin": {
                "school_id": "sid",
                "secret_key": "secret",
                "webhook_secret": SECRET,
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
            "webhook": {"dump_dir": str(tmp_path / "incoming")},
        }
    )


def _signed(payload: dict) -> dict:
    """payload 에 유효한 TimeStamp + SafeKey 를 채워 반환한다."""
    ts = int(payload.get("TimeStamp") or 1777582815)
    safekey, _ = sign_v1_safekey(SECRET, ts=ts)
    return {**payload, "TimeStamp": ts, "SafeKey": safekey}


def _patch_handlers(monkeypatch) -> list[str]:
    """모든 ingest 핸들러를 호출만 기록하는 async no-op 으로 교체."""
    calls: list[str] = []

    def make(name: str):
        async def _handler(event, cfg):  # noqa: ANN001
            calls.append(name)

        return _handler

    for fn in (
        "ingest_attendance",
        "ingest_end_summary",
        "ingest_homework_submit",
        "ingest_homework_score",
        "ingest_answer_sheet_score",
    ):
        monkeypatch.setattr(webhook_receiver, fn, make(fn))
    return calls


def test_valid_attendance_acks_errno_1_and_dispatches(tmp_path, monkeypatch):
    calls = _patch_handlers(monkeypatch)
    client = TestClient(webhook_receiver.create_app(config=_cfg(tmp_path)))

    res = client.post("/classin/webhook", json=_signed(_load("attendance_sample.json")))

    assert res.status_code == 200
    assert res.json() == {"error_info": {"errno": 1, "error": "程序正常执行"}}
    assert calls == ["ingest_attendance"]
    # 원본은 항상 dump 되어야 한다 (replay/디버깅용).
    assert list((tmp_path / "incoming").glob("*.json"))


def test_unhandled_cmd_is_acked_without_dispatch(tmp_path, monkeypatch):
    calls = _patch_handlers(monkeypatch)
    client = TestClient(webhook_receiver.create_app(config=_cfg(tmp_path)))

    res = client.post("/classin/webhook", json=_signed({"SID": 1, "Cmd": "Rating", "Data": {}}))

    # 받긴 했으나 처리 대상 아님 → ClassIn 이 무한 재전송하지 않도록 ack.
    assert res.json()["error_info"]["errno"] == 1
    assert calls == []


def test_bad_safekey_is_rejected(tmp_path, monkeypatch):
    calls = _patch_handlers(monkeypatch)
    client = TestClient(webhook_receiver.create_app(config=_cfg(tmp_path)))

    payload = {**_load("attendance_sample.json"), "TimeStamp": 1777582815, "SafeKey": "deadbeef"}
    res = client.post("/classin/webhook", json=payload)

    # 인증 실패는 errno≠1 로 거절(fail-loud). 위조/오설정을 ack 하지 않는다.
    assert res.json()["error_info"]["errno"] != 1
    assert calls == []


def test_handler_failure_still_acks_errno_1(tmp_path, monkeypatch):
    async def boom(event, cfg):  # noqa: ANN001
        raise RuntimeError("notion down")

    monkeypatch.setattr(webhook_receiver, "ingest_attendance", boom)
    client = TestClient(webhook_receiver.create_app(config=_cfg(tmp_path)))

    res = client.post("/classin/webhook", json=_signed(_load("attendance_sample.json")))

    # 핸들러(예: Notion 일시 장애)가 실패해도 원본은 dump 되었으므로 errno:1 로 ack 해
    # 파이프라인 전체 차단을 막고, dump+replay 로 복구한다.
    assert res.json()["error_info"]["errno"] == 1
    assert list((tmp_path / "incoming").glob("*.json"))


def test_non_json_body_is_acked(tmp_path, monkeypatch):
    _patch_handlers(monkeypatch)
    client = TestClient(webhook_receiver.create_app(config=_cfg(tmp_path)))

    res = client.post(
        "/classin/webhook",
        content=b"not-json",
        headers={"Content-Type": "application/json"},
    )

    assert res.json()["error_info"]["errno"] == 1
