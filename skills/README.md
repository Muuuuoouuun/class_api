# classin-toolkit skills

`classin-toolkit` 운영·개발용 Claude 스킬 모음. 미래의 Claude가 이 레포에서 작업할 때 어떤 워크플로우/Layer를 어떻게 다뤄야 할지 알려주는 reference 문서다.

## 설치

스킬은 `~/.claude/skills/<name>/SKILL.md` 위치에서 자동 로드된다. 두 가지 방법:

```bash
# A. 심볼릭 링크 (레포 갱신 즉시 반영)
./skills/install.sh

# B. 수동 복사
cp -r skills/classin-* ~/.claude/skills/
```

확인:
```bash
ls ~/.claude/skills/ | grep classin-
```

## 스킬 목록

### 운영자 라인 (원장/컨설턴트가 toolkit 사용)

| 스킬 | 트리거 |
|---|---|
| [`classin-toolkit-overview`](classin-toolkit-overview/SKILL.md) | 이 레포가 뭔지 / 어떤 워크플로우 / 어떤 명령 — 전체 진입점 |
| [`classin-schedule-import`](classin-schedule-import/SKILL.md) | 학기 초 스케줄 CSV → ClassIn 수업 일괄 생성 |
| [`classin-webhook-handling`](classin-webhook-handling/SKILL.md) | Webhook 수신 서버 / 페이로드 재생 / Cmd 디스패치 |
| [`classin-missing-homework`](classin-missing-homework/SKILL.md) | 미제출 sweep + 카톡 문구 dry_run |
| [`classin-exam-import`](classin-exam-import/SKILL.md) | 시험 결과 CSV/JSON → Notion 시험 DB 적재 |
| [`classin-missing-exam`](classin-missing-exam/SKILL.md) | 특정 시험 미응시자 sweep |
| [`classin-weekly-reports`](classin-weekly-reports/SKILL.md) | 주간 학생별 리포트 드래프트 → 승인 → 아카이브 |
| [`classin-agent-usage`](classin-agent-usage/SKILL.md) | `agent` CLI / tool-use 채팅 사용법 |
| [`classin-readiness-check`](classin-readiness-check/SKILL.md) | `check-ready` / `diagnose-apis` 사전 점검 |

### 개발자 라인 (코드 수정 시 Layer 가이드)

| 스킬 | 트리거 |
|---|---|
| [`classin-architecture`](classin-architecture/SKILL.md) | Layer 분리 원칙 / 의존성 방향 — 코드 수정 전 진입점 |
| [`classin-api-integration`](classin-api-integration/SKILL.md) | Layer 1 — ClassIn v1/v2 서명, CED action, Webhook Cmd 추가 |
| [`classin-notion-schema`](classin-notion-schema/SKILL.md) | Layer 2 — Notion DB 5종 스키마, 컬럼 추가 절차 |
| [`classin-intelligence-prompts`](classin-intelligence-prompts/SKILL.md) | Layer 3 — Claude 프롬프트 작성·수정·tool-use 도구 추가 |
| [`classin-pipelines-guide`](classin-pipelines-guide/SKILL.md) | Layer 4 — 새 파이프라인 추가, 비즈니스 로직 위치 |
| [`classin-notify-dispatch`](classin-notify-dispatch/SKILL.md) | Layer 5 — 카톡 dry_run → 알리고/솔라피 live 전환 |

## 출처

스킬 본문은 `docs/` 의 동일 주제 문서를 압축한 것. 상세 스펙은 각 SKILL.md 가 가리키는 docs 파일 참조.

코드/스펙이 바뀌면 docs 가 같이 갱신되고, 스킬도 갱신해야 한다 (드리프트 금지).
