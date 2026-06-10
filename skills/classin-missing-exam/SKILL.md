---
name: classin-missing-exam
description: Use when sweeping for students who didn't take a specific exam — compares Notion exam DB vs student Master DB and generates per-parent katalk dry-run messages
---

# 미응시자 sweep

특정 시험에 응시 기록이 없는 학생을 찾아 학부모용 카톡 문구를 생성한다.

## When to use

- 시험 결과 import ([`classin-exam-import`](../classin-exam-import/SKILL.md)) 직후
- 특정 반만 따로 sweep 하고 싶을 때

## CLI

```bash
# 전체 학생 대상
classin-toolkit sweep-missing-exam --exam-name "4월 월말평가" --exam-date 2026-04-24

# 특정 반만
classin-toolkit sweep-missing-exam \
  --exam-name "4월 월말평가" --exam-date 2026-04-24 \
  --class-name 고2-A
```

## 동작

```
시험 DB ─┐
         ├─> 비교 (시험명 + 시험일 기준)
학생 Master ┘
         │
         └─> 미응시 = (학생 Master 재원중) - (응시 row 존재)
              └─> Claude 문구 → notify/dispatcher (dry_run)
```

`응시 여부 = false` 이거나 row 자체가 없으면 미응시로 처리.

## 선행 조건

- 시험 결과 import 가 끝나 있어야 함 (그래야 비교 대상이 존재)
- 학생 Master `상태 = 재원` 만 미응시 후보 (휴원/퇴원 제외)

## 출력

`reports_out/notify_dry_run/<timestamp>__<student>__missing-exam.md`

## 흔한 함정

| 증상 | 원인 |
|---|---|
| 미응시 0명인데 실제로는 빠짐 | exam-import 가 학생 Master 매칭 실패해 row 자체가 없음 → import 로그 확인 |
| 휴원생도 알림 발송 | 학생 Master `상태` 갱신 안 됨 |
| `--class-name` 매칭 실패 | 학생 Master `반` Select 값이 정확히 일치해야 함 (공백/특수문자 주의) |

## 관련 코드

- `src/classin_toolkit/pipelines/exams.py` (`sweep_missing_exam`)
- `src/classin_toolkit/intelligence/missing_exam.py` (Claude 문구)
- `src/classin_toolkit/intelligence/prompts/` (시험 미응시 프롬프트)

## 참고 문서

- `docs/12_notion_schema.md` §4 (시험 DB)
