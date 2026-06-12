import hashlib

from classin_toolkit.classin.signing import (
    _build_signing_string,
    sign_v1_safekey,
    sign_v2,
    verify_webhook_safekey,
)


def test_sign_v1_safekey() -> None:
    safe_key, ts = sign_v1_safekey("secret", ts=1234)
    assert safe_key == hashlib.md5(b"secret1234").hexdigest()
    assert ts == 1234


def test_signing_string_sorted_and_filtered() -> None:
    body = {
        "courseName": "Math101",
        "teacherUid": 20001,
        "students": [1, 2, 3],  # 배열 → 제외
        "meta": {"x": 1},  # dict → 제외
    }
    s = _build_signing_string(body, sid=123456, timestamp=1234567890, secret="XXXXXX")
    # keys: courseName, sid, teacherUid, timeStamp. lists/dicts 제외
    assert s == "courseName=Math101&sid=123456&teacherUid=20001&timeStamp=1234567890&key=XXXXXX"


def test_sign_v2_headers_shape() -> None:
    headers, ts = sign_v2(
        {"courseName": "Math101"}, sid=123456, secret="XXXXXX", ts=1234567890
    )
    assert headers["X-EEO-UID"] == "123456"
    assert headers["X-EEO-TS"] == "1234567890"
    assert headers["Content-Type"] == "application/json"
    # Verify hash computed matches manual
    expect = hashlib.md5(
        b"courseName=Math101&sid=123456&timeStamp=1234567890&key=XXXXXX"
    ).hexdigest()
    assert headers["X-EEO-SIGN"] == expect
    assert ts == 1234567890


def test_long_string_excluded() -> None:
    body = {"short": "ok", "long": "a" * 2000}
    s = _build_signing_string(body, sid=1, timestamp=1, secret="s")
    assert "long=" not in s
    assert "short=ok" in s


def test_verify_safekey_roundtrip() -> None:
    body = {"SID": 42, "TimeStamp": 1000, "SafeKey": ""}
    secret = "shh"
    body["SafeKey"] = hashlib.md5(b"shh1000").hexdigest()
    assert verify_webhook_safekey(body, secret)

    body["SafeKey"] = "nope"
    assert not verify_webhook_safekey(body, secret)

    assert not verify_webhook_safekey({"SafeKey": hashlib.md5(b"shh").hexdigest()}, secret)
