---
name: classin-missing-homework
description: Use when running the missing-homework sweep — generates per-student katalk dry-run messages from Notion lesson records where homework wasn't submitted within the time window
---

# 미제출 sweep + 카톡 dry_run

수업 기록 DB 에서 일정 시간 내 미제출 row 를 학생별로 묶어 Claude 가 카톡 문구를 생성, dry_run 파일로 떨어뜨린다.

## When to use

- Windows 작업 스케줄러로 매 시 정각에 자동 실행
- 수동: 수업 종료 후 일정 시간 지난 시점에 한 번 더 확인하고 싶을 때

## CLI

```bash
# 기본 (config.notify.window_hours 사용)
classin-toolkit sweep-missing-homework

# 시간 창 명시
classin-toolkit sweep-missing-homework --window-hours 4

# 특정 수업만
classin-toolkit sweep-missing-homework --lesson-id LES12345
```

## 동작 (왜 단일 이벤트로 안 되는지)

ClassIn Webhook 에는 `homework_submitted: bool` 단일 이벤트가 **없다**. 여러 Cmd 를 조립해야 한다:

```
1. createUnit / createClass → activityId 확보 (Notion 저장)
2. releaseActivity 로 수업에 숙제 배정
3. Attendance 도착 → 수업 기록 DB 에 per-student row 생성
4. HomeworkSubmit 도착할 때마다 row.숙제제출=True
5. sweep: 수업일시 > now-window AND 숙제제출 != True → Claude 문구 → notify dry_run
```

즉 미제출 sweep 이 정확히 돌려면 **선행 Webhook 이 정상 수신**돼야 한다.

## 출력

`reports_out/notify_dry_run/<timestamp>__<student>.md` — 학생마다 별도 파일, 학원/교사/학생명 치환된 카톡 문구.

`config.notify.mode: live` 로 바꾸면 알리고/솔라피로 실제 발송 (Standard 티어 + 템플릿 심사 후).
UI에서는 `/api/preview-missing-homework`로 선택 대상의 AI 문구, `ready/review/blocked`, 마스킹 연락처,
live 발송 가능 여부를 먼저 확인한다. 긴 문구 목록은 번호와 접기/펴기 상세로 제한해 운영 화면이 길어지지 않게 한다.

## 문구 품질 게이트

Claude/Gemini가 만든 숙제 알림 문구는 `intelligence/notification_quality.py`에서 deterministic 검사를 거친다.
빈 문구, 보호자 연락처 없음, 낙인 표현은 `blocked`로 보고 `sweep-missing-homework` 기본 dispatch에서 제외한다.
차단된 문구는 notify history에 `provider=quality_gate`, `status=skipped`로 남겨 UI에서 사유를 확인할 수 있게 한다.
`ready` / `review` / `blocked` 상태, 점수, 경고는 미제출 목록과 알림 발송 현황에서 확인한다.
live 발송은 `ready` 상태이면서 보호자 연락처가 있는 문구만 허용한다.

## 흔한 함정

| 증상 | 원인 |
|---|---|
| 미제출인데 sweep 결과 없음 | `Attendance` Webhook 이 안 들어왔거나 학생 Master 에 ClassIn ID 매핑 누락 |
| 엉뚱한 학생 이름 | Claude 환각. `intelligence/prompts/missing_homework.md` "환각 금지" 강조 + 입력 payload 로그 확인 |
| 같은 학생에게 같은 수업 알림 반복 | 수업 기록 row 중복. `lesson_id + classin_id` 유니크 키 확인 |

## 관련 코드

- `src/classin_toolkit/pipelines/missing_homework.py`
- `src/classin_toolkit/intelligence/missing_homework.py` (Claude 문구 생성)
- `src/classin_toolkit/intelligence/notification_quality.py` (문구 품질 점검)
- `src/classin_toolkit/intelligence/prompts/missing_homework.md` (프롬프트)
- `src/classin_toolkit/notify/dispatcher.py` (dry_run 파일 출력)

## 참고 문서

- `docs/11_api_integration.md` §5.4 (MVP1 미제출 알림 구현 전략)
- `docs/13_operations_runbook.md` §3.3 ("엉뚱한 학생 이름" 대응)
