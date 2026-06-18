# 로컬 ClassIn 양방향 브링업 체크리스트

목표: **로컬에서 ClassIn API 를 양방향으로 주고받으며 본래 서비스 기능을 전부 수행** —
이제 `data sub`(ClassIn Datasub 등록)만 연결하면 끝. 스펙: [docs/11 §5](docs/11_api_integration.md), 운영: [docs/13 §1](docs/13_operations_runbook.md).

## 0. 현재 상태 (2026-06-16)

| 영역 | 상태 | 비고 |
|---|---|---|
| 패키지 설치 (`.venv`) | ✅ | `classin-toolkit` / `classin-webhook` 동작 |
| ClassIn **보내기**(outbound) | ✅ 라이브 검증 | `diagnose-apis --live`: v1 SSO + v2 LMS 서명 수락 |
| ClassIn **받기**(inbound webhook) | ✅ 코드+라이브 | errno:1 ack 수정·테스트, SafeKey 검증, 5 Cmd replay→Notion 적재 |
| Notion (token + DB 5종) | ✅ 라이브 | `api-test` 페이지에 자동 생성, 토큰 라이브 OK |
| LLM = **Gemini** | ✅ 라이브 | `gemini-3.5-flash`, generate 실동작 OK (diagnose probe 예산 8→512 수정) |
| **서비스 기능 전체** | ✅ 라이브 검증 | ingest 5종 / render-daily / weekly-drafts / missing-homework / exam-import / missing-exam — 전부 Gemini, dry-run 알림 6건 생성 |
| agent 챗(수동 라인) | ⚠️ anthropic 키 필요 | provider 무관 디커플 완료 — Gemini 단독이면 비활성(자동 라인엔 영향 없음) |
| 공개 노출(tunnel) | ✅ quick tunnel 라이브 검증 | `cloudflared` 설치, CF 엣지(icn05) 등록 확인. 운영=named tunnel(고정 URL) |
| 테스트/품질 | ✅ | pytest **108 통과**, ruff 클린 |
| **ClassIn Datasub 등록** | ⛔ = "data sub 연결" | 아래 §A·§B — 유일하게 남은 외부 단계 |
| kakao **LIVE** 발송 | ⛔ 별도 미래 단계 | dry-run 동작. live = Aligo 키 + 카톡 템플릿 승인 후 (data-sub 와 무관) |

> **즉 "data sub 만 연결되면 모든게 다" 상태 도달.** 로컬 파이프라인·LLM·Notion·리포트·알림(dry-run)이 전부 검증됨.
> 남은 건 공개 URL 고정(§A) + ClassIn 등록(§B) 두 외부 단계뿐.

## A. 고정 공개 URL 세우기 — data sub 연결 ①

quick tunnel(`scripts/tunnel-quick.sh`)은 URL 이 매번 바뀌어 등록에 부적합. **운영은 named tunnel(고정 hostname)**:

```bash
cloudflared tunnel login                              # 브라우저 — 본인 Cloudflare 계정
cloudflared tunnel create classin-academy             # → TUNNEL_ID
cp scripts/cloudflared-config.example.yml ~/.cloudflared/config.yml   # <...> 값 치환
cloudflared tunnel route dns classin-academy webhook.<도메인>
scripts/serve-webhook.sh                              # 터미널 A: 수신기 :8787 (상시)
cloudflared tunnel run classin-academy                # 터미널 B: 터널 (상시)
# 외부 확인: https://webhook.<도메인>/health → {"ok":true,"school":"실험용 테스트"}
```

학원 PC 상시 구동(작업 스케줄러/절전 해제)은 [docs/13 §1.3](docs/13_operations_runbook.md).

## B. ClassIn Datasub 등록 — data sub 연결 ②

허브 UI 설정 탭의 **파일럿 브링업** 패널에서 아래 신청 메일과 실행 명령을 Markdown으로 생성/복사할 수 있다.
보안상 SID/secret/전화번호 원문은 자동 삽입하지 않으므로 발송 직전에 직접 확인한다.

`an.vu@classin.com` / `anh.nguyen@classin.com` 로 신청:

| 항목 | 값 |
|---|---|
| 1. School ID (SID) | `87372676` |
| 2. Datasub | `AnswerSheetScore`, `ExamScore`, `EduDt`, `HomeworkSubmit`, `HomeworkScore`, `Attendance`, `End` |
| 3. Endpoint URL | `https://webhook.<도메인>/classin/webhook` (FastAPI — `/api` 접두사 없음) |
| 4. 에러 통지 이메일 | (본인 이메일) |

- 등록 중 ClassIn 의 `Cmd:Test` → 이미 `errno:1` 정상 ack 검증됨.
- **1기관 1엔드포인트**: 등록 후 URL 변경 시 재등록 필요 → 그래서 §A 고정 URL 사용.
- Datasub 는 **등록 이후 이벤트만** push (과거 데이터 백필 안 됨).

## C. 검증 명령 (언제든 재확인)

```bash
.venv/bin/classin-toolkit check-ready --mode classin-live   # data-sub 모드 준비도 → 막는 항목 없음
.venv/bin/classin-toolkit diagnose-apis --live              # ClassIn/Notion/Gemini OK (Aligo만 미래단계)
# 받기 E2E: 5개 Cmd 적재
for s in attendance end_summary homework_submit answer_sheet_score; do
  .venv/bin/classin-toolkit replay-webhook samples/${s}_sample.json; done
.venv/bin/classin-toolkit render-daily                      # 일일 HTML
.venv/bin/classin-toolkit generate-weekly-drafts            # 주간 HTML (Gemini)
.venv/bin/classin-toolkit sweep-missing-homework --window-hours 504   # 미제출 dry-run (Gemini)
```

## D. 옵션 — 켜고 싶을 때

- **agent 챗**(원장/교사 AI 어시스턴트): `config.yaml` `anthropic.api_key` 에 `sk-ant-...` 입력 → 즉시 동작. (provider 가 gemini 여도 agent 챗만 anthropic tool-use 사용. 자동 라인은 계속 Gemini.)
- **kakao LIVE 발송**: 카톡 알림톡 템플릿 승인 + `notify.aligo.*` 키 입력 + `notify.mode: live`. (현재는 dry-run 으로 `reports_out/notify_dry_run/` 에 문구 적재.)
