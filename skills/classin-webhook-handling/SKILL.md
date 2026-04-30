---
name: classin-webhook-handling
description: Use when running the ClassIn webhook receiver, replaying past payloads for debugging, or troubleshooting "Webhook이 안 들어옵니다" — covers Cloudflare Tunnel, SafeKey verification, Cmd dispatch
---

# ClassIn Webhook 수신 / 재생

ClassIn Datasub Webhook 을 로컬에서 받고, 페이로드를 Notion 수업 기록 DB 에 적재한다.

## When to use

- 수신 서버 상시 구동 (자동 라인의 핵심)
- 과거 페이로드 재처리 / 파싱 디버깅
- "Notion 에 오늘자 row 가 없어요" 장애 대응

## CLI

```bash
# 수신 서버 (학원 PC 에서 상시 구동)
classin-webhook                                       # uvicorn :8787

# 디버깅: 저장된 페이로드 재생
classin-toolkit replay-webhook samples/attendance_sample.json
classin-toolkit replay-webhook samples/end_summary_sample.json
classin-toolkit replay-webhook samples/homework_submit_sample.json
```

## 운영 제약 (절대)

1. **1기관 1 엔드포인트만 허용** — 변경 시 ClassIn 재등록
2. **Real-time 사용 금지** — After-Class + LMS Datasub 만
3. **담당자 이메일 등록 필수** — 실패 즉시 통지

## Cmd 디스패치 (MVP 사용)

| Cmd | 핸들러 | Notion 효과 |
|---|---|---|
| `Attendance` | `ingest.ingest_attendance` | upsert_lesson_record (per-student row) |
| `End` | `ingest.ingest_end_summary` | patch_lesson_record (camera/handsup/...) |
| `HomeworkSubmit` | `ingest.ingest_homework_submit` | patch_lesson_record (hw=True, late?) |
| `HomeworkScore` | `ingest.ingest_homework_score` | patch_lesson_record (score) |

`ingest` 핸들러는 Webhook `SafeKey` 검증 통과 후에만 호출됨.

## "Webhook 이 안 들어와요" 플레이북

1. `cloudflared tunnel info <name>` — connection active?
2. `curl http://localhost:8787/health` — `{"ok":true}` ?
3. 공개 URL `/health` 브라우저 접근 → 200?
4. `samples/incoming/` 에 오늘자 dump 파일 존재? — 있으면 파싱 실패, 없으면 수신 자체 안 됨
5. ClassIn 담당자에게 재전송 정책 + 에러 이메일 확인 요청

## 신규 Cmd 추가 (개발자)

[`classin-api-integration`](../classin-api-integration/SKILL.md) 참고.

## SafeKey 검증

`MD5(SECRET + TimeStamp)`. `signing.py` 에 구현. 시스템 시간 ± 5분 이내여야 통과.

## 관련 코드

- `src/classin_toolkit/webhook_receiver.py` (FastAPI dispatcher)
- `src/classin_toolkit/classin/webhook_schemas.py` (Cmd discriminated union)
- `src/classin_toolkit/pipelines/ingest.py` (per-Cmd handlers)
- `src/classin_toolkit/classin/signing.py` (SafeKey)

## 참고 문서

- `docs/11_api_integration.md` §5 (Datasub Webhook 전체)
- `docs/13_operations_runbook.md` §3.1 (장애 대응)
