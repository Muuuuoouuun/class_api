---
name: classin-toolkit-overview
description: Use when working with the classin-toolkit repo or planning any classin-toolkit task — entry point that maps workflows to specific skills and CLI commands
---

# classin-toolkit Overview

`classin-toolkit` 은 ClassIn API + Webhook + Notion DB + Claude 분석을 묶어 학원 운영을 자동화하는 **로컬 설치형 Python toolkit** 이다.

## 핵심 사실

- 형태: SaaS 아님. 학원 PC 또는 라즈베리파이에 설치하고 학원이 직접 운영
- 데이터 주권: 학원이 처리자, MOON 은 수탁자
- 자동 라인: Webhook 수신 + 배치 sweep + 주간 리포트
- 수동 라인: `agent` CLI (자연어 질의 → tool-use 답변), `ui` (로컬 브라우저)

## 5-Layer 구조

| Layer | 디렉토리 | 책임 |
|---|---|---|
| 1 | `classin/` | ClassIn API v1/v2, 서명, Webhook 스키마 |
| 2 | `storage/` | Notion DB, HTML 렌더, 출력 Protocol |
| 3 | `intelligence/` | Claude 프롬프트, 파서, 리포트, agent |
| 4 | `pipelines/` | 비즈니스 워크플로우 |
| 5 | `notify/` | dry_run / 알리고 / 솔라피 dispatch |

상세는 [`classin-architecture`](../classin-architecture/SKILL.md) 또는 `docs/10_architecture.md`.

## 워크플로우 → 스킬 매핑

| 하고 싶은 일 | 스킬 | CLI |
|---|---|---|
| 학기 초 스케줄 일괄 등록 | `classin-schedule-import` | `parse-schedule <csv>` |
| Webhook 서버 띄우기 / 페이로드 재생 | `classin-webhook-handling` | `classin-webhook` / `replay-webhook` |
| 미제출 sweep + 카톡 dry_run | `classin-missing-homework` | `sweep-missing-homework` |
| 시험 결과 import | `classin-exam-import` | `import-exam-results` |
| 미응시자 sweep | `classin-missing-exam` | `sweep-missing-exam` |
| 주간 리포트 생성·승인 | `classin-weekly-reports` | `generate-weekly-drafts` / `approve-weekly` |
| 자연어로 원장이 질문 | `classin-agent-usage` | `agent` |
| 설치 후 사전 점검 | `classin-readiness-check` | `check-ready` / `diagnose-apis` |

## 코드 수정이 필요할 때

먼저 [`classin-architecture`](../classin-architecture/SKILL.md) 를 읽고 어느 Layer 인지 판단. 그 다음 해당 Layer 스킬 (`classin-api-integration` / `classin-notion-schema` / `classin-intelligence-prompts` / `classin-pipelines-guide` / `classin-notify-dispatch`) 진입.

## 운영 원칙 (절대 어기지 말 것)

1. **ClassIn 대시보드 + API 동시 조작 금지** — 데이터 충돌
2. **학원 PC 절전 모드 해제** — 끄면 Webhook 유실
3. **API 키는 학원이 보관** — MOON 은 세팅·유지보수 일시 접근만
4. **카톡 알림톡은 Standard 티어 + 템플릿 심사 후** — 그 전까지 dry_run

## 참고 문서

- `README.md` (빠른 시작·CLI 표)
- `AI_HANDOFF.md` (AI 에이전트 인계서)
- `docs/00_index.md` (문서 지도)
- `docs/10_architecture.md` (Layer 상세)
- `docs/02_guidelines.md` (운영 원칙 원문)
