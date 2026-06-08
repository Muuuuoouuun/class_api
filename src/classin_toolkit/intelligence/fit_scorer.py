from __future__ import annotations

import json
from dataclasses import dataclass

from ..config import AppConfig
from .claude_client import load_prompt, run_json
from .saenggibu_schema import StructuredSaenggibu


@dataclass
class ScoreItem:
    key: str
    score: int
    citation: str
    citation_verified: bool = False


def validate_citations(
    items: list[ScoreItem], sg: StructuredSaenggibu
) -> list[ScoreItem]:
    """인용이 생기부에 실제 존재하는지 대조 — 환각 차단."""
    evidence = sg.all_evidence()
    for it in items:
        it.citation_verified = bool(it.citation) and any(
            it.citation in e or e in it.citation for e in evidence
        )
    return items


def score_fit(
    cfg: AppConfig, *, sg: StructuredSaenggibu, rubric: dict
) -> list[ScoreItem]:
    system = load_prompt("fit_score")
    payload = {"saenggibu": sg.model_dump(), "rubric": rubric}
    raw = run_json(cfg, system=system, user=json.dumps(payload, ensure_ascii=False))
    rows = raw.get("items", []) if isinstance(raw, dict) else []
    items = [ScoreItem(key=r["key"], score=int(r["score"]), citation=r.get("citation", "")) for r in rows]
    return validate_citations(items, sg)
