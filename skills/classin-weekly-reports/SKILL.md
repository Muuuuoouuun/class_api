---
name: classin-weekly-reports
description: Use when generating weekly per-student reports — produces HTML drafts first, then archives only approved drafts to Notion (HTML draft → review → approve → Notion archive)
---

# 주간 학생별 리포트

학생별 일주일치 활동을 Claude 가 분석해 개인화된 리포트를 만든다. **드래프트는 HTML 파일로, 승인된 것만 Notion 에 아카이브**.

## When to use

- 매주 금 17시 자동 (드래프트)
- 원장/컨설턴트 리뷰 후 수동 승인
- 데모 영상 — 페르소나별 다른 리포트 시연

## CLI

```bash
# 1) 드래프트 생성 (HTML, Notion 에는 안 씀)
classin-toolkit generate-weekly-drafts

# 2) 원장/컨설턴트가 reports_out/weekly/<week>/*.html 검토

# 3) 승인된 주차를 Notion 아카이브로 푸시
classin-toolkit approve-weekly --week 2026-04-22
```

## 두 단계 분리 이유

피드백 (2026-04-24): Notion 리포트 DB 는 "승인된 최종본 영구 보관소" 다. AI 가 곧바로 Notion 에 쓰면 검토 흔적이 남지 않고, 학부모 발송 직전 톤 수정도 어렵다. 그래서 **HTML 드래프트 → 승인 → 아카이브** 로 분리.

## config

```yaml
output:
  weekly:
    mode: "html+notion"          # html+notion (기본) | html | notion
    require_approval: true       # Notion 푸시 전 승인 단계 강제
```

`require_approval: false` 면 `generate-weekly-drafts` 가 곧장 Notion 에 쓴다 (피드백 위반 — 명확한 이유 없으면 true 유지).

## 페르소나별 차별화

`intelligence/prompts/weekly_report.md` 가 학생별로 다른 리포트가 나오도록 설계됨. 일괄 복붙 형태는 학부모가 즉시 알아챈다 (지침 02 §2.4).

데모 페르소나 5명 (`docs/15_demo_scenario.md`):

| ID | 톤 |
|---|---|
| 박성실 | 칭찬 + 다음 단계 |
| 김지각 | 부드러운 경고 + 등원 시간 제안 |
| 이하락 | 상담 제안 |
| 정활발 | 강점 강화 + 심화 과제 |
| 최결석 | 상담 필수 / 원장 자동 알림 |

## 출력 구조

```
reports_out/weekly/<week>/
  drafts.json              # index, approved:false 초기값
  S_001__박성실.html       # 학생별 리포트 본문
  ...
```

승인 후 Notion 리포트 DB row:
- `학생` (Relation), `리포트 기간` (Date), `학부모 발송 문구`, `HTML 링크` (Cloudflare Tunnel public URL), `승인됨=true`

## 관련 코드

- `src/classin_toolkit/pipelines/weekly.py` (`generate_drafts`, `approve_all`)
- `src/classin_toolkit/intelligence/weekly_report.py` (Claude 리포트 생성)
- `src/classin_toolkit/intelligence/prompts/weekly_report.md` (프롬프트)
- `src/classin_toolkit/storage/notion_repo.py` (`archive_approved_weekly_report`)
- `src/classin_toolkit/storage/templates/weekly.html`

## 참고 문서

- `docs/10_architecture.md` 데이터 흐름 D
- `docs/12_notion_schema.md` §3 (리포트 DB)
- `docs/15_demo_scenario.md` (페르소나)
