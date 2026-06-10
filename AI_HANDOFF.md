# AI 작업 인계서

이 문서는 Claude, Codex, Genspark 같은 AI 자동화 도구에 이 저장소를 넘길 때 함께 주는
최상위 안내서다. AI는 먼저 이 파일을 읽고, 필요한 문서와 코드를 따라가며 작업한다.

## 1. 프로젝트 한 줄 설명

`classin-toolkit`은 ClassIn API, Webhook, Notion DB, Claude 분석을 묶어 학원 운영을
자동화하는 로컬 설치형 Python Toolkit이다.

현재 제품 형태는 다음과 같다.

- 자동 라인: ClassIn Webhook 수신, 스케줄 파싱, 미제출 sweep, 시험 결과 병합, 주간 리포트 생성
- 수동 라인: `classin-toolkit agent` 터미널 기반 원장/교사용 AI 어시스턴트
- 운영 화면: Notion DB와 HTML 리포트 파일을 우선 사용
- 향후 전환: 로컬 CLI/Notion 운영에서 웹 UI 또는 SaaS로 확장 가능하도록 Layer 분리

## 2. 먼저 읽을 파일

AI는 작업 전에 아래 순서로 읽는다.

1. `README.md`
2. `docs/00_index.md`
3. `docs/10_architecture.md`
4. `docs/11_api_integration.md`
5. `docs/12_notion_schema.md`
6. `docs/13_operations_runbook.md`
7. `docs/14_developer_guide.md`

작업이 UI, 배포, 영업 데모에 가까우면 `docs/15_demo_scenario.md`와
`docs/16_roadmap.md`도 확인한다. 선생님 상황판, 보고서 맥락, 로컬/오프라인 공유 데이터 병합은
`docs/18_teacher_dashboard_data_merge.md`를 먼저 읽는다.

## 3. 핵심 구조

Layer 경계를 우선 지킨다.

```text
Layer 1  classin/        ClassIn API, v2 signing, Webhook schema
Layer 2  storage/        Notion repository, HTML renderer, output protocol
Layer 3  intelligence/   Claude prompts, parser, report generator, agent
Layer 4  pipelines/      business workflows
Layer 5  notify/         dry-run or live notification dispatch
```

중요 원칙:

- ClassIn API 변경은 `src/classin_toolkit/classin/*` 안에서 먼저 해결한다.
- Notion 저장소 변경은 `src/classin_toolkit/storage/notion_repo.py`의 인터페이스를 보존한다.
- 카톡, 이메일, SMS 같은 출력 변경은 `src/classin_toolkit/notify/*` 또는 출력 계층에서 처리한다.
- 학원별 정책, 톤, 키 값은 코드가 아니라 `config.yaml`과 `intelligence/prompts/*.md`에서 다룬다.
- 파이프라인끼리 무리하게 서로 import하지 않는다.
- 선생님 상황판은 기능 나열이 아니라 "오늘 처리할 학생 큐"가 되게 한다.
- 보고서, 오프라인 출결, 성적, 상담 메모 같은 로컬 공유 데이터는 원본을 보존한 채 학생별 context로 병합한다.

## 4. 로컬 실행

Windows 기준:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
Copy-Item config.yaml.example config.yaml
```

`config.yaml`에는 실제 운영 키가 필요하다.

- ClassIn SID, signing secret, webhook SafeKey
- Notion integration token, DB ID 5종 (학생/수업/리포트/메모/시험)
- Anthropic API key
- 알림 공급자 키, live 전환 전에는 `notify.mode: dry_run`

## 5. 주요 명령

```powershell
classin-webhook
classin-toolkit parse-schedule samples/schedule_sample.csv
classin-toolkit replay-webhook samples/attendance_sample.json
classin-toolkit sweep-missing-homework
classin-toolkit import-exam-results <csv|json> --exam-name ... --exam-date ... --dry-run
classin-toolkit import-exam-results <csv|json> --exam-name ... --exam-date ...
classin-toolkit sweep-missing-exam --exam-name ... --exam-date ...
classin-toolkit render-daily
classin-toolkit generate-weekly-drafts
classin-toolkit approve-weekly --week YYYY-MM-DD
classin-toolkit write-memo --classin-id 10001 --text "상담 기록" --tag 상담
classin-toolkit agent
classin-toolkit ui
classin-toolkit ui --demo
```

검증:

```powershell
pytest -q
ruff check src/ tests/
ruff format src/ tests/
```

## 6. AI에게 줄 기본 프롬프트

아래 프롬프트를 작업 지시 앞에 붙이면 된다.

```text
이 저장소는 ClassIn Toolkit입니다.
먼저 AI_HANDOFF.md, README.md, docs/00_index.md, docs/10_architecture.md,
docs/14_developer_guide.md를 읽고 작업하세요.

Layer 경계를 지키세요.
ClassIn API 변경은 classin/ 계층, 저장소 변경은 storage/ 계층,
비즈니스 흐름은 pipelines/ 계층, Claude 프롬프트/분석은 intelligence/ 계층에서 처리하세요.

기존 사용자가 만든 변경은 되돌리지 마세요.
코드 변경이 스펙이나 운영 방식에 영향을 주면 관련 docs도 함께 갱신하세요.
테스트는 가능한 범위에서 실행하고, 실행하지 못한 이유를 마지막에 명시하세요.

작업 목표:
...
```

## 7. 작업 유형별 지시 템플릿

### 신규 Webhook Cmd 추가

```text
ClassIn Webhook Cmd <CMD_NAME>을 추가하세요.
webhook_schemas.py에 모델과 Cmd 매핑을 추가하고,
pipelines/ingest.py에 처리 함수를 만들고,
webhook_receiver.py 디스패처에 연결하세요.
samples/에 예시 payload를 추가하고 tests/test_webhook_schema.py를 갱신하세요.
docs/11_api_integration.md도 변경하세요.
```

### Notion 컬럼 추가

```text
Notion <DB_NAME> DB에 <COLUMN_NAME> 컬럼을 추가하는 코드 변경을 하세요.
docs/12_notion_schema.md를 먼저 확인하고,
notion_repo.py 상단 PROP_* 상수와 관련 read/write mapping을 갱신하세요.
기존 row summary 반환 계약이 깨지지 않게 하세요.
```

### 알림 공급자 live 연동

```text
notify.mode=live에서 <PROVIDER>로 실제 발송되도록 구현하세요.
dry_run 동작은 유지하고, provider-specific 코드는 notify 계층에 격리하세요.
발송 실패는 경계 계층에서 로깅하고 파이프라인이 전체 중단되지 않게 처리하세요.
```

### 로컬 UI 추가

```text
원장/컨설턴트가 로컬에서 쓰는 얇은 UI를 추가하세요.
기존 pipeline 함수를 재사용하고, 비즈니스 로직을 UI 파일에 새로 쓰지 마세요.
초기 범위는 상태 확인, 일일 HTML 생성, 주간 드래프트 생성/승인,
미제출 sweep, 메모 작성, 간단한 AI 질문입니다.
```

### 선생님 상황판 + 로컬 데이터 병합

```text
docs/18_teacher_dashboard_data_merge.md를 먼저 읽고 작업하세요.
선생님 화면은 복잡한 관리자 페이지가 아니라 오늘 처리할 학생 큐여야 합니다.

보고서(`reports_out/weekly`, Notion 리포트 DB)와 로컬/오프라인 공유 데이터
(`local_data/inbox`의 CSV/XLSX/메모/첨부)를 학생별 context로 병합하세요.
원본 파일은 절대 수정하지 말고, 자동 매칭이 애매하면 확인 필요 큐로 올리세요.

병합 결과는 미제출 상황판, 주간 보고서 생성, AI 질문 응답에서 재사용되게 하세요.
```

## 8. 개인정보와 운영 안전

- `config.yaml`, `.env`, API key, token은 절대 커밋하지 않는다.
- Webhook 원본과 리포트 산출물은 개인정보를 포함할 수 있으므로 기본적으로 커밋하지 않는다.
- 실제 발송은 `notify.mode: live`일 때만 가능해야 한다.
- 카톡 알림톡은 템플릿 심사 전까지 dry-run을 기본으로 둔다.
- 학원 PC는 절전 모드를 해제해야 Webhook 누락 위험이 줄어든다.

## 9. 현재 우선순위

1. 파일럿 학원 1곳 기준으로 Notion DB 5종을 실제 생성하고 `config.yaml`을 채운다.
2. Webhook SafeKey 검증 알고리즘을 ClassIn 담당자에게 확인한다.
3. 로컬 UI를 선생님용 상황판 중심으로 정리해 미제출, 발송 여부, 확인 필요 학생을 바로 처리하게 한다.
4. 보고서와 로컬/오프라인 공유 데이터 병합 구조를 추가해 상담 맥락을 상황판과 주간 리포트에 반영한다.
5. 1~2주 실데이터로 리포트 품질과 미제출 알림 문구를 검증한다.
6. 카톡 live 발송은 템플릿 심사 후 별도 단계로 전환한다.

## 10. 작업 완료 보고 형식

AI는 작업을 끝낸 뒤 아래를 짧게 보고한다.

```text
변경:
- ...

검증:
- ...

주의:
- 실행하지 못한 테스트, 필요한 실키, 운영상 남은 확인 사항
```
