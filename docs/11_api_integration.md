# 11. ClassIn API 통합 스펙

출처: https://docs.eeo.cn/api/en/ (2026-04 기준)

## 1. 공통

- **root_url**: 공식 문서의 `root_url` 은 `api.eeo.cn` 으로 치환한다.
- **v1/v2 공존**:
  - v1: `POST https://api.eeo.cn/partner/api/course.api.php?action=<ACTION>`
  - v2: `POST https://api.eeo.cn/lms/...`
  - 2026-04 기준 LMS 디렉터리 API는 v2, `register`/`addCourse` 등 기존 사용자·교실 API는 v1이다.
- **v1 응답 엔벨로프**:
  ```json
  { "data": <any>, "error_info": { "errno": <int>, "error": <str> }, "more_data": { /* 일부 action */ } }
  ```
  - `errno == 1`이면 성공. `data`는 action별 의미 (보통 생성된 엔티티 ID).
  - bulk v1 API는 최상위 성공 후 `data[]` 안에 항목별 `errno/error` 를 담기도 한다.
- **v2 응답 엔벨로프**:
  ```json
  { "code": 1, "msg": "程序正常执行", "data": <any> }
  ```
  - `code == 1`이면 성공.

## 2. v1 SafeKey

출처: `appendix/Gettingstartedguide.html`
구현: [signing.py](../src/classin_toolkit/classin/signing.py)

- body/form 에 `SID`, `safeKey`, `timeStamp` 를 포함한다.
- `safeKey = MD5(SECRET + timeStamp)` (32자 lowercase)
- `timeStamp` 는 현재 호출 시점 20분 이내의 Unix epoch 초.
- `studentJson`, `classJson` 등 배열/객체 파라미터는 form 필드 안의 JSON 문자열로 전송한다.

## 3. v2 서명 알고리즘

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

## 4. 핵심 API

### 4.1 User (v1)
- `register` — 유저 계정 생성. `data` = UID (정수)
  - body: `telephone` / `email` / `password` / `nickname` / `addToSchoolMember`
  - 이미 가입된 전화/이메일은 `135`/`461` 과 함께 UID 가 내려올 수 있으므로 idempotent 처리 가능.
- `registerMultiple` — 다수 동시 등록
- `modifyPassword` — 비밀번호 변경
- `addSchoolStudent` — 학교 소속 학생으로 등록 (`studentAccount` 또는 `studentUid`, `studentName`)
- `addTeacher` — 학교 소속 교사로 등록 (`teacherAccount`, `teacherName`)
- `editTeacher` / `stopUsingTeacher` / `restartUsingTeacher`
- `updateClassStudentComment` — 교사의 학생 평가

### 4.2 Classroom (v1)
- `addCourse` — 반(Course) 생성. `data` = courseId
  - body: `courseName`, 선택 `mainTeacherUid`
- `addCourseClass` — 레거시 개별 수업 생성. `data` = classId
  - body: `courseId`, `className`, `beginTime`, `endTime`, `teacherUid`
  - 문서상 2025-05-26 이후 업데이트 중지. 신규 연동은 LMS `createClass` 권장.
- `addCourseClassMultiple` — 반복 일정 일괄 생성
- `addCourseStudentMultiple` — 반에 학생 여러 명 추가 (`courseId`, `identity`, `studentJson`)
- `addClassStudentMultiple` — 특정 수업에만 학생 다수 추가 (`courseId`, `classId`, `identity`, `studentJson`)
- `delCourseStudent` / `delCourseClass` / `endCourse`
- `editSchoolSettings` — 학교 설정

### 4.3 LMS (v2)
- `POST /lms/unit/create` — 단원 생성. `data.unitId`
- `POST /lms/activity/createClass` — 권장课堂 활동 생성. `data.activityId`, `data.classId`
- `POST /lms/activity/createActivityNoClass` — 숙제/퀴즈/자료 등 비课堂 활동 초안 생성. `activityType=2` 가 숙제.
- `POST /lms/activity/release` — 활동 게시. 실 API 확인 결과 단일 `activityId` 필드 필요. 복수 게시도 client에서 단일 호출을 반복한다.
- `POST /lms/activity/addStudent` / `deleteStudent` — 활동 대상 학생 관리.

### 4.4 SSO (v1)
- `getLoginLinked`
  - body: `uid`, `telephone`, `courseId`, `classId`, `deviceType`(1=Win/Mac, 2=iOS, 3=Android), `lifeTime`(초, 기본 86400)
  - `SID`, `safeKey`, `timeStamp` 는 [client.py](../src/classin_toolkit/classin/client.py) 가 자동 추가.
  - 응답 `data` = 호출 URL
    - PC: `classin://...`
    - 모바일: `https://...`
  - 구현: [sso.py](../src/classin_toolkit/classin/sso.py)

### 4.5 스케줄 생성 설정

`classin-toolkit parse-schedule --live` 는 기본적으로 LMS classroom API를 사용한다.

```yaml
classin:
  schedule_api: "lms"       # lms | legacy
  teacher_uids:
    "김선생": "20001"       # 스케줄 parser 의 teacher_name → ClassIn teacherUid
  default_teacher_uid: ""   # teacher_uids 매칭 실패 시 fallback
```

- `schedule_api: lms`: `addCourse` → `createUnit` → `createClass` → 숙제가 있으면 `createActivityNoClass` → `releaseActivity`
- `schedule_api: legacy`: `addCourse` → `addCourseClass`; 숙제가 있으면 LMS homework activity 만 별도로 생성한다.
- LMS `createClass` 와 homework 생성에는 `teacherUid` 가 필요하므로 `teacher_uids` 또는 `default_teacher_uid` 를 채워야 한다.
- `createActivityNoClass(activityType=2)` 로 빈 숙제 초안은 만들 수 있지만, 실 API에서 내용 없는 숙제를 `releaseActivity` 하면 `errno=29601 内容不能为空` 이 반환된다. ClassIn 대시보드에서 숙제 내용을 채우거나 숙제 콘텐츠 작성 API가 필요하다.

## 5. Datasub Webhook

### 5.1 운영 제약 (지침 02 §1.2 재확인)

- **1기관 1 엔드포인트**. 엔드포인트 변경 시 ClassIn에 재등록 필요.
- **Real-time 사용 금지**. 이 프로젝트 범위에서는 수업 중 실시간 이벤트를 연결하지 않는다.
- **After-Class + LMS Datasub만 사용**. 수업 종료 데이터와 숙제/성적 이벤트를 안정 경로로 받는다.
- 담당자 이메일 등록 → 실패 시 즉시 통지.

### 5.2 공통 필드

모든 이벤트 body 최상위 (Cmd별 존재 여부 상이):

| 필드 | 설명 |
|---|---|
| `SID` | School ID |
| `Cmd` | 이벤트 디스크리미네이터 |
| `ClassID` | Lesson(수업) ID |
| `CourseID` | Course(반) ID — 일부 Cmd 누락 |
| `ActionTime` | 이벤트 발생 시각 (초) |
| `TimeStamp` | 전송 시각 (초) |
| `SafeKey` | 수신자 서명 검증 (`MD5(SECRET + TimeStamp)`) |
| `Data` | Cmd별 페이로드 |

### 5.3 Cmd 목록

#### After-Class Class-related (출처: `datasub/classrelated.html`)

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

### 5.4 MVP1 "미제출 알림" 구현 전략

**핵심**: 단일 Webhook 이벤트에 `homework_submitted: bool`이 없다. 여러 Cmd를 조립해야 함.

1. `createUnit` → `createClass` 로 수업 생성 → `classId`/`activityId` 확보 (Notion 저장).
2. LMS `releaseActivity` 로 해당 수업에 숙제 배정 → `activityId` 확보.
3. 수업 종료 시 `Attendance` 이벤트 수신 → 수업 기록 DB 에 per-student row 생성.
4. 이후 `HomeworkSubmit` 이벤트가 들어올 때마다 해당 row의 `숙제 제출 = True`.
5. 스케줄러가 `sweep_missing_homework` 실행 — `수업일시 > now - window AND 숙제 제출 != True` 인 row를 학생별 grouping 하여 Claude 문구 생성 → 발송.

## 6. 현재 코드의 확정/추정 매트릭스

| 항목 | 상태 | 근거 |
|---|---|---|
| v2 서명 알고리즘 | ✅ 확정 | signature.html + sign_demo.html 3종 코드 예시 일치 |
| v1/v2 엔드포인트 분리 | ✅ 확정 | v1 `course.api.php?action=...`, v2 `/lms/...` |
| `register` 응답 UID 위치 (`data`) | ✅ 확정 | register.html 명시 |
| `addCourseClass` 필드명 (`beginTime`/`endTime`/`teacherUid`) | ✅ 확정 | addCourseClass.html 명시 |
| LMS Unit/Classroom/Activity 기본 파라미터 | ✅ 확정 | LMS createUnit/createClassroom/createActivityNoClass/releaseActivity |
| `Attendance` 필드명 (`Data[].Uid/AttendanceTime/FirstInTime/LastOutTime`) | ✅ 확정 | classrelated.html |
| `HomeworkSubmit` 필드명 (`Data.ActivityId/StudentInfo.Uid/IsSubmitLate`) | ✅ 확정 | coursedata.html |
| `End` 내부 `handsupEnd/awardEnd/inoutEnd` 키 구조 | ⚠️ 추정 | 카테고리 존재는 확정, 세부 키는 샘플 페이로드 확인 필요 |
| `SafeKey` 계산식 | ✅ 확정 | `MD5(SECRET+TimeStamp)` |
| `releaseActivity` 필드명 | ✅ 확정 | 실 API상 `activityId` 필요. `activityIds` 배열은 `field "activityId" is not set` |
| 빈 숙제 초안 release | ✅ 확정 | `createActivityNoClass(activityType=2)` 후 내용 없이 release 시 `errno=29601 内容不能为空` |
| Webhook 재전송 정책 | ❌ 미확인 | ClassIn 담당자 확인 필요 |

`⚠️` 항목은 실 샘플 페이로드로 alias 조정하면 바로 확정 가능.
`❌` 항목은 ClassIn 문의가 필요.

## 7. 테스트 전략

- **단위**: `tests/test_signing_v2.py` — v1 SafeKey, v2 서명 정렬·필터링·해시, Webhook SafeKey
- **HTTP client**: `tests/test_classin_client.py` — v1 form/SafeKey, v2 headers/JSON, 엔벨로프 처리, CED payload 매핑
- **파싱**: `tests/test_webhook_schema.py` — 3종 샘플 (Attendance/End/HomeworkSubmit) 디스크리미네이트
- **Webhook e2e**: `classin-toolkit replay-webhook samples/attendance_sample.json` — 실 Notion 연동으로 적재 확인.
