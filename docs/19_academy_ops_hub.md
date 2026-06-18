# Academy Ops Hub

이 문서는 `classin-toolkit`을 단순 CLI 모음이 아니라 원장·교사가 매일 여는 학원 운영 AI 허브로
발전시키기 위한 제품 구조를 정의한다. 중심 질문은 하나다.

> 오늘 ClassIn과 학원 데이터를 보고, 누구에게 무엇을 해야 하는가?

허브는 아래 네 축으로 기능을 묶는다.

## 1. ClassIn API Push 기능

학원 운영자가 가진 계획과 결정을 ClassIn으로 밀어 넣는 영역이다. 주로 CED/LMS API를 사용한다.

| 기능 | 현재 구현 | 허브에서의 역할 |
|---|---|---|
| 학기/월간 스케줄 생성 | `parse-schedule`, UI 스케줄 입력 | 원장 표를 ClassIn 수업·숙제로 변환 |
| 숙제 활동 생성 | `core_engine`, LMS activity | 반복 숙제 세팅 시간을 줄임 |
| OMR 답안지 초안 생성 | `create-answer-sheet`, UI OMR 폼 | 시험/단원평가 운영을 ClassIn에 연결 |
| SSO 링크 생성 | `sso-link`, UI 접속 링크 폼 | 학생/교사 접속 지원 |

운영 원칙:

- 기본은 dry-run 또는 검토 단계로 시작한다.
- 실제 생성은 ClassIn 설정 점검 후 명시적인 클릭/옵션으로만 실행한다.
- ClassIn 대시보드와 API를 동시에 조작하지 않는다.

## 2. ClassIn Data Subscription 기능

ClassIn에서 들어오는 수업 후 데이터와 활동 데이터를 학원 운영 신호로 바꾸는 영역이다.

| 데이터 | 현재 구현 | 허브에서의 역할 |
|---|---|---|
| 출석/지각/결석 | Webhook ingest, Notion 수업 기록 | 오늘 지각·결석 학생 확인 |
| 숙제 제출/점수 | HomeworkSubmit/HomeworkScore ingest | 미제출 알림 큐 생성 |
| 수업 종료 요약 | End event ingest | 참여도·카메라·손들기 맥락 수집 |
| AnswerSheetScore | OMR 점수 ingest | 시험 DB와 개인 리포트 연결 |
| 원본 이벤트 수신함 | UI 데이터 탭, `/api/webhook-inbox` | dump 파일을 Cmd·수업·학생 신호별로 확인 |

운영 원칙:

- Webhook 원본은 보관하되 개인정보 포함 가능성을 전제로 다룬다.
- UI는 내부 이벤트명보다 학생 중심 상태를 보여준다.
- 실패한 ingest나 매칭 불가 데이터는 확인 필요 큐로 올린다.

## 3. 기존 자기 데이터·학원 데이터 융합

ClassIn 데이터만으로는 학원 맥락이 부족하다. 허브는 학원이 이미 갖고 있는 오프라인 자료,
상담 메모, 시험 파일, 기존 보고서를 학생별 context로 묶는다.

| 데이터 | 위치/형태 | 사용처 |
|---|---|---|
| 오프라인 출결 | `local_data/inbox/attendance/*.csv` | 결석/보강/지각 보정 |
| 오프라인 성적 | `local_data/inbox/scores/*.xlsx` | 성적 하락·상승 코멘트 |
| 상담 메모 | `local_data/inbox/memos/*.md`, Notion 메모 DB | 학부모 문구·리포트 톤 |
| 주간 리포트 산출물 | `reports_out/weekly`, Notion 리포트 DB | 승인 상태·지난 코멘트 참조 |
| 학생별 융합 맥락 | UI 데이터 탭, `/api/academy-contexts` | 어떤 자체 데이터가 누구에게 붙었는지 확인 |

운영 원칙:

- 원본 파일은 수정하지 않는다.
- 학생명만으로 강제 병합하지 않고, 가능하면 ClassIn ID·반·날짜를 함께 본다.
- 애매한 매칭은 자동 반영하지 않고 확인 필요 큐에 올린다.
- 민감한 오프라인 데이터 기반 문구는 보호자 발송 전 교사가 확인한다.

## 4. 개별 리포트 구성

개별 리포트는 ClassIn 활동 기록과 학원 자체 데이터를 합쳐 학생별로 다른 결론을 내야 한다.
목표는 "예쁜 요약"이 아니라 원장·교사가 바로 설명할 수 있는 근거 있는 코멘트다.

권장 구성:

| 섹션 | 포함 내용 | 근거 |
|---|---|---|
| 이번 주 한 줄 판단 | 성실, 지각, 하락, 결석, 상담 필요 등 | ClassIn 출석·숙제·참여도 |
| 출결·수업 루틴 | 출석률, 지각, 조퇴, 보강 여부 | Data Subscription + 오프라인 출결 |
| 숙제·학습 태도 | 제출률, 지연 제출, 점수, 반복 미제출 | Homework events + 미제출 sweep |
| 성적·시험 신호 | OMR/오프라인 시험, 최근 변화 | AnswerSheetScore + 학원 성적 파일 |
| 상담/메모 맥락 | 학부모 요청, 교사 관찰, 민감 이슈 | Notion 메모 + 로컬 메모 |
| 다음 액션 | 보강, 루틴 회복, 심화 과제, 상담 권장 | 위 근거의 종합 |
| 학부모 메시지 | 승인 후 발송 가능한 짧은 문구 | Claude/Gemini 생성 + 교사 검토 |
| AI 품질 점검 | 근거, 다음 액션, 표현 안전, 개인화 여부 | deterministic review gate |

품질 기준:

- 학생마다 다른 근거와 액션이 있어야 한다.
- 같은 템플릿 문장을 반복하지 않는다.
- "왜 이 코멘트가 나왔는지" 출처를 짧게 남긴다.
- 보호자에게 보낼 문구는 단정적 낙인보다 관찰·제안 톤을 우선한다.
- 드래프트에는 `ready` / `review` / `blocked` 품질 상태와 경고를 붙인다.
- 드래프트 생성 전에도 학생별 `개별 리포트 구성` preflight에서 출결·숙제·시험·메모·다음 액션 섹션의
  준비도와 보강 항목을 확인한다.
- preflight가 만든 근거는 `/api/student-report-pack`에서 학생별 Markdown 초안으로 묶어 교사용 확인,
  학부모 문안 초안, 체크리스트까지 한 번에 복사할 수 있게 한다.
- `blocked`는 기본 승인/아카이브 대상에서 제외하고 원장/교사 검토 대상으로 본다.
- 필요한 경우에만 `approve-weekly --force-blocked-quality`로 강제 아카이브한다.
- 허브 UI의 주간 드래프트 검토 큐에서 상태·점수·경고·HTML 링크를 먼저 확인한 뒤 승인한다.
- 숙제 미제출 알림도 빈 문구, 보호자 연락처 없음, 낙인 표현은 `blocked`로 보고 자동 발송에서 제외한다.
- 숙제 미제출 알림은 발송 전 `/api/preview-missing-homework`로 AI 문구, 품질 상태, 연락처 마스킹,
  live 발송 가능 여부를 먼저 확인한다. live 모드는 `ready` 문구와 보호자 연락처가 모두 있는 항목만 전송한다.
- 긴 운영 결과는 무한 스크롤이 되지 않도록 접기/펴기 또는 페이지·번호 목록으로 제한한다.

## 5. 허브 첫 화면

첫 화면은 메뉴가 아니라 운영 큐다.

```text
Academy Ops Hub

ClassIn API Push      준비 / 설정 필요
ClassIn Data Sub      Webhook 원본 N건 / 미제출 N명
학원 데이터 융합       학생 맥락 N명 / 확인 필요 N건
개별 리포트            드래프트 N건 / 승인 대기 N건
품질 검토              ready N건 / review N건 / blocked N건

오늘의 운영 브리핑
1. 발송 실패 재시도
2. 보호자 연락처 보완
3. blocked 리포트 수정

오늘 처리할 학생
- 이하락: 리포트 품질 blocked · 다음 액션 부족
- 김지각: 숙제 미제출 연락 필요 · 2회째
- 동명이인: 오프라인 성적 학생 자동 매칭 필요
- 최결석: 리포트 구성 review · 시험 신호 보강 필요

각 학생 큐는 `ready` / `review_required` 실행 상태, 안전 게이트, 완료 기준을 함께 가진다. 교사는
"바로 실행 가능" 항목만 즉시 처리하고, `review_required` 항목은 근거·연락처·품질 경고를 보강한 뒤 처리한다.

오늘의 운영 리포트
- 숙제 알림, Data Subscription, 학원 데이터 융합, 리포트 품질 상태를 Markdown으로 생성
- 원장 공유, 교사 인수인계, 하루 마감 기록에 사용
- `reports.output_dir/ops`에 저장하면 최근 기록 목록에서 다시 열람

오늘의 자동화 실행계획
- 설정 점검, ClassIn 수신 데이터 확인, 미제출 알림, 학원 데이터 매칭, 리포트 보강, 승인, 마감 리포트 순서로 정리
- 각 단계는 담당자, 위험도, 예상 시간, UI 이동 위치, CLI 기준 명령, 안전 게이트를 함께 표시

운영 전환 체크리스트
- `local-demo`, `classin-live`, `kakao-live` 단계별 준비 상태를 UI에서 확인
- config 누락, ClassIn live 준비물, 알리고 senderkey/템플릿 코드 누락을 짧은 목록과 페이지로 표시

Notion DB 설계 미리보기
- 학생 Master, 수업 기록, 리포트, 메모, 시험 DB 5개의 생성 예정 속성을 UI에서 dry-run으로 확인
- `setup-notion` dry-run/write 명령과 `config.yaml` DB ID 조각을 복사해 초기 세팅 실수를 줄임

파일럿 브링업
- Cloudflare named tunnel, ClassIn DataSub 등록 메일, Windows 작업 스케줄러 등록 명령을 하나의 Markdown 브리프로 생성
- `secret_key`, `webhook_secret`, 알리고 키, 전화번호, SID 원문은 자동 삽입하지 않고 운영자가 최종 발송 직전에 확인
- 수신기와 터널은 `scripts/install-windows-tasks.ps1`로 학원 PC 로그인 시 자동 실행되게 등록

개별 리포트 초안
- 학생별 ClassIn 근거, 학원 데이터 맥락, 학부모 전달 문안, 교사 확인 체크리스트를 Markdown으로 생성
```

구현 위치:

- 백엔드 요약 API: `src/classin_toolkit/ui.py`의 `/api/ops-hub`
  (`ops_brief`가 설정·미제출·데이터 매칭·리포트 품질을 우선순위별 실행 항목으로 변환)
- 운영 리포트 API: `src/classin_toolkit/ui.py`의 `/api/ops-report`
  (허브 요약·학생 큐·Webhook 수신함·학원 데이터 맥락·리포트 품질을 Markdown 인수인계로 변환)
- 운영 리포트 저장/최근 기록 API: `src/classin_toolkit/ui.py`의 `/api/ops-handoff`, `/api/ops-handoffs`
  (`reports.output_dir/ops`에 Markdown 파일과 최근 기록 인덱스를 저장)
- 자동화 실행계획 API: `src/classin_toolkit/ui.py`의 `/api/ops-playbook`
  (허브 요약을 설정 점검·데이터 확인·알림·리포트·마감 순서와 안전 게이트로 변환)
- 운영 전환 체크리스트 API: `src/classin_toolkit/ui.py`의 `/api/readiness`
  (`readiness.check_readiness` 결과를 원장/교사용 설정 탭 카드와 페이지 목록으로 표시)
- Notion DB 설계 미리보기 API: `src/classin_toolkit/ui.py`의 `/api/notion-schema`
  (`storage.notion_setup.dry_run_schema` 결과와 setup 명령/config 조각을 UI에 표시)
- 파일럿 브링업 API: `src/classin_toolkit/ui.py`의 `/api/pilot-brief`
  (Cloudflare/DataSub/Windows 상시 구동 브리프를 민감값 없이 생성)
- Webhook 수신함 API: `src/classin_toolkit/ui.py`의 `/api/webhook-inbox`
  (`webhook.dump_dir`의 원본 JSON을 읽기 전용 운영 이벤트로 요약)
- 학원 데이터 융합 API: `src/classin_toolkit/ui.py`의 `/api/academy-contexts`
  (`academy_context`의 학생별 자체 데이터 맥락과 자동 매칭 확인 필요 항목을 표시)
- 통합 교사 액션 큐: `src/classin_toolkit/intelligence/action_queue.py`
  (숙제 알림, 학원 데이터 매칭, 주간 드래프트 품질, 개별 리포트 구성 preflight를 우선순위로 정렬하고
  실행 상태·안전 게이트·완료 기준을 부여)
- 숙제 알림 문구 미리보기 API: `src/classin_toolkit/ui.py`의 `/api/preview-missing-homework`
  (선택한 미제출 학생의 AI 문구·품질·연락처 게이트를 발송 전 확인)
- 드래프트 검토 API: `src/classin_toolkit/ui.py`의 `/api/weekly-drafts`
- 개별 리포트 구성 API: `src/classin_toolkit/ui.py`의 `/api/report-compositions`
- 개별 리포트 초안 API: `src/classin_toolkit/ui.py`의 `/api/student-report-pack`
  (preflight 결과를 학생 1명 기준 Markdown 초안과 교사 체크리스트로 변환)
- ClassIn 접속 링크 API: `src/classin_toolkit/ui.py`의 `/api/sso-link`
- 선생님 큐 기준: `docs/18_teacher_dashboard_data_merge.md`
- 로컬 데이터 병합: `src/classin_toolkit/intelligence/academy_context.py`
  (`pipelines/data_merge.py`는 기존 import 호환 wrapper)
- 개인 리포트 구성 preflight: `src/classin_toolkit/intelligence/report_composition.py`
- 개인 리포트 생성: `src/classin_toolkit/pipelines/weekly.py`,
  `src/classin_toolkit/intelligence/weekly_report.py`

## 6. AI 재사용 경로

허브의 병합 context는 화면에만 쓰면 안 된다. 같은 학생 맥락을 아래 경로에서 재사용한다.

| 경로 | 구현 | 사용하는 context |
|---|---|---|
| 주간 리포트 드래프트 | `pipelines/weekly.py` → `intelligence/weekly_report.py` | `academy_context` payload |
| 통합 교사 액션 큐 | `intelligence/action_queue.py` + `/api/ops-hub` | 미제출 알림·데이터 매칭·리포트 품질/구성 |
| HTML 리뷰 화면 | `storage/templates/weekly.html` | 학생 맥락 출처 표 |
| 개별 리포트 구성 | `intelligence/report_composition.py` + `/api/report-compositions` | 수업·숙제·시험·메모·알림 이력 섹션 준비도 |
| 개별 리포트 초안 패키지 | `/api/student-report-pack` | preflight 섹션·학원 맥락·교사용 체크리스트 |
| 리포트 품질 점검 | `intelligence/report_quality.py` | 근거·액션·표현 안전·개인화 경고 |
| 숙제 알림 품질 점검 | `intelligence/notification_quality.py` | 연락처·숙제 맥락·다음 액션·표현 안전 경고 |
| 원장/교사 자연어 질문 | `intelligence/agent.py`의 `query_academy_context` tool | 리포트 상태, 오프라인 출결/성적, 상담 메모, 확인 필요 자료 |

프롬프트 규칙:

- ClassIn 수업 기록과 학원 자체 데이터를 함께 보되, 출처가 있는 내용만 리포트에 반영한다.
- 자동 매칭이 애매한 자료는 리포트 문장에 섞지 않고 확인 필요 큐로 남긴다.
- AI 질문에서 학생 상담 맥락이 필요하면 `query_academy_context`를 호출한 뒤 답한다.
