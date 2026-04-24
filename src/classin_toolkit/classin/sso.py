"""SSO helper — ClassIn 클라이언트 호출 링크 생성.

출처: https://docs.eeo.cn/api/en/getLoginLinked.html

`getLoginLinked` 는 v1 body 스타일로 safeKey 를 직접 받는다 (v2 서명 아님).
응답 `data` 는 호출 URL 문자열 (PC: classin://, 모바일: https://).
"""
from __future__ import annotations

import time

import httpx

from .client import ENTRYPOINT, ClassInAPIError


def get_login_linked(
    *,
    base_url: str,
    sid: str | int,
    safe_key: str,
    uid: str | int,
    course_id: str | int,
    class_id: str | int,
    telephone: str,
    device_type: int = 1,
    life_time: int = 86400,
    timeout: float = 15.0,
) -> str:
    body = {
        "SID": str(sid),
        "safeKey": safe_key,
        "timeStamp": int(time.time()),
        "courseId": int(course_id),
        "classId": int(class_id),
        "uid": str(uid),
        "telephone": telephone,
        "deviceType": device_type,
        "lifeTime": life_time,
    }
    with httpx.Client(base_url=base_url, timeout=timeout) as client:
        r = client.post(ENTRYPOINT, params={"action": "getLoginLinked"}, json=body)
    r.raise_for_status()
    env = r.json()
    err = env.get("error_info") or {}
    try:
        errno = int(err.get("errno", 0))
    except (TypeError, ValueError):
        errno = -1
    if errno != 0:
        raise ClassInAPIError("getLoginLinked", errno, err.get("error", "unknown"), payload=env)
    return str(env.get("data", ""))
