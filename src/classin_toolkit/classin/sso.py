"""SSO helper — ClassIn 클라이언트 호출 링크 생성.

출처: https://docs.eeo.cn/api/en/getLoginLinked.html

`getLoginLinked` 는 v1 body 스타일이다 (v2 서명 아님).
응답 `data` 는 호출 URL 문자열 (PC: classin://, 모바일: https://).
"""
from __future__ import annotations

from .client import ClassInClient


def get_login_linked(
    *,
    base_url: str,
    sid: str | int,
    secret_key: str | None = None,
    safe_key: str | None = None,
    uid: str | int,
    course_id: str | int,
    class_id: str | int,
    telephone: str,
    device_type: int = 1,
    life_time: int = 86400,
    timeout: float = 15.0,
) -> str:
    secret = secret_key or safe_key
    if not secret:
        raise ValueError("secret_key is required")
    body = {
        "courseId": int(course_id),
        "classId": int(class_id),
        "uid": str(uid),
        "telephone": telephone,
        "deviceType": device_type,
        "lifeTime": life_time,
    }
    with ClassInClient(
        base_url=base_url,
        school_id=str(sid),
        secret_key=secret,
        timeout=timeout,
    ) as client:
        return str(client.call_v1("getLoginLinked", body))
