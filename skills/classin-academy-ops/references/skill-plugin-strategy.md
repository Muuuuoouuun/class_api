# Skill And Plugin Strategy

Use a clean separation:

- Claude Agent: product runtime intelligence that the academy operator uses.
- Claude/Codex Skill: repo-local operating knowledge for AI assistants working on this project.
- Codex Plugin: installable distribution bundle once workflow, scripts, and integration policy are stable.

## Claude Agent

Use for user-facing runtime features:

- Natural-language operator chat: `classin-toolkit agent`.
- Tool-use queries over Notion and pipeline data.
- Parent-message composition, weekly reports, schedule parsing, and future OCR.

Keep Claude focused on analysis, text generation, and tool selection. Do not make Claude the source of truth for ClassIn IDs, Notion schema, or notification history.

## Claude/Codex Skills

Use for AI-assisted development and operations:

- Explain how to install or diagnose the toolkit.
- Route an AI assistant to the right layer and CLI command.
- Preserve project-specific safety rules that a general model might miss.
- Keep workflows deterministic and short enough to fit context.

This repo already stores skills in `skills/classin-*`. They are plain `SKILL.md` folders and can be symlinked into Claude or Codex skill directories.

## Codex Plugin

Create a plugin only after the skill pack is stable and there is a distribution need:

- Multiple academy PCs need the same installable package.
- The package needs bundled apps, MCP servers, hooks, or assets.
- A marketplace entry, install policy, or authentication policy matters.
- The user explicitly wants a plugin skeleton or publishable bundle.

Do not start with a plugin when a skill is enough. Plugins add packaging policy and lifecycle overhead.

## Recommended Path

1. Maintain the current `skills/classin-*` pack as the shared Claude/Codex knowledge base.
2. Use `classin-academy-ops` as the master entry point and router.
3. Add scripts only for deterministic checks that are repeatedly needed.
4. Once the pilot workflow is stable, scaffold a plugin named `classin-academy-toolkit`.
5. A future plugin should include:
   - skills copied or referenced from `skills/classin-*`
   - scripts for config diagnostics and sample-data validation
   - optional browser/UI helpers
   - optional MCP/app integration for GitHub/Notion if the deployment model requires it

## Plugin Readiness Checklist

- Skill pack has no TODO placeholders.
- `diagnose-apis` is stable and documented.
- Live ClassIn schedule creation has a tiny pilot workflow.
- Notion schema setup is repeatable.
- Notification live mode decision is settled.
- Install target is known: repo-local, home-local, or marketplace.
- Authentication policy is known: on install vs on use.
