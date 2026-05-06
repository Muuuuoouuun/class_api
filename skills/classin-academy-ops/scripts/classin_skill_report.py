#!/usr/bin/env python3
"""Report basic health for the repo-local classin skill pack."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SKILLS_DIR = ROOT / "skills"


def main() -> int:
    rows = []
    for skill_md in sorted(SKILLS_DIR.glob("classin-*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        frontmatter = _frontmatter(text)
        name = frontmatter.get("name", "")
        description = frontmatter.get("description", "")
        rows.append(
            {
                "skill": skill_md.parent.name,
                "frontmatter_name": name,
                "name_matches_folder": name == skill_md.parent.name,
                "has_description": bool(description.strip()),
                "has_todo": "TODO" in text or "[TODO" in text,
                "has_agents_metadata": (skill_md.parent / "agents" / "openai.yaml").exists(),
            }
        )

    summary = {
        "skills_dir": str(SKILLS_DIR),
        "skill_count": len(rows),
        "problems": [
            row
            for row in rows
            if not row["name_matches_folder"] or not row["has_description"] or row["has_todo"]
        ],
        "skills": rows,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["problems"] else 0


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---\n", text, flags=re.DOTALL)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


if __name__ == "__main__":
    raise SystemExit(main())
