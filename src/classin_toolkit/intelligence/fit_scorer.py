from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ..config import AppConfig
from .claude_client import load_prompt, run_json
from .saenggibu_schema import StructuredSaenggibu

log = logging.getLogger(__name__)


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
        cit = it.citation.strip()
        # Phase 1: 부분문자열 매칭. 단, 짧은 단편(예: "작성")의 허위 검증을 막기 위해
        # 최소 길이 가드. Phase 2에서 문장 단위/유사도 매칭으로 강화 예정.
        it.citation_verified = (
            len(cit) >= 5
            and any(cit in e or e in cit for e in evidence)
        )
    return items


def score_fit(
    cfg: AppConfig, *, sg: StructuredSaenggibu, rubric: dict
) -> list[ScoreItem]:
    system = load_prompt("fit_score")
    payload = {"saenggibu": sg.model_dump(), "rubric": rubric}
    raw = run_json(cfg, system=system, user=json.dumps(payload, ensure_ascii=False))
    rows = raw.get("items", []) if isinstance(raw, dict) else []
    items: list[ScoreItem] = []
    for r in rows:
        try:
            items.append(ScoreItem(key=r["key"], score=int(r["score"]), citation=r.get("citation", "")))
        except (KeyError, ValueError, TypeError):
            log.warning("malformed score row skipped: %r", r)
            continue
    return validate_citations(items, sg)
