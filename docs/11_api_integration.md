# 11. ClassIn API 통합 스펙

출처: https://docs.eeo.cn/api/en/ (2026-04 기준)

## 1. 공통

- **단일 진입점**: `POST https://api.eeo.cn/partner/api/course.api.php?action=<ACTION>`
- **Content-Type**: `application/json`
- **응답 엔벨로프**:
  ```json
  { "data": <any>, "error_info": { "errno": <int>, "error": <str> }, "more_data": { /* 일부 action */ } }
  ```
  - `errno == 0`이면 성공. `data`는 action별 의미 (보통 생성된 엔티티 ID).
  - `errno != 0`이면 client 에서 `ClassInAPIError` 로 변환.
- 레거시 v1 (일부 오래된 endpoint, 예: `getLoginLinked`) 은 body에 `SID`/`safeKey`/`timeStamp` 포함. 신규 endpoint는 v2 사용 권장.

## 2. v2 서명 알고리즘

출처: `appendix/signature.html`, `appendix/sign_demo.html`
구현: [signing.py](../src/classin_toolkit/classin/signing.py)

### 2.1 헤더

| 헤더 | 값 |
|---|---|
| `X-EEO-UID`  | `sid` (School ID) |
| `X-EEO-TS`   | `timeStamp` (Unix epoch 초, 서버와 ±5분 이내) |
| `X-EEO-SIGN` | MD5 서명 (32자 lowercase) |
| `Content-Type` | `application/json` |

### 2.2 서명 문자열 구성

1. body에서 제외:
   - list / dict 값
   - UTF-8 기준 1024 bytes 초과 문자열
2. `sid`, `timeStamp` 추가 (body에는 넣지 않고 서명 계산에만 사용).
3. key를 **ASCII 오름차순** 정렬.
4. `k1=v1&k2=v2&...` 포맷 연결.
5. 끝에 `&key=<SECRET>` 부착.
6. MD5(lowercase, 32).

### 2.3 예시

```
body: { "courseName": "Math101", "teacherUid": 20001 }
sid: 123456, timeStamp: 1234567890, secret: XXXXXX

signing string:
courseName=Math101&sid=123456&teacherUid=20001&timeStamp=1234567890&key=XXXXXX

MD5 → X-EEO-SIGN 헤더
```

## 3. 핵심 Action (action=`<name>`)

### 3.1 User
- `register` — 유저 계정 생성. `data` = UID (정수)
  - body: `telephone` / `email` / `password` / `nickname` / `addToSchoolMember`
- `registerMultiple` — 다수 동시 등록
- `modifyPassword` — 비밀번호 변경
- `addSchoolStudent` — 학교 소속 학생으로 등록 (uid, nickname, studentNumber)
- `addTeacher` / `editTeacher` / `stopUsingTeacher` / `restartUsingTeacher`
- `updateClassStudentComment` — 교사의 학생 평가

### 3.2 Classroom
- `addCourse` — 반(Course) 생성. `data` = courseId
  - body: `courseName`, `teacherUid` (단일 교사는 이 필드, 다중은 addCourseTeacher 로 추가)
- `addCourseClass` — 개별 수업 생성. `data` = classId
  - body: `courseId`, `className`, `beginTime`, `endTime`, `teacherUid`
- `addCourseClassMultiple` — 반복 일정 일괄 생성
- `addCourseStudentMultiple` — 반에 학생 여러 명 추가 (`courseId`, `uidList`)
- `addClassStudentMultiple` — 특정 수업에만 학생 다수 추가 (`classId`, `uidList`)
- `delCourseStudent` / `delCourseClass` / `endCourse`
- `editSchoolSettings` — 학교 설정

### 3.3 LMS (숙제·시험)
- `createUnit` → `createClassroom` → `createActivityNoClass` → `releaseActivity` 의 체인 패턴.
- `addStudent` / `deleteStudent` — 활동 대상 학생 관리.

### 3.4 SSO
- `getLoginLinked` (v1 스타일, body에 `safeKey`)
  - body: `SID`, `safeKey`, `timeStamp`, `uid`, `telephone`, `courseId`, `classId`, `deviceType`(1=Win/Mac, 2=iOS, 3=Android), `lifeTime`(초, 기본 86400)
  - 응답 `data` = 호출 URL
    - PC: `classin://...`
    - 모바일: `https://...`
  - 구현: [sso.py](../src/classin_toolkit/classin/sso.py)

## 4. Datasub Webhook

### 4.1 운영 제약 (지침 02 §1.2 재확인)

- **1기관 1 엔드포인트**. 엔드포인트 변경 시 ClassIn에 재등록 필요.
- **Real-time 사용 금지**. After-Class 만 안정 (지연 없이 20분 내 도착).
- 담당자 이메일 등록 → 실패 시 즉시 통지.

### 4.2 공통 필드

모든 이벤트 body 최상위 (Cmd별 존재 여부 상이):

| 필드 | 설명 |
|---|---|
| `SID` | School ID |
| `Cmd` | 이벤트 디스크리미네이터 |
| `ClassID` | Lesson(수업) ID |
| `CourseID` | Course(반) ID — 일부 Cmd 누락 |
| `ActionTime` | 이벤트 발생 시각 (초) |
| `TimeStamp` | 전송 시각 (초) |
| `SafeKey` | 수신자 서명 검증 |
| `Data` | Cmd별 페이로드 |

### 4.3 Cmd 목록

#### Class-related (출처: `datasub/classrelated.html`)

| Cmd | 용도 | MVP 사용 |
|---|---|---|
| `Attendance`   | 출석 종합 (per-student Uid/Name/Identity/AttendanceTime/FirstInTime/LastOutTime) | ✅ 필수 |
| `End`          | 수업 종료 요약. `Data`에 `handsupEnd/awardEnd/inoutEnd/answerEnd/...` 20+ 중첩 | ✅ 보조 |
| `Rating`       | 교사↔학생 상호 평가 | ❌ |
| `Record`       | 녹화 파일 생성 완료 (`VUrl`, `Duration`, `FileId`) | 선택 |
| `Upload`       | 업로드 녹화 완료 | 선택 |
| `EduDt`        | QR시험/클라이언트시험 결과 (`questionList[].studentAnswers`) | 선택 |
| `ReplayDataDetail` | 웹 복습 시청 | ❌ |
| `ClientPlaybackDataDetail` | 클라이언트 복습 시청 | ❌ |
| `ChatContent`  | 채팅 로그 zip URL | ❌ |

#### LMS (출처: `datasub/coursedata.html`)

| Cmd | 용도 | MVP 사용 |
|---|---|---|
| `HomeworkSubmit` | 학생 제출 시마다 (UnitId, ActivityId, StudentInfo, SubmissionTime, IsSubmitLate) | ✅ 필수 |
| `HomeworkScore`  | 채점 완료 (Score, ReviewDetails) | ✅ |
| `ExamScore`      | 시험 점수 | ❌ |
| `AnswerSheetScore` | 답안지 점수 | ❌ |
| `DiscussionChangeInfo` | 댓글/답글/좋아요 실시간 | ❌ |

### 4.4 MVP1 "미제출 알림" 구현 전략

**핵심**: 단일 Webhook 이벤트에 `homework_submitted: bool`이 없다. 여러 Cmd를 조립해야 함.

1. `addCourseClass` 로 수업 생성 → `classId` 확보 (Notion 저장).
2. LMS `releaseActivity` 로 해당 수업에 숙제 배정 → `activityId` 확보.
3. 수업 종료 시 `Attendance` 이벤트 수신 → 수업 기록 DB 에 per-student row 생성.
4. 이후 `HomeworkSubmit` 이벤트가 들어올 때마다 해당 row의 `숙제 제출 = True`.
5. 스케줄러가 `sweep_missing_homework` 실행 — `수업일시 > now - window AND 숙제 제출 != True` 인 row를 학생별 grouping 하여 Claude 문구 생성 → 발송.

## 5. 현재 코드의 확정/추정 매트릭스

| 항목 | 상태 | 근거 |
|---|---|---|
| v2 서명 알고리즘 | ✅ 확정 | signature.html + sign_demo.html 3종 코드 예시 일치 |
| 엔드포인트 entrypoint | ✅ 확정 | `course.api.php?action=...` 다수 페이지 재확인 |
| `register` 응답 UID 위치 (`data`) | ✅ 확정 | register.html 명시 |
| `addCourseClass` 필드명 (`beginTime`/`endTime`/`teacherUid`) | ✅ 확정 | addCourseClass.html 명시 |
| `Attendance` 필드명 (`Data[].Uid/AttendanceTime/FirstInTime/LastOutTime`) | ✅ 확정 | classrelated.html |
| `HomeworkSubmit` 필드명 (`Data.ActivityId/StudentInfo.Uid/IsSubmitLate`) | ✅ 확정 | coursedata.html |
| `End` 내부 `handsupEnd/awardEnd/inoutEnd` 키 구조 | ⚠️ 추정 | 카테고리 존재는 확정, 세부 키는 샘플 페이로드 확인 필요 |
| `SafeKey` 계산식 | ⚠️ 추정 | 현재 `MD5(SID+TimeStamp+secret)`. `datasub/publicfield.html` 확인 필요 |
| LMS 숙제 활동 생성 체인 (Unit→Classroom→Activity) 실 파라미터 | ⚠️ 추정 | 스키마 대략만 확보 |
| Webhook 재전송 정책 | ❌ 미확인 | ClassIn 담당자 확인 필요 |

`⚠️` 항목은 실 샘플 페이로드로 alias 조정하면 바로 확정 가능.
`❌` 항목은 ClassIn 문의가 필요.

## 6. 테스트 전략

- **단위**: `tests/test_signing_v2.py` — v2 서명 정렬·필터링·해시
- **파싱**: `tests/test_webhook_schema.py` — 3종 샘플 (Attendance/End/HomeworkSubmit) 디스크리미네이트
- **통합 (추후)**: `respx` mock으로 `ClassInClient.call` 동작 검증 — HTTP 호출 없이 서명·엔벨로프 처리까지.
- **Webhook e2e**: `classin-toolkit replay-webhook samples/attendance_sample.json` — 실 Notion 연동으로 적재 확인.
