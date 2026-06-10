---
name: classin-schedule-import
description: Use when uploading a term schedule CSV/XLSX into ClassIn — parses freeform schedule with Claude and creates courses/classes/homework activities via CED + LMS APIs
---

# ClassIn 스케줄 일괄 등록

자유형 스케줄 (CSV/XLSX/텍스트) → Claude 파싱 → 구조화 JSON → ClassIn API 호출.

## When to use

- 학기 초 / 분기 시작 시 ClassIn 에 수업·반·숙제 일괄 생성
- 수동 등록의 반복 작업을 한 번에 끝낼 때

## CLI

```bash
# dry-run (기본): API 호출 없이 파싱 결과만 확인
classin-toolkit parse-schedule samples/schedule_sample.csv

# 실제 ClassIn 에 생성
classin-toolkit parse-schedule samples/schedule_sample.csv --live
```

`--live` 없이 먼저 파싱 결과를 확인한 뒤 `--live` 로 한 번 더 실행하는 게 표준 절차.

## 파이프라인

```
스케줄 파일
   └─> intelligence/schedule_parser  (Claude 파싱 → 구조화 JSON)
        └─> pipelines/core_engine
             ├─ schedule_api: lms     → addCourse → createUnit → createClass → createActivityNoClass → releaseActivity
             └─ schedule_api: legacy  → addCourse → addCourseClass (+ LMS homework activity 별도)
                  └─> 반환 ID 들을 storage/notion_repo 에 영구 저장
```

## 필수 config

```yaml
classin:
  schedule_api: "lms"            # lms (권장) | legacy
  teacher_uids:
    "김선생": "20001"            # parser 의 teacher_name → ClassIn teacherUid
  default_teacher_uid: ""        # teacher_uids 매칭 실패 시 fallback
```

LMS `createClass` 와 homework activity 모두 `teacherUid` 필수. 매핑 누락 시 실패.

## 흔한 실패

| 증상 | 원인 |
|---|---|
| `teacherUid required` | `teacher_uids` / `default_teacher_uid` 둘 다 비어있음 |
| 서명 에러 (`签名异常`) | 시스템 시간 ± 5분 초과. `w32tm /resync` (Windows) |
| `errno != 1` (v1) / `code != 1` (v2) | API 응답 엔벨로프 처리 — `classin-api-integration` 스킬 참고 |

## 관련 코드

- `src/classin_toolkit/intelligence/schedule_parser.py`
- `src/classin_toolkit/pipelines/core_engine.py`
- `src/classin_toolkit/classin/ced.py`

## 참고 문서

- `docs/11_api_integration.md` §4.5 (스케줄 생성 설정)
- `docs/14_developer_guide.md` §4.2 (CED action 추가 절차)
