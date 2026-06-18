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

# blocked 품질 드래프트를 정말 승인해야 할 때만
classin-toolkit approve-weekly --week 2026-04-22 --force-blocked-quality
```

## 두 단계 분리 이유

피드백 (2026-04-24): Notion 리포트 DB 는 "승인된 최종본 영구 보관소" 다. AI 가 곧바로 Notion 에 쓰면 검토 흔적이 남지 않고, 학부모 발송 직전 톤 수정도 어렵다. 그래서 **HTML 드래프트 → 승인 → 아카이브** 로 분리.

드래프트 HTML에는 `AI 품질 점검` 섹션이 함께 표시된다. 근거 표현, 다음 액션, 표현 안전, 학생 개인화,
학원 데이터 반영 여부를 deterministic rule로 검사해 `ready` / `review` / `blocked` 상태와 경고를 붙인다.
`blocked`는 기본 승인에서 제외된다. 원장/교사가 수정하거나 재생성한 뒤 승인하는 것을 기본으로 하며,
정말 아카이브해야 할 때만 `--force-blocked-quality` 를 붙인다.

운영 UI에서는 리포트 탭의 `주간 드래프트 검토` 큐가 `/api/weekly-drafts`를 통해 `drafts.json`을 읽는다.
여기서 학생별 품질 상태, 점수, 경고, 학생 맥락, HTML 링크를 확인한 뒤 승인한다.
같은 리포트 탭의 `개별 리포트 구성` 패널은 `/api/report-compositions`를 통해 드래프트 생성 전
학생별 출결·숙제·시험·메모·다음 액션 섹션 준비도와 보강 항목을 확인한다.

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
- `src/classin_toolkit/ui.py` (`/api/report-compositions`, `/api/weekly-drafts`, 리포트 탭 UI)
- `src/classin_toolkit/intelligence/report_composition.py` (드래프트 생성 전 개별 리포트 구성 preflight)
- `src/classin_toolkit/intelligence/weekly_report.py` (Claude 리포트 생성)
- `src/classin_toolkit/intelligence/report_quality.py` (드래프트 품질 점검)
- `src/classin_toolkit/intelligence/prompts/weekly_report.md` (프롬프트)
- `src/classin_toolkit/storage/notion_repo.py` (`archive_approved_weekly_report`)
- `src/classin_toolkit/storage/templates/weekly.html`

## 참고 문서

- `docs/10_architecture.md` 데이터 흐름 D
- `docs/12_notion_schema.md` §3 (리포트 DB)
- `docs/15_demo_scenario.md` (페르소나)
