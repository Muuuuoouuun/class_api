# Notion DB 세팅 가이드

학원별 1회 세팅. 모든 DB 에 통합 토큰을 Invite 하고, DB ID 를 `config.yaml` 에 기입한다.

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
| 학생 | Relation → 학생 Master | |
| 수업일시 | Date (시간포함) | |
| 출석 여부 | Select | 출석 / 지각 / 결석 |
| 손들기 횟수 | Number | After-Class |
| 트로피 수 | Number | After-Class |
| 카메라 시간(분) | Number | After-Class |
| Poll 응답 | Number | After-Class |
| 숙제 제출 | Checkbox | |
| ClassIn 수업 ID | Text | Webhook lessonId |
| ClassIn 반 ID | Text | Webhook courseId |

## 3. 리포트 DB

| 속성 이름 | 타입 | 비고 |
|---|---|---|
| 학생 | Relation → 학생 Master | Title 로 두어도 OK |
| 리포트 기간 | Date (기간) | |
| 학부모 발송 문구 | Text | 카톡 발송용 |
| 발송 여부 | Checkbox | |
| 발송일시 | Date | |

리포트 본문은 페이지 children 으로 저장된다 (Notion 블록).

## DB ID 찾는 방법

1. Notion 에서 해당 DB 를 Full Page 로 연다.
2. URL `https://www.notion.so/{workspace}/{DB_ID}?v=...` 중 `DB_ID` 를 복사.
3. `config.yaml` `notion.databases.*` 에 붙여넣기.
