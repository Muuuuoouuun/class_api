---
name: classin-academy-ops
description: Use when operating, diagnosing, packaging, or evolving the classin-toolkit academy automation project across ClassIn API, Notion DB, Claude intelligence, Kakao/Aligo notifications, Codex/Claude skills, or plugin decisions. Trigger for setup checks, live API access checks, safe rollout planning, skill/plugin packaging, and choosing which classin-* workflow skill or CLI command to use.
---

# ClassIn Academy Ops

Master operating skill for `classin-toolkit`: ClassIn scheduling, homework/exam sweeps, Notion storage, Claude reports/agent, and notification dispatch. Use this first to choose the safe path, then load narrower `classin-*` skills only when needed.

## First Moves

1. Run `git status -sb` and preserve unrelated user changes.
2. Read `README.md`, `docs/00_index.md`, and the relevant existing skill from `skills/classin-*`.
3. If config/API readiness matters, start with `classin-readiness-check` and prefer non-mutating checks.
4. If code changes are needed, identify the layer before editing:
   - Layer 1: `src/classin_toolkit/classin/*`
   - Layer 2: `src/classin_toolkit/storage/*`
   - Layer 3: `src/classin_toolkit/intelligence/*`
   - Layer 4: `src/classin_toolkit/pipelines/*`
   - Layer 5: `src/classin_toolkit/notify/*`
5. Before finalizing code, run the smallest useful tests, then expand to `ruff` and full `pytest` when feasible.

## Safety Gates

- Do not print `config.yaml` secrets, API tokens, phone numbers, SSO links, or generated parent-message payloads unless explicitly sanitized.
- Do not run ClassIn mutation commands (`parse-schedule --live`, account/class creation, homework release) until `check-ready` and `diagnose-apis --live` pass for the relevant service.
- Keep notification work in `notify.mode: dry_run` unless Aligo template approval, sender, and live-mode implementation are explicitly confirmed.
- Treat Notion DB writes as production writes. Prefer `--dry-run` for exam imports and replay sample webhooks before live data.
- Pause before plugin/marketplace packaging if distribution scope, install location, or authentication policy is unclear.

## Workflow Router

| User intent | Load skill | Command or area |
|---|---|---|
| Overall orientation | `classin-toolkit-overview` | `README.md`, `docs/10_architecture.md` |
| Setup/API check | `classin-readiness-check` | `check-ready`, `diagnose-apis` |
| Schedule/ClassIn creation | `classin-schedule-import` + `classin-api-integration` | `parse-schedule` |
| Webhook ingest | `classin-webhook-handling` | `classin-webhook`, `replay-webhook` |
| Homework missing alerts | `classin-missing-homework` | `sweep-missing-homework` |
| Exam import/missing alerts | `classin-exam-import`, `classin-missing-exam` | `import-exam-results`, `sweep-missing-exam` |
| Weekly reports | `classin-weekly-reports` | `generate-weekly-drafts`, `approve-weekly` |
| Natural-language operator agent | `classin-agent-usage`, `classin-intelligence-prompts` | `classin-toolkit agent` |
| Notion schema/storage changes | `classin-notion-schema` | `storage/notion_repo.py` |
| Notification provider/live sending | `classin-notify-dispatch` | `notify/dispatcher.py` |
| Codex/Claude skill or plugin packaging | this skill + `references/skill-plugin-strategy.md` | `skills/`, future plugin skeleton |

## API Diagnosis Pattern

Use this sequence unless the user explicitly requests a risky live action:

```bash
classin-toolkit check-ready --mode local-demo --config config.yaml
classin-toolkit check-ready --mode classin-live --config config.yaml
classin-toolkit diagnose-apis --config config.yaml
classin-toolkit diagnose-apis --live --config config.yaml
```

Interpretation details live in `references/live-api-checks.md`.

## Skill And Plugin Packaging

Use `references/skill-plugin-strategy.md` before creating packaging artifacts. Default decision:

- Claude Agent: product runtime inside `src/classin_toolkit/intelligence/agent.py`.
- Claude/Codex Skill: operational knowledge for this repo, stored in `skills/classin-*`.
- Codex Plugin: later distribution bundle when skills, scripts, MCP/app needs, and install policy are stable.

## Validation

For code changes:

```bash
.venv/bin/ruff check --no-cache .
.venv/bin/pytest -q
git diff --check
```

For skill-pack changes:

```bash
python3 skills/classin-academy-ops/scripts/classin_skill_report.py
python3 /Users/clmagi/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/classin-academy-ops
```

If a command cannot run in the current environment, report that clearly and keep the next step concrete.

## References

- `references/operating-map.md`: project commands, layers, and safe rollout checklist.
- `references/live-api-checks.md`: `diagnose-apis` probe meanings and required missing values.
- `references/skill-plugin-strategy.md`: Claude vs Codex skill vs Codex plugin decision guide.
