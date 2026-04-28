# 14. 개발자 가이드

## 1. 개발 환경

### 요구사항
- Python 3.11+ (3.14 테스트됨)
- Windows/macOS/Linux
- 개발 편집기: VS Code + Pylance 권장

### 설치
```bash
git clone <repo>
cd class_api
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -e ".[dev]"
cp config.yaml.example config.yaml   # 그리고 값 채우기
```

### 테스트 실행
```bash
pytest -v
```
단위 테스트는 외부 서비스 호출 없이 전부 통과해야 함 (서명 계산, 페이로드 파싱 등).

### Lint / Format
```bash
ruff check src/ tests/
ruff format src/ tests/
```

## 2. CLI 명령어 빠른 참조

| 명령 | 언제 | 대응 레이어 |
|---|---|---|
| `classin-webhook` | 상시 구동. Webhook 수신 | Layer 1 수신기 |
| `classin-toolkit parse-schedule <csv>` | 학기 초 스케줄 업로드 | 코어 엔진 |
| `classin-toolkit replay-webhook <json>` | 과거 페이로드 재처리, 디버깅 | MVP1 ingest |
| `classin-toolkit sweep-missing-homework` | 배치 — 미제출자 알림 | MVP1 sweep |
| `classin-toolkit import-exam-results samples/exam_results_sample.csv --exam-name "4월 월말평가" --exam-date 2026-04-24 --dry-run` | 시험 결과 병합 사전 확인 | 시험 skill/API |
| `classin-toolkit sweep-missing-exam --exam-name "4월 월말평가" --exam-date 2026-04-24` | 배치 — 미응시자 알림 | 시험 skill/API |
| `classin-toolkit render-daily [--date YYYY-MM-DD]` | 일일 현황 HTML 생성 | 출력 레이어 |
| `classin-toolkit generate-weekly-drafts` | 주간 리포트 드래프트(HTML) | MVP2 |
| `classin-toolkit approve-weekly --week YYYY-MM-DD` | 드래프트 승인 → Notion 아카이브 | MVP2 |
| `classin-toolkit write-memo --classin-id X --text "..." [--tag ...]` | 원장 메모 기록 | 편집 채널 |
| `classin-toolkit sso-link --uid ... --course-id ... --class-id ... --telephone ...` | 학생·교사 ClassIn 앱 링크 | SSO |
| `classin-toolkit check-ready --mode local-demo` | config/API/DB 준비 상태 점검 | 운영 점검 |
| `classin-toolkit setup-notion --parent-page-id X --write` | Notion DB 5개 자동 생성 | 초기 세팅 |
| `classin-toolkit seed-demo-data --write` | 5명 페르소나 데모 데이터 생성 | 데모 준비 |
| `classin-toolkit agent` | 원장·교사 질문 대화 | 수동 오더 에이전트 |
| `classin-toolkit ui [--port 8790]` | 로컬 브라우저 운영 화면 | 수동 오더 UI |
| `classin-toolkit ui --demo` | config/Notion 없는 5명 페르소나 상황판 | 데모 UI |

모든 명령은 `--config <path>` 옵션으로 다른 config.yaml 지정 가능.

## 3. 에이전트 사용 예

```
> classin-toolkit agent

원장님 > 이번 주 숙제 안 낸 학생 누구야?
(Claude가 query_missing_homework 호출 → Notion 조회)
assistant > 이번 주(월~일) 미제출 학생 3명입니다. 지각/결석도 겹치네요...

원장님 > 박성실 학생 요즘 어때?
(query_student_stats 호출)
assistant > ...

원장님 > 주간 리포트 지금 돌려줘
(trigger_weekly_report 호출 → Notion에 페이지 5개 생성)
```

도구 추가는 [agent.py](../src/classin_toolkit/intelligence/agent.py) 의 `TOOLS` 리스트 + `_execute_tool` 분기에 한 곳에서만 한다.

## 3.1 로컬 UI 사용

```bash
classin-toolkit ui
# 브라우저: http://127.0.0.1:8790

classin-toolkit ui --demo
# config.yaml 없이 데모 데이터로 상황판 확인
```

UI는 기존 파이프라인을 감싸는 얇은 FastAPI 화면이다. 상태 확인, 일일 HTML 생성,
주간 드래프트 생성/승인, 미제출 sweep, 메모 작성, 단발 AI 질문을 실행한다.

## 4. 자주 있는 확장 시나리오

### 4.1 새 Webhook `Cmd` 지원 추가
1. [webhook_schemas.py](../src/classin_toolkit/classin/webhook_schemas.py) — `Cmd` 상수 + pydantic 이벤트 클래스 추가. `_KNOWN` 맵에 등록.
2. [pipelines/ingest.py](../src/classin_toolkit/pipelines/ingest.py) — 이벤트별 핸들러 함수 작성.
3. [webhook_receiver.py](../src/classin_toolkit/webhook_receiver.py) — `dispatch` 맵에 새 Cmd 추가.
4. 샘플 페이로드를 `samples/`에 추가하고 `tests/test_webhook_schema.py`에 assertion 추가.

### 4.2 새 CED action 추가
1. [ced.py](../src/classin_toolkit/classin/ced.py)에 메서드 추가. body 구성 + `self._c.call("actionName", body)` 호출. 반환 `data` 에서 ID 추출.
2. 필요 시 [schemas.py](../src/classin_toolkit/classin/schemas.py) 도메인 모델 확장.
3. pipelines에서 새 메서드 활용.

### 4.3 새 Notion 속성 추가
1. Notion DB 에 컬럼 추가 (정확한 이름 일치 주의).
2. [notion_repo.py](../src/classin_toolkit/storage/notion_repo.py) 상단 `PROP_*` 상수 추가.
3. `upsert_lesson_record` / `patch_lesson_record` 에 파라미터 + props 매핑 추가.
4. `_row_summary` 반환에도 필드 추가 (쿼리 결과에 노출).

### 4.4 새 에이전트 도구 추가
1. [agent.py](../src/classin_toolkit/intelligence/agent.py) `TOOLS` 리스트에 Anthropic tool 스키마 추가.
2. `_execute_tool` 에 분기 추가. Notion/파이프라인 호출.
3. 수동으로 `classin-toolkit agent` 실행해서 자연어로 부를 수 있는지 확인.

### 4.5 카톡 실제 발송 연동 (MVP 이후)
1. [dispatcher.py](../src/classin_toolkit/notify/dispatcher.py) `_send_via_aligo` 구현.
2. 알림톡 템플릿 ID / 파라미터 placeholder 매핑.
3. 프롬프트에 템플릿 규칙 반영 (변수명/순서 고정).
4. `config.yaml` `notify.mode: live` 로 전환.

## 5. 로컬에서 학원 환경 흉내내기

### 샘플 재생
```bash
classin-toolkit replay-webhook samples/attendance_sample.json --config config.yaml
classin-toolkit replay-webhook samples/homework_submit_sample.json --config config.yaml
classin-toolkit replay-webhook samples/end_summary_sample.json --config config.yaml
classin-toolkit sweep-missing-homework --window-hours 24 --config config.yaml
```

결과는 Notion(실 연동 시)·`reports_out/notify_dry_run/`(dry-run)에 쌓인다.

### Cloudflare Tunnel (개발)
```bash
# 계정 로그인은 한 번만
cloudflared tunnel login

# Quick tunnel (일회용)
cloudflared tunnel --url http://localhost:8787
# 출력된 trycloudflare.com URL 을 Postman 으로 두드려 테스트
```

## 6. 디버깅 팁

- 모든 CLI 명령에 `-v` / `--verbose` 가능 → DEBUG 로그 활성화.
- Webhook 원본은 `samples/incoming/<timestamp>.json` 에 자동 저장. 실패 재현 시 `replay-webhook` 사용.
- Claude 응답이 이상하면 `intelligence/prompts/*.md` 를 직접 수정 → 재실행(코드 재배포 불필요).
- v2 서명 에러는 99% 시스템 시간 문제. `w32tm /resync` (Windows).

## 7. 패키징 / 배포 (학원 전달용)

현재 설치 방식: 소스 체크아웃 + `pip install -e .`.

향후 옵션(지침 02 §2.2):
- PyInstaller 단일 exe
- Docker 컨테이너 (라즈베리파이 옵션과 궁합 좋음)
- `setup.exe` 마법사 (진짜 판매 단계)

MVP 단계는 소스 배포 + README 가이드로 충분.

## 8. 코드 스타일 최소 규칙

- 주석은 **WHY만**. WHAT 은 이름으로.
- 새 모듈 추가 전 "이게 어느 Layer인가?" 판단. 잘못된 위치면 리팩토링.
- 타입 힌트 필수. `from __future__ import annotations` 파일 맨 위.
- 에러 처리는 **경계에서만**. 내부 함수는 throw, 경계(CLI/Webhook handler)가 catch.
- 테스트는 외부 서비스 mock 없이 돌 수 있어야 함 (순수 모듈 기준).
