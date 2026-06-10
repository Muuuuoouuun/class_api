"""ClassIn HTTP 클라이언트 (Layer 1).

ClassIn API 는 v1/v2 가 공존한다.

- v1: `POST /partner/api/course.api.php?action=<ACTION>`
  - 인증: form body 의 `SID` / `safeKey=MD5(SECRET+timeStamp)` / `timeStamp`
  - 성공: `error_info.errno == 1`
- v2: `POST /lms/...`
  - 인증: 헤더 `X-EEO-UID` / `X-EEO-TS` / `X-EEO-SIGN`
  - 성공: 최상위 `code == 1`

이 파일이 외부 호출을 전담한다. ClassIn API 변경 시 여기만 수정.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .signing import sign_v1_safekey, sign_v2

log = logging.getLogger(__name__)

ENTRYPOINT = "/partner/api/course.api.php"


class ClassInAPIError(RuntimeError):
    def __init__(self, action: str, errno: int, message: str, payload: Any = None):
        super().__init__(f"ClassIn [{action}] errno={errno} {message}")
        self.action = action
        self.errno = errno
        self.message = message
        self.payload = payload


class ClassInClient:
    def __init__(
        self,
        *,
        base_url: str,
        school_id: str,
        secret_key: str,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.school_id = school_id
        self.secret_key = secret_key
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)

    def __enter__(self) -> "ClassInClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def call(
        self,
        action: str,
        body: dict[str, Any] | None = None,
        *,
        success_codes: tuple[int, ...] = (1,),
        ts: int | None = None,
    ) -> Any:
        """v1 action 호출.

        기존 코드 호환을 위해 `call()` 은 v1 `course.api.php?action=...` 로 둔다.
        LMS 등 v2 경로형 API 는 `call_v2()` 를 사용한다.
        """
        return self.call_v1(action, body, success_codes=success_codes, ts=ts)

    def call_v1(
        self,
        action: str,
        body: dict[str, Any] | None = None,
        *,
        success_codes: tuple[int, ...] = (1,),
        ts: int | None = None,
    ) -> Any:
        body = dict(body or {})
        safe_key, timestamp = sign_v1_safekey(self.secret_key, ts=ts)
        form_body = _encode_v1_form(
            {
                "SID": self.school_id,
                "safeKey": safe_key,
                "timeStamp": timestamp,
                **body,
            }
        )
        log.debug("ClassIn v1 action=%s body=%s", action, body)
        r = self._http.post(
            ENTRYPOINT,
            params={"action": action},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=form_body,
        )
        if r.status_code >= 400:
            raise ClassInAPIError(action, -1, f"HTTP {r.status_code}: {r.text[:400]}")

        envelope = _json_or_error(r, action)
        err = envelope.get("error_info") or {}
        errno = _errno(err)
        if errno not in success_codes:
            raise ClassInAPIError(
                action, errno, err.get("error", "unknown"), payload=envelope
            )
        return envelope.get("data")

    def call_v2(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        success_codes: tuple[int, ...] = (1,),
        ts: int | None = None,
    ) -> Any:
        """v2 경로형 API 호출. 예: `/lms/unit/create`."""
        body = dict(body or {})
        headers, _ts = sign_v2(body, sid=self.school_id, secret=self.secret_key, ts=ts)
        normalized_path = path if path.startswith("/") else f"/{path}"
        log.debug("ClassIn v2 path=%s body=%s", normalized_path, body)
        r = self._http.post(normalized_path, headers=headers, json=body)
        if r.status_code >= 400:
            raise ClassInAPIError(normalized_path, -1, f"HTTP {r.status_code}: {r.text[:400]}")

        envelope = _json_or_error(r, normalized_path)
        code = _int_value(envelope.get("code", -1))
        if code not in success_codes:
            raise ClassInAPIError(
                normalized_path,
                code,
                str(envelope.get("msg") or envelope.get("message") or "unknown"),
                payload=envelope,
            )
        return envelope.get("data")


def _errno(err: dict) -> int:
    return _int_value(err.get("errno", 0))


def _int_value(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def _json_or_error(response: httpx.Response, action: str) -> dict[str, Any]:
    try:
        envelope = response.json()
    except ValueError as e:
        raise ClassInAPIError(action, -1, f"non-json: {e}") from e
    if not isinstance(envelope, dict):
        raise ClassInAPIError(action, -1, "json response is not an object", payload=envelope)
    return envelope


def _encode_v1_form(body: dict[str, Any]) -> dict[str, str]:
    """v1 form body 인코딩.

    ClassIn v1 문서는 중첩 배열/객체 파라미터를 JSON 문자열로 form 필드에 싣는다.
    """
    encoded: dict[str, str] = {}
    for key, value in body.items():
        if value is None:
            continue
        if isinstance(value, (list, dict)):
            encoded[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        elif isinstance(value, bool):
            encoded[key] = "1" if value else "0"
        else:
            encoded[key] = str(value)
    return encoded
