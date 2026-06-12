---
name: classin-api-integration
description: Use when modifying Layer 1 (ClassIn API client, v1/v2 signing, CED actions, Webhook schema/Cmd dispatch) — covers signature algorithms, response envelopes, and how to add a new Cmd or CED action
---

# Layer 1 — ClassIn API 통합

ClassIn API 변경은 `src/classin_toolkit/classin/*` 안에서 먼저 해결. 변경이 다른 Layer 로 새면 분리 원칙 위반.

## v1 vs v2

| | v1 | v2 (LMS) |
|---|---|---|
| URL | `POST https://api.eeo.cn/partner/api/course.api.php?action=<ACTION>` | `POST https://api.eeo.cn/lms/...` |
| 인증 | form body: `SID`, `safeKey`, `timeStamp` | header: `X-EEO-UID`, `X-EEO-TS`, `X-EEO-SIGN` |
| Content-Type | form | `application/json` |
| 성공 | `error_info.errno == 1` | 최상위 `code == 1` |

2026-04 기준 LMS 디렉터리 API 는 v2, `register`/`addCourse` 등 사용자·교실 API 는 v1.

## v1 SafeKey

```
safeKey = MD5(SECRET + timeStamp)   # 32자 lowercase
timeStamp: 호출 시점 ± 20분 이내 Unix epoch 초
```

`studentJson`, `classJson` 등 배열/객체 파라미터는 form 필드 안의 **JSON 문자열** 로 전송.

## v2 서명 알고리즘

1. body 에서 제외:
   - list / dict 값
   - UTF-8 기준 1024 bytes 초과 문자열
2. `sid`, `timeStamp` 추가 (body 에는 안 넣고 서명 계산에만 사용)
3. key 를 **ASCII 오름차순** 정렬
4. `k1=v1&k2=v2&...` 포맷
5. 끝에 `&key=<SECRET>` 부착
6. MD5 (lowercase, 32)

```
body: { "courseName": "Math101", "teacherUid": 20001 }
sid: 123456, timeStamp: 1234567890, secret: XXXXXX

signing string:
courseName=Math101&sid=123456&teacherUid=20001&timeStamp=1234567890&key=XXXXXX

MD5 → X-EEO-SIGN
```

서버와 시간 ± 5분 이내. 어긋나면 `签名异常`.

## Webhook SafeKey

`MD5(SECRET + TimeStamp)` — 수신 시 `signing.py` 가 검증.

## 핵심 API (요약)

### v1 User
- `register` — UID 반환. 이미 가입된 전화/이메일은 `135`/`461` 코드와 함께 UID 내려옴 → idempotent 처리
- `registerMultiple`, `addSchoolStudent`, `addTeacher`, `editTeacher`, `stopUsingTeacher`

### v1 Classroom
- `addCourse` → courseId
- `addCourseClass` (2025-05-26 이후 업데이트 중지, **신규 연동은 LMS `createClass` 권장**)
- `addCourseClassMultiple`, `addCourseStudentMultiple`, `addClassStudentMultiple`, `delCourseClass`, `endCourse`

### v2 LMS
- `POST /lms/unit/create` → `data.unitId`
- `POST /lms/activity/createClass` → `data.activityId`, `data.classId`
- `POST /lms/activity/createActivityNoClass` (`activityType=2` = 숙제)
- `POST /lms/activity/release` — 표는 `activityIds`, 예시는 `activityId` (단일/복수 둘 다 처리)
- `POST /lms/activity/addStudent` / `deleteStudent`

### v1 SSO
- `getLoginLinked` — 응답 `data` = 호출 URL
  - PC: `classin://...`
  - Mobile: `https://...`

## Webhook Cmd 디스패치 (MVP 사용)

After-Class Class-related:

| Cmd | 핸들러 | MVP |
|---|---|---|
| `Attendance` | `ingest_attendance` → upsert_lesson_record | ✅ |
| `End` | `ingest_end_summary` → patch_lesson_record(camera/handsup/...) | ✅ |
| `Rating`/`Record`/`Upload`/`EduDt` | — | 선택/❌ |

LMS:

| Cmd | 핸들러 | MVP |
|---|---|---|
| `HomeworkSubmit` | `ingest_homework_submit` → patch(hw=True, late?) | ✅ |
| `HomeworkScore` | `ingest_homework_score` → patch(score) | ✅ |

## 새 Cmd 추가 절차

1. `classin/webhook_schemas.py` — Cmd 상수 + pydantic 모델 추가, `_KNOWN` 맵에 등록
2. `pipelines/ingest.py` — 핸들러 함수 작성 (Layer 4)
3. `webhook_receiver.py` — `dispatch` 맵에 새 Cmd 추가
4. `samples/` 에 페이로드 추가 + `tests/test_webhook_schema.py` assertion

## 새 CED action 추가

1. `classin/ced.py` 메서드 추가:
   - v1: `self._c.call_v1("actionName", body)`
   - v2: `self._c.call_v2("/lms/...", body)`
   - 반환 `data` 에서 ID 추출
2. 필요 시 `classin/schemas.py` 도메인 모델 확장
3. `pipelines/` 에서 활용

스케줄 생성 흐름 변경 시 `pipelines/core_engine.py` 의 `classin.schedule_api` 분기 + `classin.teacher_uids` 매핑도 같이 확인. LMS `createClass` 와 homework 생성에 `teacherUid` 필수.

## 확정/추정 매트릭스

`docs/11_api_integration.md` §6 참고. `⚠️` 항목은 실 샘플 페이로드로 alias 조정 가능, `❌` 는 ClassIn 문의 필요.

## 흔한 함정

| 증상 | 원인 |
|---|---|
| `签名异常` / signature error | 시스템 시간 ± 5분 초과. `w32tm /resync` (Windows) |
| `errno != 1` 인데 errno 메시지 모호 | bulk v1 API 는 최상위 성공 후 `data[]` 안에 항목별 errno 들어있음 |
| `releaseActivity` 필드명 혼란 | 표는 `activityIds`, 예시는 `activityId` — 코드는 둘 다 받게 |

## 관련 코드

- `src/classin_toolkit/classin/client.py` (단일 action POST + v2 서명)
- `src/classin_toolkit/classin/signing.py` (v1 SafeKey + v2 서명)
- `src/classin_toolkit/classin/ced.py` (register/addCourse/...)
- `src/classin_toolkit/classin/sso.py`
- `src/classin_toolkit/classin/schemas.py` (도메인 모델)
- `src/classin_toolkit/classin/webhook_schemas.py` (Cmd discriminated union)

## 테스트

- `tests/test_signing_v2.py` — v1 SafeKey, v2 정렬·필터링·해시
- `tests/test_classin_client.py` — v1 form, v2 headers/JSON, 엔벨로프
- `tests/test_webhook_schema.py` — 3종 샘플 디스크리미네이트

## 참고 문서

- `docs/11_api_integration.md` (전체 스펙)
