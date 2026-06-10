# Operating Map

## Core Commands

| Need | Command |
|---|---|
| Offline readiness | `classin-toolkit check-ready --mode local-demo --config config.yaml` |
| ClassIn readiness | `classin-toolkit check-ready --mode classin-live --config config.yaml` |
| Kakao readiness | `classin-toolkit check-ready --mode kakao-live --config config.yaml` |
| API probe table | `classin-toolkit diagnose-apis --config config.yaml` |
| Live non-mutating probes | `classin-toolkit diagnose-apis --live --config config.yaml` |
| Schedule dry-run | `classin-toolkit parse-schedule samples/schedule_sample.csv --dry-run --config config.yaml` |
| Schedule live write | `classin-toolkit parse-schedule <csv> --live --config config.yaml` |
| Webhook server | `classin-webhook --config config.yaml` |
| Replay sample webhook | `classin-toolkit replay-webhook samples/attendance_sample.json --config config.yaml` |
| Missing homework sweep | `classin-toolkit sweep-missing-homework --config config.yaml` |
| Exam import dry-run | `classin-toolkit import-exam-results <csv|json> --exam-name ... --exam-date ... --dry-run --config config.yaml` |
| Missing exam sweep | `classin-toolkit sweep-missing-exam --exam-name ... --exam-date ... --config config.yaml` |
| Weekly draft reports | `classin-toolkit generate-weekly-drafts --config config.yaml` |
| Approve weekly archive | `classin-toolkit approve-weekly --week YYYY-MM-DD --config config.yaml` |
| Operator chat | `classin-toolkit agent --config config.yaml` |
| Local UI | `classin-toolkit ui --config config.yaml` |

## Layer Ownership

| Layer | Owns | Do not leak into |
|---|---|---|
| `classin/` | ClassIn signing, HTTP envelopes, CED/Webhook schemas | pipelines, storage |
| `storage/` | Notion DB and output persistence | ClassIn API calls |
| `intelligence/` | Claude calls, prompts, agent tool schemas | direct DB mutation without tools |
| `pipelines/` | business workflows and orchestration | low-level signing or DB schemas |
| `notify/` | dry-run/live notification dispatch | report generation logic |

## Safe Rollout Checklist

1. Confirm `git status -sb` and avoid touching unrelated dirty files.
2. Confirm `config.yaml` exists, but never paste or print secrets.
3. Run `check-ready` for the target mode.
4. Run `diagnose-apis --live` before any live ClassIn/Notion/Claude/Aligo workflow.
5. Run dry-run/import preview paths first when available.
6. For live ClassIn schedule creation, require actual `teacherUid` mapping and a small pilot CSV.
7. For notifications, require dry-run artifact review before live mode.
8. After changes, run focused tests plus `ruff`/`pytest` when feasible.

## Values Needed For Full Pilot

- ClassIn `school_id`, `secret_key`, `webhook_secret`.
- Real ClassIn `teacherUid` mapping: `classin.teacher_uids` or `classin.default_teacher_uid`.
- A known existing `uid/course_id/class_id/telephone` tuple for strong SSO verification.
- Notion Integration token shared with all target DBs.
- Notion DB IDs: students, lessons, reports, memos, exams.
- Anthropic API key and chosen model names.
- Aligo API key, user id, sender, approved templates, and live dispatcher implementation.
- Cloudflare Tunnel public URL and ClassIn Datasub webhook registration.
