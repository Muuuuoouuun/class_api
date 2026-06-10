---
name: classin-agent-usage
description: Use when the academy director needs to ask classin-toolkit questions in natural language — terminal-based Claude tool-use chat that queries Notion and triggers pipelines
---

# `classin-toolkit agent` — 수동 오더 에이전트

원장/교사가 자연어로 질문하면 Claude 가 tool-use 로 Notion 을 조회하거나 파이프라인을 실행한다. 자동 라인 (Webhook/sweep/weekly) 과 **완전히 분리된 수동 라인**.

## When to use

- 원장이 "이번 주 누가 숙제 안 냈어?" / "박성실 학생 요즘 어때?" 같은 질문을 즉석에서 할 때
- 주간 리포트를 정해진 시간이 아니라 지금 당장 돌리고 싶을 때
- V2 (SaaS) 전환 시 이 인터페이스만 웹 채팅 UI 로 교체될 예정 — 도구 구현은 그대로 재사용

## CLI

```bash
classin-toolkit agent
```

## 등록된 도구 (5종)

| 도구 | 호출 시 동작 |
|---|---|
| `query_missing_homework` | 미제출 학생 조회 (Notion 수업 기록 DB) |
| `query_missing_exam` | 특정 시험 미응시자 조회 |
| `query_student_stats` | 학생 단일 스탯 (출석률 / 참여도 / 숙제 제출률 etc.) |
| `list_students` | 재원생 목록 / 반별 필터 |
| `trigger_weekly_report` | `pipelines/weekly` 실행 → Notion 페이지 생성 |

## 사용 예

```
> classin-toolkit agent

원장님 > 이번 주 숙제 안 낸 학생 누구야?
(Claude → query_missing_homework 호출)
assistant > 이번 주(월~일) 미제출 학생 3명입니다. 지각/결석도 겹치네요...

원장님 > 박성실 학생 요즘 어때?
(query_student_stats 호출)
assistant > ...

원장님 > 주간 리포트 지금 돌려줘
(trigger_weekly_report 호출)
```

## 도구 추가는 한 곳에서만

`src/classin_toolkit/intelligence/agent.py` 의 `TOOLS` 리스트 + `_execute_tool` 분기. 새 도구는 [`classin-intelligence-prompts`](../classin-intelligence-prompts/SKILL.md) §"새 에이전트 도구 추가" 참고.

## 자동 vs 수동 분리 원칙

- 자동 (Webhook / 스케줄러): UI 없음. 로그·Notion 적재로만 존재
- 수동: `agent` CLI **한 곳에서만**. 원장이 즉석으로 수치·리포트 뽑는 유일한 인터페이스

## 관련 코드

- `src/classin_toolkit/intelligence/agent.py` (chat loop + tool 정의)
- `src/classin_toolkit/intelligence/claude_client.py` (Anthropic SDK 래퍼)

## 참고 문서

- `docs/10_architecture.md` 데이터 흐름 E (수동 오더 에이전트)
- `docs/14_developer_guide.md` §3 (사용 예 + §4.4 도구 추가)
