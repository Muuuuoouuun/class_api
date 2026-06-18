---
name: classin-notify-dispatch
description: Use when modifying the notification layer or switching from dry_run to live (Aligo/Solapi katalk relay) — Layer 5 is the swap point for local→web migration, must stay decoupled
---

# Layer 5 — Notify Dispatch

카톡 dry_run → 알리고/솔라피 라이브 발송. **Layer 5 만 교체하면 출력 매체를 바꿀 수 있다** (카톡 → 이메일 → SMS → 웹 알림).

## 현재 상태

- `notify.mode: dry_run` (기본) — `reports_out/notify_dry_run/<timestamp>__<student>.md` 파일로 출력
- `notify.mode: live` — `_send_via_aligo`가 승인 템플릿 코드·senderkey·품질 ready 메시지에 한해 전송
- notify history에는 발송 상태와 함께 `quality_status`, `quality_score`, `quality_warnings`를 남긴다.
  품질 게이트에서 막힌 문구는 `provider=quality_gate`, `status=skipped`로 기록된다.

MVP 단계는 dry_run 까지만 완성. 실제 발송은 카카오 알림톡 템플릿 심사 (2~3주) + Standard 티어 후.

## live 전환 절차

1. 알림톡 템플릿 코드 + 발신프로필 `sender_key` 확보
2. 알림톡 템플릿 ID + 파라미터 placeholder 매핑
3. `intelligence/prompts/missing_homework.md` 등에 **템플릿 변수명·순서 고정** 반영 — 템플릿 심사된 변수 외 자유 텍스트 금지
4. `config.yaml`:
   ```yaml
   notify:
     mode: live
     provider: aligo            # aligo | solapi
     aligo:
       api_key: "..."
     sender_id: "..."
     sender_key: "..."
     template_code_missing_homework: "..."
   ```
5. `kakao-live` 모드 [`classin-readiness-check`](../classin-readiness-check/SKILL.md) 통과 확인

## 알림 유형 (지침 02 §4.2)

| 알림 | 트리거 | 수신자 |
|---|---|---|
| 숙제 미제출 | After-Class 데이터 수신 후 sweep | 학생·학부모 |
| 지각/결석 | 출석 데이터 이상 감지 | 학부모 |
| 주간 리포트 | 매주 금 자동 | 학부모 |
| 장기 결석 경고 | 3주 연속 결석 패턴 | 원장·학부모 |
| 상담 일정 확인 | 상담 전날 자동 | 학부모 |

## 문구 작성 원칙

- Claude 가 생성 (Layer 3) → notify 가 발송 (Layer 5) — 분리 유지
- 학원명·교사명·학생명 **치환 변수**
- 미제출 알림은 부드럽게, 반복 미제출은 단계적으로 톤 강화
- 너무 딱딱하지 않게 — 학원 카톡 = 학부모와 관계 유지 도구

## 새 provider 추가

1. `dispatcher.py` 에 `_send_via_<provider>` 함수 추가
2. `notify.provider` 분기 추가
3. config schema 에 새 키 추가
4. dry_run / live 둘 다 같은 인터페이스 (입력 = 학생/문구/메타, 출력 = 발송 ID 또는 dry-run 파일 경로)

## 절대 규칙

1. **다른 Layer 가 notify 를 import 하면 안 됨** — 단방향 sink
2. **dry_run 출력은 파일** — Notion 도 코드도 건드리지 않음. 그래야 미발송 검증 안전
3. **카톡 템플릿 심사 전 live 전환 금지** — 알림톡 정책 위반

## kakao-live readiness

`kakao-live` 모드는 알리고 키, 발신번호, `sender_key`, 숙제 미제출 템플릿 코드가 모두 채워져야 통과한다.

## 관련 코드

- `src/classin_toolkit/notify/dispatcher.py`
- `src/classin_toolkit/notify/message.py` (메시지 객체)

## 참고 문서

- `docs/02_guidelines.md` §4 (카톡 알림 운영 지침)
- `docs/14_developer_guide.md` §4.5 (live 연동 절차)
- `docs/13_operations_runbook.md` §2 (학원 고지사항)
