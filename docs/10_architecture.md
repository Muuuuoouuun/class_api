# 10. 아키텍처

## 원칙 (지침 02 §2.1)

- **Layer 분리가 최우선**. ClassIn API 변경 시 Layer 1만 수정. 로컬→웹 전환 시 Layer 5만 교체.
- **비즈니스 로직과 출력 계층 분리**. 출력은 언제든 대체 가능(카톡→이메일→SMS).
- **학원별 커스터마이징은 코드 수정 없이 `config.yaml` 변경만으로**.

## Layer 지도 (실 파일 매핑)

```
┌─ Layer 5 : 출력 ────────────────── notify/dispatcher.py
│              (카톡 dry_run / 알리고·솔라피 릴레이)
├─ Layer 4 : 비즈니스 ──────────── pipelines/
│              core_engine.py        스케줄 → 수업·숙제 자동 생성
│              ingest.py             Webhook Cmd 4종 → Notion 적재
│              missing_homework.py   미제출자 sweep (배치)
│              weekly.py             주간 리포트 생성
├─ Layer 3 : 지능화 ─────────────── intelligence/
│              claude_client.py      Anthropic SDK 래퍼 + prompt caching
│              schedule_parser.py    자유형 스케줄 → 구조화 JSON       (자동 파이프라인용)
│              missing_homework.py   학생별 카톡 문구 생성             (자동 파이프라인용)
│              weekly_report.py      학생별 주간 리포트 생성           (자동 파이프라인용)
│              agent.py              Claude tool-use 채팅 (수동 오더) — 도구 4종
│              prompts/*.md          프롬프트 파일(외부화)
├─ Layer 2 : 저장소 ─────────────── storage/notion_repo.py
│              (단일 진실원. ClassIn UID ↔ Notion page_id 매핑)
└─ Layer 1 : API 래퍼 ───────────── classin/
               client.py             단일 action POST + v2 서명
               signing.py            v2 MD5 정렬 알고리즘 + SafeKey 검증
               ced.py                register/addCourse/addCourseClass 등
               sso.py                getLoginLinked 헬퍼
               schemas.py            도메인 모델 (Student/Course/Lesson/Homework)
               webhook_schemas.py    Cmd 디스크리미네이터 유니온
```

## 데이터 흐름 3갈래

### A. CED 쓰기 (코어 엔진)
```
스케줄 파일
   └─> intelligence/schedule_parser  (Claude 파싱)
        └─> pipelines/core_engine
             └─> classin/ced (addCourse → addCourseClass → [LMS Activity])
                  └─> 반환 ID
                       └─> storage/notion_repo (영구 저장)
```

### B. Webhook 읽기 (MVP1 적재)
```
ClassIn 서버  ──POST──>  FastAPI /classin/webhook
                           │
                           ├─ dump_dir 원본 덤프
                           ├─ SafeKey 검증
                           └─ Cmd 디스패치
                                ├─ Attendance     → ingest.ingest_attendance    → upsert_lesson_record
                                ├─ End            → ingest.ingest_end_summary   → patch_lesson_record(camera)
                                ├─ HomeworkSubmit → ingest.ingest_homework_submit → patch_lesson_record(hw=True)
                                └─ HomeworkScore  → ingest.ingest_homework_score  → patch_lesson_record(score)
```

### C. 배치 파이프라인 (MVP1 sweep + MVP2)
```
Scheduler (cron / 수동 CLI)
   ├─ missing_homework.sweep_missing_homework
   │     └─> Notion 조회 → Claude 문구 → notify/dispatcher (dry_run)
   └─ weekly.run_weekly_reports
         └─> Notion 학생별 집계 → Claude 리포트 → Notion 페이지 저장
```

### D. 수동 오더 에이전트 (상시 대기)
```
원장 터미널  ──>  classin-toolkit agent
                     └─ intelligence/agent.chat_loop
                          └─ Claude tool_use
                               ├─ query_missing_homework ┐
                               ├─ query_student_stats    ├─> storage/notion_repo
                               ├─ list_students          ┘
                               └─ trigger_weekly_report ──> pipelines/weekly
```

자동 파이프라인(A/B/C)과 **완전히 분리**된 수동 라인. 원장이 "이번 주 누가 숙제 안 냈어?" 를
자연어로 물으면 Claude 가 도구 호출 → Notion 조회 → 답변.
V2(SaaS) 전환 시 이 인터페이스만 웹 채팅 UI 로 교체되고 도구 구현은 그대로 재사용.

## 의존성 방향

Layer N은 Layer N-k만 import. **역방향 import 금지**.

- `classin/` ← 다른 레이어가 import함. `classin/`은 어떤 레이어도 import하지 않음 (pydantic/httpx 외부만).
- `storage/notion_repo.py` ← `pipelines/`가 import. `storage/`는 `classin/`·`intelligence/`를 import하지 않음 (스키마 의존 없음).
- `intelligence/` ← `pipelines/`가 import. `intelligence/`는 `classin/webhook_schemas` 는 읽기용으로만 import (값 전달용).
- `pipelines/` ← `webhook_receiver.py`와 `cli/`만 import. 파이프라인끼리 상호 import 금지.
- `notify/` ← `pipelines/`만 import. 역참조 금지.

## 확장 포인트

| 바꾸고 싶은 것 | 건드릴 파일 | 바뀌면 안 되는 곳 |
|---|---|---|
| ClassIn API 스펙 변경 | `classin/*` 전부 | 나머지 전부 |
| 저장소 Notion → 다른 DB | `storage/notion_repo.py` 인터페이스 보존 | 나머지 전부 |
| 카톡 → 이메일/SMS | `notify/dispatcher.py` + provider 추가 | 나머지 전부 |
| 학원별 톤/정책 | `intelligence/prompts/*.md` + `config.yaml` | 코드 |
| 신규 Webhook Cmd 대응 | `classin/webhook_schemas.py`에 모델 추가 + `pipelines/ingest.py`에 핸들러 + `webhook_receiver.py` 디스패처 등록 | 나머지 |

## 실행 토폴로지 (학원 PC)

```
[학원 PC / 라즈베리파이]
   ├─ uvicorn (classin-webhook)   :8787               ← 자동: 이벤트 수신
   │     └─ Cloudflare Tunnel ─── https://<sub>.domain
   │                               └─> ClassIn Datasub 등록
   ├─ cron (Windows 작업 스케줄러)                     ← 자동: 배치
   │     ├─ 매 시각 +30분 : classin-toolkit sweep-missing-homework
   │     └─ 매주 금 17시  : classin-toolkit weekly-reports
   ├─ 원장 대화형 셸                                   ← 수동: 질문 응답
   │     └─ classin-toolkit agent
   └─ 파일 기반 상태
         ├─ config.yaml           (학원별 키)
         ├─ samples/incoming/     (Webhook 원본 덤프)
         └─ reports_out/          (카톡 dry_run 결과물 + 산출물)
```

**자동 트리거 vs 수동 오더의 분리 원칙**
- 자동(Webhook/스케줄러)은 UI 없음. 로그·Notion 적재로만 존재.
- 수동은 `agent` CLI 한 곳에서만. 원장이 수치·리포트를 **즉석으로 뽑아볼 수 있는 유일한 인터페이스**.
- V2(SaaS) 전환 시 자동 라인은 서버로, 수동 라인은 웹 채팅 UI 로 이동 — **Layer 5만 교체**.

## 왜 이렇게 굳이 분리했는가 (요약)

- **ClassIn 스펙 변동성**: 2026년 기준 v2 서명도 상대적으로 최신. 서명 알고리즘·Webhook 필드는 언제든 바뀔 수 있으므로 Layer 1만 교체할 수 있어야 함.
- **원장 기술 문맹 리스크**: UI를 자체 구현하지 않고 Notion에 위임 (= Layer 5를 외부화).
- **개인정보 수탁자 책임**: 학원 PC 로컬에만 데이터가 있어야 함 → 저장소는 원장 소유 Notion 워크스페이스.
- **영업 단계의 빠른 교체**: 카톡 승인 전까지는 dry_run, 이후 Standard 티어부터 live — `notify` 교체만으로 전환.
