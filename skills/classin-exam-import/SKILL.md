---
name: classin-exam-import
description: Use when importing exam results (CSV/JSON) into the Notion exam DB — merges external exam scores with student Master DB, deduplicates by exam name + date + student
---

# 시험 결과 import

외부 학원 DB / CSV / 별도 시험 API 결과를 학생 Master DB 와 병합해 Notion 시험 DB 에 upsert.

## When to use

- 월말평가 / 모의고사 / 내신 성적 도착 후
- 미응시자 sweep ([`classin-missing-exam`](../classin-missing-exam/SKILL.md)) 의 선행 작업

## CLI

```bash
# dry-run: 매칭 결과만 확인 (Notion 쓰지 않음)
classin-toolkit import-exam-results samples/exam_results_sample.csv \
  --exam-name "4월 월말평가" --exam-date 2026-04-24 --dry-run

# 실제 적재
classin-toolkit import-exam-results samples/exam_results_sample.csv \
  --exam-name "4월 월말평가" --exam-date 2026-04-24
```

표준 절차: **반드시 `--dry-run` 으로 먼저 매칭 확인 → 학생 Master 누락 / 동명이인 처리 → 본 실행**.

## 입력 형식

CSV 헤더 예 (동의어 허용):
```
학생명, 반, 과목, 원점수, 만점, 외부시험ID, 데이터출처
```

JSON: 같은 필드명의 레코드 배열.

## 매칭 로직

1. `학생명 + 반` 우선 매칭
2. 매칭 실패 시 `학생명` 만으로 시도 (동명이인 경고 출력)
3. 그래도 실패 → `학생 Master 에 없는 학생` 으로 리포트 → 수동 처리 필요

## Notion 시험 DB 컬럼 (필수)

| 컬럼 | 타입 | 비고 |
|---|---|---|
| 시험명 | Title | `--exam-name` 인자 |
| 학생 | Relation → 학생 Master | 매칭 결과 |
| 시험일 | Date | `--exam-date` 인자 |
| 응시 여부 | Checkbox | 점수 있으면 true |
| 원점수 / 만점 / 백분율 | Number | 백분율 = `원점수/만점*100` |
| 데이터 출처 | Text | `academy-db` / `csv-import` / 등 |
| 외부 시험 ID | Text | 멱등 키 (재실행 시 upsert) |

전체 스키마는 [`classin-notion-schema`](../classin-notion-schema/SKILL.md) 또는 `docs/12_notion_schema.md`.

## 관련 코드

- `src/classin_toolkit/pipelines/exams.py` (`import_exam_results`)
- `src/classin_toolkit/storage/notion_repo.py` (시험 DB upsert)

## 참고 문서

- `docs/12_notion_schema.md` §4 (시험 DB)
- `docs/10_architecture.md` (데이터 흐름 C)
