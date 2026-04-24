"""ClassIn HTTP 클라이언트 (Layer 1).

단일 진입점: `POST https://api.eeo.cn/partner/api/course.api.php?action=<ACTION>`
- 모든 요청은 JSON body
- 인증: v2 서명 (X-EEO-UID / X-EEO-TS / X-EEO-SIGN)
- 응답: `{ "data": ..., "error_info": { "errno": int, "error": str } }`

이 파일이 외부 호출을 전담한다. ClassIn API 변경 시 여기만 수정.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .signing import sign_v2

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
    ):
        self.base_url = base_url.rstrip("/")
        self.school_id = school_id
        self.secret_key = secret_key
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout)

    def __enter__(self) -> "ClassInClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def call(self, action: str, body: dict[str, Any] | None = None) -> Any:
        body = dict(body or {})
        headers, _ts = sign_v2(body, sid=self.school_id, secret=self.secret_key)
        log.debug("ClassIn action=%s body=%s", action, body)
        r = self._http.post(ENTRYPOINT, params={"action": action}, headers=headers, json=body)
        if r.status_code >= 400:
            raise ClassInAPIError(action, -1, f"HTTP {r.status_code}: {r.text[:400]}")
        try:
            envelope = r.json()
        except ValueError as e:
            raise ClassInAPIError(action, -1, f"non-json: {e}") from e

        err = envelope.get("error_info") or {}
        errno = _errno(err)
        if errno != 0:
            raise ClassInAPIError(
                action, errno, err.get("error", "unknown"), payload=envelope
            )
        return envelope.get("data")


def _errno(err: dict) -> int:
    v = err.get("errno", 0)
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1
