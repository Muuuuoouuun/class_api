---
name: classin-intelligence-prompts
description: Use when authoring or editing Claude prompts in intelligence/prompts/, modifying schedule_parser/missing_homework/weekly_report logic, or adding a new agent tool — Claude is for analysis/reports/copy/OCR only, not data collection
---

# Layer 3 — Claude 프롬프트 / Intelligence

Claude 활용 원칙: **분석·리포트·문구 생성·OCR** 전용. 데이터 수집·저장은 다른 Layer 가 한다.

## 핵심 원칙 (지침 02 §2.4)

1. Claude 는 분석/리포트/문구/OCR 만. 데이터 fetch 하지 않음
2. 학생별로 **다른 리포트** 가 나오도록 프롬프트 설계 — 일괄 복붙은 학부모가 즉시 알아챔
3. 한국 학원 문화 톤앤매너 — **카톡 문구 ≠ 공식 보고서 문체**
4. 프롬프트는 학원 티어별 분리 가능하게 외부 파일 (`prompts/*.md`)
5. 학원별 정책·톤·키 값은 **코드가 아니라** `config.yaml` + `prompts/*.md` 에서 다룸

## 디렉토리

```
intelligence/
  claude_client.py       # Anthropic SDK 래퍼 + prompt caching
  schedule_parser.py     # 자유형 스케줄 → 구조화 JSON  (자동)
  missing_homework.py    # 학생별 카톡 문구           (자동)
  missing_exam.py        # 시험 미응시 카톡 문구
  weekly_report.py       # 주간 리포트                (자동)
  agent.py               # tool-use 채팅              (수동)
  prompts/*.md           # 프롬프트 (외부화)
```

## 프롬프트 수정 시

- `prompts/*.md` 만 수정해도 됨 — 코드 재배포 불필요
- 학원명·교사명·학생명은 **치환 변수** 활용. 하드코딩 금지
- "환각 금지" / "입력에 없는 학생 이름 만들지 말 것" 같은 가드는 카톡 문구 프롬프트에 명시

## 새 에이전트 도구 추가 (`agent.py`)

`agent.py` 의 `TOOLS` 리스트 + `_execute_tool` 분기에 **한 곳에서만** 등록.

1. Anthropic tool 스키마를 `TOOLS` 에 추가 (`name`, `description`, `input_schema`)
2. `_execute_tool` 의 `if tool_name == ...` 분기 추가 — Notion 또는 파이프라인 호출
3. 수동으로 `classin-toolkit agent` 실행해 자연어로 부를 수 있는지 확인

기존 도구 6종: `query_missing_homework`, `query_missing_exam`, `query_student_stats`, `list_students`, `query_academy_context`, `trigger_weekly_report`. 사용법은 [`classin-agent-usage`](../classin-agent-usage/SKILL.md).

## Claude 응답이 이상할 때

1. `intelligence/prompts/<해당>.md` 를 직접 수정 → 재실행
2. 입력 payload 로그 확인 — Notion ↔ ClassIn UID 매핑 오류일 가능성
3. `claude_client.py` 의 prompt caching 키가 의도대로 박히고 있는지

## prompt caching

`claude_client.py` 가 Anthropic SDK 의 prompt caching 을 래핑한다. 시스템 프롬프트·도구 스키마처럼 **자주 안 바뀌는** 부분을 cache control 로 묶어 비용·지연 절감.

## OCR 사용처

LMS 성적 조회 API 가 없어서 (지침 02 §1.3) 오프라인 시험지 / 사진 → Claude 멀티모달 OCR 로 점수 추출 가능. 결과는 [`classin-exam-import`](../classin-exam-import/SKILL.md) 의 import 경로로 들어감.

## 관련 코드

- `src/classin_toolkit/intelligence/claude_client.py`
- `src/classin_toolkit/intelligence/agent.py`
- `src/classin_toolkit/intelligence/schedule_parser.py`
- `src/classin_toolkit/intelligence/missing_homework.py`
- `src/classin_toolkit/intelligence/missing_exam.py`
- `src/classin_toolkit/intelligence/weekly_report.py`
- `src/classin_toolkit/intelligence/prompts/*.md`

## 참고 문서

- `docs/02_guidelines.md` §2.4 (Claude 활용 원칙)
- `docs/14_developer_guide.md` §4.4 (도구 추가 절차)
- `docs/10_architecture.md` 데이터 흐름 E (수동 오더 에이전트)
