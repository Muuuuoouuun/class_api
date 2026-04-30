---
name: classin-pipelines-guide
description: Use when adding a new business workflow or modifying an existing pipeline (core_engine/ingest/missing_homework/exams/weekly/daily) — pipelines orchestrate Layer 1+2+3+5, must not import each other
---

# Layer 4 — Pipelines (비즈니스 워크플로우)

`pipelines/` 는 Layer 1+2+3+5 를 조립해 비즈니스 워크플로우를 만든다. CLI 또는 webhook receiver 가 진입점.

## 현재 파이프라인

| 파일 | 역할 | 진입점 |
|---|---|---|
| `core_engine.py` | 스케줄 → 수업·숙제 자동 생성 | `parse-schedule` |
| `ingest.py` | Webhook Cmd 4종 → Notion 적재 | `webhook_receiver` 디스패치 |
| `missing_homework.py` | 미제출 sweep | `sweep-missing-homework` |
| `exams.py` | 시험 결과 import + 미응시 sweep | `import-exam-results`, `sweep-missing-exam` |
| `weekly.py` | 주간 드래프트 + 승인 아카이브 | `generate-weekly-drafts`, `approve-weekly` |
| `daily.py` | 일일 현황 HTML 렌더 | `render-daily` |

## 절대 규칙

1. **파이프라인끼리 import 금지** — 공통 로직이 있으면 `intelligence/` 또는 `storage/` 로 끌어올림
2. 파이프라인은 다음만 import: `classin/`, `storage/`, `intelligence/`, `notify/`
3. CLI 와 webhook receiver 만 파이프라인을 import — 역참조 금지

## 새 파이프라인 추가 절차

1. `pipelines/<new>.py` 생성 — 함수형 또는 클래스. `from __future__ import annotations` 맨 위
2. 진입점:
   - 배치/수동 → `cli/main.py` 에 새 subcommand 추가
   - Webhook 트리거 → `webhook_receiver.py` 의 dispatch 맵에 등록
3. 의존하는 Layer 별 파일을 import:
   - 데이터 가져오기 → `storage/notion_repo`
   - Claude 처리 → `intelligence/<적절한 모듈>` 또는 `intelligence/claude_client`
   - 출력 → `notify/dispatcher` 또는 `storage/html_renderer`
4. 테스트: 외부 서비스 mock 없이 돌게 — `tests/test_<new>.py` 에 순수 입출력 케이스

## 트랜잭션 / 멱등성

- Webhook 재전송 정책 미확인 (`docs/11_api_integration.md` §6) → 핸들러는 **멱등** 하게 작성
- `upsert_lesson_record` 는 `lesson_id + classin_id` 유니크 키 기준 upsert
- 시험 import 는 `외부 시험 ID` 기준 멱등

## 에러 처리 경계

- 내부 함수: 그대로 throw
- 경계 (CLI handler / webhook handler): catch → 로그 + 적절한 응답
- Webhook handler 가 throw 하면 ClassIn 이 retry 할 수도 있고 안 할 수도 — 멱등성 + catch 둘 다 필수

## 자동 vs 수동 분리

- 자동 (Webhook / cron) 파이프라인: UI 없음. 로그·Notion 적재로만 존재 → 모듈명에 `interactive` 들어가면 잘못된 위치
- 수동 (`agent.py` 도구로 호출되는) 파이프라인: 즉석 결과 반환. 예: `weekly.run_for_student(student_id)` 가 단일 학생 리포트 동기 반환

## 출력 모드 분기

```yaml
output:
  daily:
    mode: "html"               # html | notion | both
  weekly:
    mode: "html+notion"        # html+notion | html | notion
    require_approval: true
  memo:
    mode: "notion"             # notion | off
```

파이프라인은 모드를 읽어 어디로 쓸지 분기. **HTML 만** 모드여도 동작해야 함 (Notion 미설정 학원).

## 관련 코드

- `src/classin_toolkit/pipelines/*.py`
- `src/classin_toolkit/cli/main.py` (subcommand 등록)
- `src/classin_toolkit/webhook_receiver.py` (dispatch 맵)

## 참고 문서

- `docs/10_architecture.md` (Layer + 데이터 흐름)
- `docs/14_developer_guide.md` §4 (확장 시나리오)
