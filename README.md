# classin-toolkit

ClassIn 서드파티 자동화 컨설팅 Toolkit.
로컬 설치 + Claude 지능화로 학원 운영 자동화를 수행한다.
ClassIn API로 학원 운영 자동화를 관리하는 앱/도구 모음이다.

**문서는 [`docs/00_index.md`](docs/00_index.md)에서 시작.** 이 README 는 빠른 시작·커맨드 레퍼런스만.

## 아키텍처 (Layer 분리)

```
[Layer 1] classin/          ClassIn CED API + Webhook 스키마 + v2 서명
[Layer 2] storage/          Notion DB (학생/수업/리포트/메모/시험)
[Layer 3] intelligence/     Claude 프롬프트 · 분석 · 에이전트(수동 오더)
[Layer 4] pipelines/        비즈니스 로직 (ingest / core_engine / missing_homework / exams / weekly)
[Layer 5] notify/           카톡 알림 (dry_run → 알리고/솔라피)
```

ClassIn API 변경 시 **Layer 1만 수정**. 로컬→웹(V2 SaaS) 전환 시 Layer 5만 교체.
자세한 데이터 흐름은 [`docs/10_architecture.md`](docs/10_architecture.md).

## 빠른 시작

```bash
python -m venv .venv
.venv\Scripts\activate       # Windows
pip install -e ".[dev]"

cp config.yaml.example config.yaml
# config.yaml 값 채우기 (ClassIn 키, Notion 토큰, Claude 키)

# 자동 라인
classin-webhook                                             # Webhook 수신 서버
classin-toolkit parse-schedule samples/schedule_sample.csv  # 스케줄 업로드
classin-toolkit replay-webhook samples/attendance_sample.json
classin-toolkit sweep-missing-homework
classin-toolkit import-exam-results samples/exam_results_sample.csv --exam-name "4월 월말평가" --exam-date 2026-04-24 --dry-run
classin-toolkit import-exam-results samples/exam_results_sample.csv --exam-name "4월 월말평가" --exam-date 2026-04-24
classin-toolkit sweep-missing-exam --exam-name "4월 월말평가" --exam-date 2026-04-24
classin-toolkit create-answer-sheet --course-id 414193 --unit-id 22360790 --name "6월 OMR 답안지" --teacher-uid 1006368 --dry-run
classin-toolkit weekly-reports
classin-toolkit check-ready --mode local-demo
classin-toolkit diagnose-apis --live

# 수동 오더 라인
classin-toolkit agent    # 원장 대화형 AI 어시스턴트
classin-toolkit ui       # 로컬 브라우저 운영 UI / Academy Ops Hub
classin-toolkit ui --demo # config 없이 5명 페르소나 허브 데모
```

## 커맨드 레퍼런스

| 라인 | 커맨드 | 용도 |
|---|---|---|
| 자동 (수신) | `classin-webhook` | `/classin/webhook` POST 수신, Cmd 디스패치 |
| 자동 (CED)  | `classin-toolkit parse-schedule <csv> [--live]` | 스케줄 → ClassIn 수업 일괄 생성 |
| 자동 (디버그) | `classin-toolkit replay-webhook <json>` | 저장된 페이로드 재생 |
| 자동 (MVP1) | `classin-toolkit sweep-missing-homework [--lesson-id X]` | 미제출자 카톡 문구 생성 |
| 자동 (시험) | `classin-toolkit import-exam-results <csv|json> --exam-name ... --exam-date ... [--dry-run]` | 시험 결과를 학생 Master 와 병합해 Notion 시험 DB 에 적재 |
| 자동 (시험) | `classin-toolkit sweep-missing-exam --exam-name ... --exam-date ... [--class-name ...]` | 특정 시험 미응시자 카톡 문구 생성 |
| 자동 (OMR) | `classin-toolkit create-answer-sheet --course-id ... --unit-id ... --name ... [--release] [--live]` | ClassIn LMS Answer Sheet(OMR) activity 초안 생성, 선택 게시 |
| 자동 (MVP2) | `classin-toolkit weekly-reports` | 학생별 주간 리포트 Notion 페이지 |
| 자동 (SSO)  | `classin-toolkit sso-link --uid ... --course-id ... --class-id ... --telephone ...` | ClassIn 앱 호출 링크 |
| 점검 | `classin-toolkit check-ready --mode local-demo` | 테스트 단계별 API 키·DB ID 누락 확인 |
| 점검 | `classin-toolkit diagnose-apis [--live]` | ClassIn/Notion/Claude/Aligo 연결을 비파괴 probe로 확인 |
| 수동 (Agent) | `classin-toolkit agent` | 원장/교사 자연어 질문 → Claude tool-use (미제출·미응시·학원 데이터 맥락 조회 포함) |
| 수동 (UI) | `classin-toolkit ui` | 로컬 브라우저 Academy Ops Hub: ClassIn API Push, Data Subscription, 학원 데이터 융합, 개별 리포트 운영 |

## MVP 상태

- [x] Academy Ops Hub: 원장/교사용 첫 화면에서 4축 운영 상태 + 오늘 처리할 학생 큐 표시
- [x] 운영 전환 체크리스트: 허브 UI에서 local-demo/classin-live/kakao-live 준비 상태 확인
- [x] Notion DB 설계 미리보기: 허브 UI에서 학생·수업·리포트·메모·시험 5 DB dry-run 확인
- [x] 파일럿 브링업 브리프: 허브 UI에서 DataSub 신청 메일·Cloudflare 명령·Windows 상시 구동 체크리스트 생성/복사
- [x] 오늘의 운영 브리핑: 설정·미제출·데이터 매칭·리포트 품질을 실행 순서로 정리
- [x] 오늘의 자동화 실행계획: 설정 점검·데이터 확인·미제출 알림·리포트 보강·마감 리포트를 안전 게이트와 함께 Markdown으로 생성/복사
- [x] 오늘의 운영 리포트: 숙제 알림·데이터 수신·학원 데이터 융합·리포트 품질을 Markdown 인수인계로 생성/복사
- [x] 통합 교사 액션 큐: 숙제 알림, 데이터 매칭, 리포트 품질/구성 보강을 우선순위로 정렬
- [x] ClassIn 접속 링크 생성: 허브 UI에서 UID/Course/Class/전화번호 기반 SSO 링크 생성·복사
- [x] ClassIn Data Subscription 수신함: Webhook 원본 dump를 Cmd·수업·학생 신호별로 읽기 전용 확인
- [x] 학원 데이터 융합 context를 주간 리포트와 원장/교사용 AI 질문 응답에 재사용
- [x] 학원 데이터 융합 패널: 학생별 오프라인 출결·성적·메모·지난 리포트 연결 상태와 확인 필요 항목 표시
- [x] 개별 리포트 구성 preflight: 학생별 출결·숙제·시험·메모 섹션 준비도와 보강 항목 표시
- [x] 개별 리포트 초안 패키지: 학생별 ClassIn 근거·학원 데이터·학부모 문안·교사 체크리스트를 Markdown으로 생성/복사
- [x] 숙제 미제출 알림 문구 품질 점검: 빈 문구·연락처 없음·낙인 표현은 자동 발송 제외
- [x] 숙제 미제출 알림 문구 미리보기: 선택 대상 발송 전 AI 문구·품질·연락처 게이트 확인
- [x] Aligo 알림톡 live dispatch 경로: 승인 템플릿 코드·senderkey·품질 ready 게이트 통과 시 전송
- [x] 주간 리포트 드래프트 품질 점검: 근거·다음 액션·표현 안전·개인화 경고 표시
- [x] `blocked` 품질 리포트는 기본 승인/아카이브에서 제외
- [x] 허브 UI에서 주간 드래프트 품질 큐 조회 후 승인 처리
- [x] 코어 엔진: 스케줄 → Claude 파싱 → CED API (addCourse/addCourseClass)
- [x] MVP1: After-Class Webhook → Notion 적재 → 미제출 sweep → 카톡 dry-run
- [x] MVP2: 주간 학생별 개인화 리포트 → Notion 페이지 + 학부모 문구
- [x] 시험 결과 import + 기존 학생 Master 병합 + 미응시 sweep
- [x] OMR Answer Sheet 생성 + `AnswerSheetScore` Webhook → Notion 시험 DB 적재
- [x] 에이전트: tool-use 채팅 (수동 오더, 시험 미응시 조회 포함)
- [x] LMS 스케줄 생성 체인 (Unit/Classroom/Homework Activity/releaseActivity) Layer 1 + core engine mock 검증
- [ ] 실제 카톡 알림톡 운영 전환 (템플릿 심사·Standard 티어·학원 계정 live 검증)
- [ ] Notion DB 5종 스키마 세팅 (학원별 1회) — [docs/12_notion_schema.md](docs/12_notion_schema.md)
- [ ] 파일럿 학원 1곳 확보 → 실 데이터 검증

## 학원 PC 운영 체크리스트 (발췌)

전체는 [`docs/13_operations_runbook.md`](docs/13_operations_runbook.md). 요지:

- [ ] 절전 모드 해제 (Windows 전원옵션)
- [ ] Cloudflare Tunnel 상시 구동
- [ ] ClassIn 대시보드와 API 동시 조작 금지 고지
- [ ] API 키는 학원이 직접 보관
- [ ] 데이터 주권: 학원 = 처리자, MOON = 수탁자

## ClassIn API 매핑 (요약)

- v1: `POST https://api.eeo.cn/partner/api/course.api.php?action=<ACTION>`
  - 인증: form body `SID` / `safeKey=MD5(SECRET+timeStamp)` / `timeStamp`
  - 성공: `error_info.errno == 1`
- v2(LMS): `POST https://api.eeo.cn/lms/...`
  - 인증: 헤더 `X-EEO-UID` / `X-EEO-TS` / `X-EEO-SIGN`
  - 성공: 최상위 `code == 1`
- Webhook: 1 엔드포인트, `Cmd` 디스크리미네이터, `SafeKey` 필드 검증
- LMS Activity: `createActivityNoClass(activityType=2)` 는 숙제, `activityType=7` 은 Answer Sheet(OMR)
- 전체 스펙: [`docs/11_api_integration.md`](docs/11_api_integration.md)

## 다음 할 일

1. 실계정에서 OMR draft 생성 → 문항/정답 작성 → 게시 → `AnswerSheetScore` 샘플 1건 캡처
2. `config.yaml` 의 `classin.teacher_uids` 에 실제 교사명→UID 매핑 입력
3. Notion DB 5종 실제 생성 후 `config.yaml` 의 DB ID 채우기
4. 5 페르소나 페이크 데이터로 MVP2 리포트 차별화 수동 검증
5. 파일럿 학원 확보 → 실 Webhook 스트림 1~2주 캡처
6. 실학원 고정 URL 기준으로 설정 탭 `파일럿 브링업` 브리프 복사 → ClassIn DataSub 등록 요청
