"""ClassIn API v1/v2 서명 + Webhook SafeKey 검증.

출처: https://docs.eeo.cn/api/en/appendix/signature.html
     https://docs.eeo.cn/api/en/appendix/sign_demo.html

## v1 SafeKey 규칙

레거시 `course.api.php?action=...` API 는 body/form 에 `SID`, `safeKey`,
`timeStamp` 를 포함한다.

- `safeKey = MD5(SECRET + timeStamp)` (lowercase, 32자)

## v2 서명 규칙 (요청)

1) body 에서 다음을 제외한다:
   - list / dict 타입 값
   - 1024 바이트 초과 문자열
2) `sid`, `timeStamp` 두 필드를 추가한다 (body 엔 넣지 않고 서명 계산에만 사용).
3) key 를 ASCII 오름차순 정렬.
4) `k1=v1&k2=v2&...` 로 연결.
5) 끝에 `&key=SECRET` 을 붙인다.
6) MD5(lowercase, 32자)를 서명으로 사용.

헤더:
- `X-EEO-UID`  = sid
- `X-EEO-TS`   = timeStamp (Unix epoch 초, 서버와 ±5분 이내)
- `X-EEO-SIGN` = 위 MD5

body 에는 sid/timeStamp 를 넣지 않는다.
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

_MAX_VAL_BYTES = 1024


def sign_v1_safekey(secret: str, *, ts: int | None = None) -> tuple[str, int]:
    """레거시 v1 API safeKey 와 timestamp 를 반환한다."""
    ts = ts or int(time.time())
    safe_key = hashlib.md5(f"{secret}{ts}".encode("utf-8")).hexdigest()
    return safe_key, ts


def _should_include(value: Any) -> bool:
    if isinstance(value, (list, dict)):
        return False
    if isinstance(value, str) and len(value.encode("utf-8")) > _MAX_VAL_BYTES:
        return False
    return True


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def _build_signing_string(
    body: dict[str, Any], *, sid: str | int, timestamp: int, secret: str
) -> str:
    pairs: dict[str, str] = {}
    for k, v in body.items():
        if not _should_include(v):
            continue
        pairs[k] = _stringify(v)
    pairs["sid"] = str(sid)
    pairs["timeStamp"] = str(timestamp)

    joined = "&".join(f"{k}={pairs[k]}" for k in sorted(pairs.keys()))
    return f"{joined}&key={secret}"


def sign_v2(
    body: dict[str, Any],
    *,
    sid: str | int,
    secret: str,
    ts: int | None = None,
) -> tuple[dict[str, str], int]:
    """body 기반으로 v2 서명 헤더 3종을 반환한다. (headers, timestamp) 튜플."""
    ts = ts or int(time.time())
    signing = _build_signing_string(body, sid=sid, timestamp=ts, secret=secret)
    sig = hashlib.md5(signing.encode("utf-8")).hexdigest()
    headers = {
        "X-EEO-UID": str(sid),
        "X-EEO-TS": str(ts),
        "X-EEO-SIGN": sig,
        "Content-Type": "application/json",
    }
    return headers, ts


def verify_webhook_safekey(body: dict, secret: str) -> bool:
    """Webhook 페이로드의 SafeKey 필드 검증.

    Datasub public field 문서 기준 `MD5(SECRET + TimeStamp)` 를 사용한다.
    """
    sent = body.get("SafeKey") or body.get("safeKey")
    if not sent:
        return False
    ts = body.get("TimeStamp") or body.get("timeStamp") or ""
    if not ts:
        return False
    raw = f"{secret}{ts}".encode("utf-8")
    expected = hashlib.md5(raw).hexdigest()
    return str(sent).lower() == expected
