---
name: classin-architecture
description: Use before modifying classin-toolkit code — defines the 5-layer separation, dependency direction rules, and which file to touch for which kind of change
---

# Layer 분리 아키텍처

코드 수정 전 **"이게 어느 Layer 인가?"** 부터 판단. 잘못된 위치면 리팩토링 — 바로 추가 금지.

## 5-Layer 지도

```
Layer 5  notify/        카톡 dry_run / 알리고·솔라피 릴레이
Layer 4  pipelines/     비즈니스 워크플로우 (core_engine, ingest, missing_homework, exams, weekly, daily)
Layer 3  intelligence/  Claude 프롬프트, 파서, 리포트, agent
Layer 2  storage/       Notion repository, HTML renderer, output protocol
Layer 1  classin/       ClassIn API, v1 SafeKey, v2 signing, Webhook schema
```

## 의존성 방향 (역방향 import 금지)

| Layer | import 가능 | import 금지 |
|---|---|---|
| `classin/` | pydantic, httpx 외부만 | 다른 Layer 전부 |
| `storage/` | (외부만) | `classin/`, `intelligence/` |
| `intelligence/` | `classin/webhook_schemas` (값 전달용 read-only) | `storage/` 양방향 |
| `pipelines/` | `classin/`, `storage/`, `intelligence/`, `notify/` | **다른 pipelines** |
| `notify/` | (외부만) | 모두 — 단방향 sink |
| `webhook_receiver.py` / `cli/` | `pipelines/` | 직접 다른 Layer |

**파이프라인끼리 상호 import 금지** — 공통 로직이 있으면 `intelligence/` 또는 `storage/` 쪽으로 끌어내림.

## "X 를 바꾸고 싶다" → 어느 파일

| 바꾸고 싶은 것 | 건드릴 곳 | 바뀌면 안 되는 곳 |
|---|---|---|
| ClassIn API 스펙 변경 | `classin/*` 전부 | 나머지 |
| 저장소 Notion → 다른 DB | `storage/notion_repo.py` 인터페이스 보존 | 나머지 |
| 카톡 → 이메일/SMS | `notify/dispatcher.py` + provider 추가 | 나머지 |
| 학원별 톤·정책 | `intelligence/prompts/*.md` + `config.yaml` | 코드 |
| 신규 Webhook Cmd | `classin/webhook_schemas.py` + `pipelines/ingest.py` + `webhook_receiver.py` 디스패처 | 나머지 |

## 데이터 흐름 4갈래

A. **CED 쓰기**: 스케줄 파일 → `intelligence/schedule_parser` → `pipelines/core_engine` → `classin/ced` → Notion
B. **Webhook 읽기**: ClassIn → FastAPI `/classin/webhook` → SafeKey 검증 → Cmd 디스패치 → `pipelines/ingest` → Notion
C. **배치**: cron → `missing_homework` / `exams` / `weekly` → Claude → notify
D. **출력**: `pipelines/daily` → HTML / `pipelines/weekly` → HTML 드래프트 → 승인 → Notion

상세는 `docs/10_architecture.md`.

## 자동 vs 수동 분리 원칙

- 자동 라인 (Webhook / 스케줄러): **UI 없음**. 로그·Notion 적재로만 존재
- 수동 라인 (`agent` / `ui`): 원장이 즉석으로 수치·리포트 뽑는 인터페이스
- V2 (SaaS) 전환 시 **Layer 5 만 교체** — 자동은 서버로, 수동은 웹 UI 로

## 코드 스타일 최소 규칙

1. 주석은 **WHY 만**. WHAT 은 이름으로
2. 새 모듈 추가 전 Layer 판단. 잘못된 위치면 리팩토링
3. `from __future__ import annotations` 파일 맨 위
4. 에러 처리는 **경계에서만**. 내부 함수는 throw, CLI/Webhook handler 가 catch
5. 테스트는 외부 mock 없이 돌 수 있어야 함

## 다음 스킬

- Layer 1 작업 → [`classin-api-integration`](../classin-api-integration/SKILL.md)
- Layer 2 작업 → [`classin-notion-schema`](../classin-notion-schema/SKILL.md)
- Layer 3 작업 → [`classin-intelligence-prompts`](../classin-intelligence-prompts/SKILL.md)
- Layer 4 작업 → [`classin-pipelines-guide`](../classin-pipelines-guide/SKILL.md)
- Layer 5 작업 → [`classin-notify-dispatch`](../classin-notify-dispatch/SKILL.md)

## 참고 문서

- `docs/10_architecture.md` (Layer 매핑 + 데이터 흐름 + 의존성 + 실행 토폴로지)
- `docs/02_guidelines.md` §2.1 (아키텍처 원칙 원문)
