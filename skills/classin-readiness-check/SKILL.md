---
name: classin-readiness-check
description: Use when verifying classin-toolkit setup before running anything — check-ready validates config completeness per stage, diagnose-apis runs non-destructive probes against ClassIn/Notion/Claude/Aligo
---

# 사전 점검 — `check-ready` / `diagnose-apis`

운영 전 config / API / DB 가 모드별로 충분히 채워졌는지 확인한다. 키 값은 마스킹 출력. **실제 수업·Notion row·카톡 메시지를 만들지 않는다.**

## When to use

- 학원 PC 첫 설치 직후
- `config.yaml` 변경 후
- 장애 의심 시 (어디가 빠졌는지부터)

## 두 명령의 차이

| 명령 | 동작 |
|---|---|
| `check-ready --mode <m>` | offline. config 의 키·DB ID 가 모드별로 채워졌는지만 확인 |
| `diagnose-apis` | offline 표만 출력 |
| `diagnose-apis --live` | ClassIn / Notion / Claude / Aligo 에 비파괴 probe 실행 |

## 모드

| 모드 | 목적 | 필요 외부 API |
|---|---|---|
| `local-demo` | 샘플 Webhook 재생 + Notion/Claude/HTML 검증 | Notion, Claude |
| `classin-live` | 실제 ClassIn Webhook/CED 검증 | + ClassIn, Cloudflare Tunnel |
| `kakao-live` | 실제 알림톡 발송 전 최종 점검 | + 알리고/솔라피 |

## CLI

```bash
classin-toolkit check-ready --mode local-demo
classin-toolkit check-ready --mode classin-live
classin-toolkit check-ready --mode kakao-live

classin-toolkit diagnose-apis           # offline
classin-toolkit diagnose-apis --live    # 실제 probe
```

## 결과 해석

- `MISSING` / `BLOCKED` — 해당 모드 미준비. 진행 금지
- `WARN` — 즉시 막히진 않지만 운영 전 확인 필요
- `OK` — 통과

## `--live` probe 판정 기준

| 대상 | 판정 |
|---|---|
| ClassIn v1 | 더미 SSO 요청이 **파라미터 오류**로 거절 → 서버 도달 OK |
| ClassIn v2 LMS | 빈 `createUnit` payload 가 **검증 오류** (서명 오류 아님) 로 거절 → signing path OK |
| Notion | 각 DB ID 를 `retrieve` 로 읽어 Integration 공유 확인 |
| Claude | 짧은 `messages.create` 로 key/billing/model 확인 |
| Aligo | `heartinfo` 잔여건수 조회만 (실제 발송 X) |

`签名异常` / signature 에러는 v2 secret 또는 LMS API 권한 확인 대상.

## local-demo 최소 준비물

- Notion Integration token + DB ID 4개 (학생 Master, 수업 기록, 리포트, 메모)
- Claude API key
- `samples/{attendance,end_summary,homework_submit}_sample.json`
- `notify.mode: dry_run`

ClassIn SID / secret 은 local-demo 에서 **없어도 됨** — 샘플 JSON 을 `replay-webhook` 으로 재생하기 때문.

## kakao-live 주의

현재 코드는 dry-run 까지만 완성. 알리고 키가 채워져도 `notify.dispatcher` live 구현 전까지 `BLOCKED`.

## 관련 코드

- `src/classin_toolkit/readiness.py`
- `src/classin_toolkit/api_diagnostics.py`

## 참고 문서

- `docs/17_test_readiness.md` (전체 절차)
