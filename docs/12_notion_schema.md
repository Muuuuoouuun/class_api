# Notion DB 세팅 가이드

학원별 1회 세팅. **4개 DB** 를 만들고 통합 토큰 Invite → `config.yaml` 에 ID 기입.
수동 생성 대신 CLI 자동 생성을 권장한다.

```bash
classin-toolkit setup-notion --parent-page-id <NOTION_PAGE_ID> --dry-run
classin-toolkit setup-notion --parent-page-id <NOTION_PAGE_ID> --write --config config.yaml
```

원장님 입장에서는 Notion에 빈 페이지 하나를 만들고, 그 페이지를 Integration에 공유한 뒤,
페이지 ID만 전달하면 된다. `--write` 실행 후 출력되는 `notion.databases.*` 값을 `config.yaml`에 붙여넣는다.

**역할 분담** (출력 레이어 재설계, 2026-04-24):
- 학생 Master / 수업 기록 = **원본 데이터 저장소** (자동 적재)
- 리포트 = **승인된 아카이브만** (드래프트는 HTML 파일, 주간)
- 메모 = **원장 편집 채널** (수동 입력, 원장이 계속 쓰는 공간)

일일 현황은 Notion 에 쓰지 않는다 — `reports_out/daily/<date>.html` 로 생성 후 Cloudflare Tunnel 공개 URL 로 접근.

## 1. 학생 Master DB

| 속성 이름 | 타입 | 필수 | 비고 |
|---|---|---|---|
| 학생명 | Title | ✓ | |
| ClassIn ID | Text | ✓ | CED API 반환값. 수정·삭제 키값 |
| 반 | Select | | 예: 고2-A |
| 학부모 연락처 | Phone | | 카톡 알림 수신 번호 |
| 등록일 | Date | | |
| 상태 | Select | | 재원 / 휴원 / 퇴원 |

## 2. 수업 기록 DB

| 속성 이름 | 타입 | 비고 |
|---|---|---|
| 기록 | Title | Notion DB 필수 title 속성 |
| 학생 | Relation → 학생 Master | |
| 수업일시 | Date (시간포함) | |
| 출석 여부 | Select | 출석 / 지각 / 결석 |
| 실참여시간(초) | Number | Attendance.Data[].AttendanceTime |
| 손들기 횟수 | Number | End.Data.handsupEnd 파생 |
| 트로피 수 | Number | End.Data.awardEnd 파생 |
| 카메라 시간(분) | Number | End.Data.inoutEnd 파생 |
| Poll 응답 | Number | End.Data.answerEnd 파생 |
| 숙제 제출 | Checkbox | HomeworkSubmit 도착 시 true |
| 지각 제출 | Checkbox | HomeworkSubmit.Data.IsSubmitLate |
| 숙제 점수 | Number | HomeworkScore.Data.Score |
| ClassIn 숙제 ID | Text | HomeworkSubmit.Data.ActivityId |
| ClassIn 수업 ID | Text | Webhook ClassID |
| ClassIn 반 ID | Text | Webhook CourseID |

## 3. 리포트 DB (승인된 아카이브만)

지침(feedback_storage_notion, 2026-04-24): 주간 리포트는 HTML 드래프트 먼저,
컨설턴트/원장 확인 후에만 Notion 에 아카이브. 이 DB 는 "최종본 영구 보관소".

| 속성 이름 | 타입 | 비고 |
|---|---|---|
| 리포트명 | Title | Notion DB 필수 title 속성 |
| 학생 | Relation → 학생 Master | |
| 리포트 기간 | Date (기간) | |
| 학부모 발송 문구 | Text | 카톡 발송용 |
| HTML 링크 | URL | Cloudflare Tunnel 공개 URL |
| 승인됨 | Checkbox | archive 시 true |
| 발송 여부 | Checkbox | |
| 발송일시 | Date | |

리포트 본문(마크다운 섹션)은 페이지 children 블록으로 저장된다.

## 4. 메모 DB (원장 편집 채널)

`output.memo.mode = notion` 일 때 사용. 원장·교사·컨설턴트가 학생 단위 대응기록을 남기는 공간.

| 속성 이름 | 타입 | 비고 |
|---|---|---|
| 내용 | Title | 메모 본문 (최대 1,900자) |
| 학생 | Relation → 학생 Master | |
| 일자 | Date | |
| 태그 | Select | 예: 상담 / 행동 / 학습 / 건강 |

CLI: `classin-toolkit write-memo --classin-id 10001 --text "어머님과 상담, 다음 주 보강 제안" --tag 상담`

## DB ID 찾는 방법

1. Notion 에서 해당 DB 를 Full Page 로 연다.
2. URL `https://www.notion.so/{workspace}/{DB_ID}?v=...` 중 `DB_ID` 를 복사.
3. `config.yaml` `notion.databases.*` 에 붙여넣기.
