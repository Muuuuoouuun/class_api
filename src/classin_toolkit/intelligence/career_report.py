"""진학 리포트 생성기 — 진학 적합도 진단 결과 리포트."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..config import AppConfig
from .claude_client import load_prompt, run_structured
from .fit_scorer import ScoreItem


@dataclass
class CareerReport:
    summary_markdown: str
    parent_message: str

    @classmethod
    def parse(cls, text: str) -> "CareerReport":
        parts = text.split("## 학부모 카톡 문구", 1)
        summary = parts[0].strip()
        parent = parts[1].strip() if len(parts) == 2 else ""
        return cls(summary_markdown=summary, parent_message=parent)


def build_career_report(cfg: AppConfig, *, major: str, items: list[ScoreItem]) -> CareerReport:
    system = load_prompt("career_report")
    payload = {
        "major": major,
        "scores": [
            {
                "key": i.key,
                "score": i.score,
                "citation": i.citation,
                "citation_verified": i.citation_verified,
            }
            for i in items
        ],
    }
    text = run_structured(
        cfg,
        system=system,
        user=json.dumps(payload, ensure_ascii=False, indent=2),
        tier="report",
        max_tokens=2048,
    )
    return CareerReport.parse(text)
