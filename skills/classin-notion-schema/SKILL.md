---
name: classin-notion-schema
description: Use when modifying Notion DB schemas or notion_repo.py — covers the 5 DBs (학생 Master / 수업 기록 / 리포트 / 메모 / 시험), their columns, and the procedure to add a new Notion property
---

# Layer 2 — Notion 스키마 / Storage

학원별 1회 세팅. **5개 DB**. 통합 토큰 Invite → `config.yaml` 에 ID 기입. `storage/notion_repo.py` 인터페이스를 보존하면 저장소를 다른 DB 로 갈아끼울 수 있다.

## DB 역할 분담 (2026-04-24 출력 레이어 재설계 기준)

| DB | 역할 | 쓰기 주체 |
|---|---|---|
| 학생 Master | 원본 데이터 저장소 | 자동 적재 + 학원 수동 |
| 수업 기록 | 원본 (Webhook 자동 적재) | 자동 |
| 리포트 | **승인된 아카이브만** | 사람 승인 후 |
| 메모 | 원장 편집 채널 | 사람 수동 |
| 시험 | 외부 시험·학원 DB 병합 허브 | import CLI |

**일일 현황은 Notion 에 안 씀** — `reports_out/daily/<date>.html` 로 생성 후 Cloudflare Tunnel public URL 로 접근.

## 1. 학생 Master DB

| 컬럼 | 타입 | 필수 | 비고 |
|---|---|---|---|
| 학생명 | Title | ✓ | |
| ClassIn ID | Text | ✓ | CED API 반환값. 수정·삭제 키 |
| 반 | Select | | 예: 고2-A |
| 학부모 연락처 | Phone | | 카톡 수신 번호 |
| 등록일 | Date | | |
| 상태 | Select | | 재원 / 휴원 / 퇴원 |

## 2. 수업 기록 DB

| 컬럼 | 타입 | 비고 |
|---|---|---|
| 학생 | Relation → 학생 Master | |
| 수업일시 | Date (시간포함) | |
| 출석 여부 | Select | 출석/지각/결석 |
| 실참여시간(초) | Number | `Attendance.Data[].AttendanceTime` |
| 손들기 횟수 | Number | `End.Data.handsupEnd` 파생 |
| 트로피 수 | Number | `End.Data.awardEnd` |
| 카메라 시간(분) | Number | `End.Data.inoutEnd` |
| Poll 응답 | Number | `End.Data.answerEnd` |
| 숙제 제출 | Checkbox | `HomeworkSubmit` 도착 시 true |
| 지각 제출 | Checkbox | `HomeworkSubmit.Data.IsSubmitLate` |
| 숙제 점수 | Number | `HomeworkScore.Data.Score` |
| ClassIn 숙제 ID | Text | `HomeworkSubmit.Data.ActivityId` |
| ClassIn 수업 ID | Text | Webhook `ClassID` |
| ClassIn 반 ID | Text | Webhook `CourseID` |

## 3. 리포트 DB (승인된 아카이브만)

지침 (2026-04-24): 주간 리포트는 **HTML 드래프트 먼저, 컨설턴트/원장 확인 후에만 Notion 아카이브**.

| 컬럼 | 타입 | 비고 |
|---|---|---|
| 학생 | Relation | |
| 리포트 기간 | Date (기간) | |
| 학부모 발송 문구 | Text | 카톡 발송용 |
| HTML 링크 | URL | Cloudflare Tunnel public URL |
| 승인됨 | Checkbox | archive 시 true |
| 발송 여부 | Checkbox | |
| 발송일시 | Date | |

리포트 본문 마크다운 섹션은 **페이지 children 블록** 으로 저장.

## 4. 시험 DB

외부 학원 DB / CSV / 별도 시험 API 결과를 학생 Master 와 병합.

| 컬럼 | 타입 | 비고 |
|---|---|---|
| 시험명 | Title | 예: 4월 월말평가 |
| 학생 | Relation | |
| 시험일 | Date | |
| 반 | Text | 스냅샷 |
| 과목 | Text | 선택 |
| 응시 여부 | Checkbox | |
| 원점수 / 만점 / 백분율 | Number | 백분율 = `원점수/만점*100` |
| 데이터 출처 | Text | `academy-db` / `csv-import` / 등 |
| 외부 시험 ID | Text | 멱등 키 |

## 5. 메모 DB

| 컬럼 | 타입 | 비고 |
|---|---|---|
| 내용 | Title | 최대 1,900자 |
| 학생 | Relation | |
| 일자 | Date | |
| 태그 | Select | 상담/행동/학습/건강 |

CLI: `classin-toolkit write-memo --classin-id 10001 --text "..." --tag 상담`

## 새 컬럼 추가 절차

1. Notion DB 에 컬럼 추가 (정확한 이름 일치 주의 — 한글 공백·특수문자 그대로)
2. `storage/notion_repo.py` 상단 `PROP_*` 상수 추가
3. `upsert_lesson_record` / `patch_lesson_record` 에 파라미터 + props 매핑 추가
4. `_row_summary` 반환에도 필드 추가 (쿼리 결과 노출)
5. 영향받는 파이프라인 (`ingest`, `missing_homework`, `weekly` 등) 업데이트

`PROP_` 상수가 없는 raw string 으로 컬럼명을 박지 말 것.

## DB ID 찾는 법

1. Notion 에서 해당 DB 를 Full Page 로 열기
2. URL `https://www.notion.so/{workspace}/{DB_ID}?v=...` 의 `DB_ID` 복사
3. `config.yaml` `notion.databases.*` 에 붙여넣기

## 인터페이스 보존 원칙

저장소를 다른 DB 로 갈아끼우려면 `notion_repo.py` 의 public 메서드 시그니처 (`upsert_lesson_record`, `patch_lesson_record`, `archive_approved_weekly_report`, ...) 를 **보존**해야 한다. 시그니처 변경은 다른 Layer 영향 — PR 에서 명시.

## 관련 코드

- `src/classin_toolkit/storage/notion_repo.py`
- `src/classin_toolkit/storage/output_port.py` (Protocol)
- `src/classin_toolkit/storage/html_renderer.py`
- `src/classin_toolkit/storage/templates/`

## 참고 문서

- `docs/12_notion_schema.md` (DB 스키마 원문)
- `docs/14_developer_guide.md` §4.3 (컬럼 추가 절차)
